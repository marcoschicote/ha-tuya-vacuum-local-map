"""The connection thread, against a fake tinytuya Device. No network, no HA."""

import logging
import time

import pytest
import tinytuya
from tinytuya.core import const, message_helper

from custom_components.tuya_vacuum_local_map import connection
from custom_components.tuya_vacuum_local_map.connection import (
    DeviceConnection,
    InvalidKey,
    ReadOnlyDevice,
    Unreachable,
    check_reply,
    probe,
)

from .fakes import DEVICE_ID, HOST, KEY, Factory, FakeDevice, wait_for


class Recorder:
    def __init__(self):
        self.dps, self.states, self.stalled = [], [], []

    def make(self, factory, **kwargs):
        defaults = dict(heartbeat_seconds=0.05, backoff_start=0.01, backoff_max=0.04)
        return DeviceConnection(
            DEVICE_ID,
            HOST,
            KEY,
            3.3,
            self.dps.append,
            self.states.append,
            self.stalled.append,
            device_factory=factory,
            **(defaults | kwargs),
        )


@pytest.fixture
def conns():
    made = []

    def build(factory, **kwargs):
        recorder = Recorder()
        conn = recorder.make(factory, **kwargs)
        made.append(conn)
        return recorder, conn

    yield build
    for conn in made:
        conn.stop()
        assert conn._thread is None or not conn._thread.is_alive()


# --- delivery ----------------------------------------------------------------------


def test_delivers_the_first_status_and_pushed_data_in_order(conns):
    device = FakeDevice(
        {"dps": {"1": "a"}},
        [{"dps": {"2": "b"}}, {"t": 5}, {"dps": {}}, {"dps": {"3": "c"}}],
    )
    rec, conn = conns(Factory(device))
    conn.start()
    assert wait_for(lambda: len(rec.dps) == 3)
    assert rec.dps == [{"1": "a"}, {"2": "b"}, {"3": "c"}]
    assert rec.states == [True] and conn.connected


def test_the_device_is_opened_persistent_with_the_given_details(conns):
    factory = Factory()
    rec, conn = conns(factory)
    conn.start()
    assert wait_for(lambda: rec.states == [True])
    assert factory.args[0] == (DEVICE_ID, HOST, KEY, 3.3, True)


def test_idle_receive_returns_none_without_reconnecting(conns):
    factory = Factory()
    rec, conn = conns(factory)
    conn.start()
    assert wait_for(lambda: factory.made[0].calls.count("receive") > 30)
    assert len(factory.made) == 1 and rec.states == [True] and conn.reconnects == 0


def test_heartbeats_are_sent_on_a_timer(conns):
    factory = Factory()
    rec, conn = conns(factory)
    conn.start()
    assert wait_for(lambda: factory.made[0].calls.count("heartbeat") >= 3)


# --- reading only ------------------------------------------------------------------


def test_only_status_heartbeat_receive_are_ever_called(conns):
    factory = Factory(FakeDevice(inbox=[{"dps": {"1": 1}}, {"Err": "905"}]))
    rec, conn = conns(factory)
    conn.start()
    assert wait_for(
        lambda: len(factory.made) >= 2 and "heartbeat" in factory.made[1].calls
    )
    allowed = {"status", "receive", "heartbeat", "set_socketTimeout"}
    for device in factory.made:
        assert set(device.calls) <= allowed, device.calls


@pytest.mark.parametrize(
    "name", FakeDevice.WRITES + ("set_dps", "anything_else", "_send_receive")
)
def test_the_wrapper_refuses_everything_else(name):
    device = ReadOnlyDevice(FakeDevice())
    with pytest.raises(AttributeError):
        getattr(device, name)
    with pytest.raises(AttributeError):
        device.status = lambda: None


def test_the_allowed_methods_exist_on_the_real_device_class():
    for name in connection._ALLOWED:
        assert callable(getattr(tinytuya.Device, name)), name


# --- errors and reconnecting -------------------------------------------------------


def test_an_error_dict_in_receive_means_a_lost_connection_and_a_reconnect(conns):
    factory = Factory(
        FakeDevice(inbox=[{"dps": {"1": 1}}, {"Err": "904"}]),
        FakeDevice(status={"dps": {"1": 2}}),
    )
    rec, conn = conns(factory)
    conn.start()
    assert wait_for(lambda: rec.states == [True, False, True])
    assert rec.dps[:3] == [{"1": True}, {"1": 1}, {"1": 2}]
    assert conn.reconnects >= 1
    assert factory.made[0].closed.is_set() and conn.last_error == "904"


@pytest.mark.parametrize(
    "error", [OSError("reset"), TimeoutError(), ValueError("x"), RuntimeError()]
)
def test_exceptions_in_receive_mean_a_reconnect(conns, error):
    factory = Factory(FakeDevice(inbox=[error]))
    rec, conn = conns(factory)
    conn.start()
    assert wait_for(lambda: rec.states == [True, False, True])


def test_an_error_reply_to_the_first_status_never_counts_as_connected(conns):
    factory = Factory(*[FakeDevice(status={"Err": "914"}) for _ in range(50)])
    rec, conn = conns(factory)
    conn.start()
    assert wait_for(lambda: len(factory.made) >= 3)
    assert rec.states == [] and rec.dps == [] and not conn.connected
    assert conn.last_error == "914"


def test_a_missing_first_status_is_a_failure_too(conns):
    broken = FakeDevice()
    broken.status = lambda nowait=False: None
    factory = Factory(broken)
    rec, conn = conns(factory)
    conn.start()
    assert wait_for(lambda: rec.states == [True])  # the second device is fine
    assert len(factory.made) == 2 and broken.closed.is_set()


def test_backoff_doubles_up_to_the_limit(conns, monkeypatch):
    waits = []
    factory = Factory(*[FakeDevice(status={"Err": "905"}) for _ in range(20)])
    rec, conn = conns(factory, backoff_start=5, backoff_max=40)
    monkeypatch.setattr(conn._stop, "wait", lambda t: waits.append(t) or len(waits) > 6)
    conn.start()
    assert wait_for(lambda: len(waits) > 6)
    assert waits[:6] == [5, 10, 20, 40, 40, 40]


def test_a_good_connection_resets_the_backoff(conns, monkeypatch):
    waits = []
    factory = Factory(
        FakeDevice(status={"Err": "905"}),
        FakeDevice(status={"Err": "905"}),
        FakeDevice(inbox=[{"Err": "905"}]),
        FakeDevice(status={"Err": "905"}),
        *[FakeDevice(status={"Err": "905"}) for _ in range(10)],
    )
    rec, conn = conns(factory, backoff_start=5, backoff_max=40)
    monkeypatch.setattr(conn._stop, "wait", lambda t: waits.append(t) or len(waits) > 4)
    conn.start()
    assert wait_for(lambda: len(waits) > 4)
    assert waits[:4] == [5, 10, 5, 10]


def test_a_failing_callback_does_not_kill_the_thread(conns):
    factory = Factory(FakeDevice(inbox=[{"dps": {"1": 1}}, {"dps": {"2": 2}}]))
    seen = []

    def bad(dps):
        seen.append(dps)
        raise RuntimeError("boom")

    conn = DeviceConnection(
        DEVICE_ID, HOST, KEY, 3.3, bad, device_factory=factory, heartbeat_seconds=0.05
    )
    conns_list = [conn]
    conn.start()
    assert wait_for(lambda: len(seen) == 3)
    assert len(factory.made) == 1
    conn.stop()
    del conns_list


# --- stopping ----------------------------------------------------------------------


def test_stop_ends_the_thread_and_closes_the_socket(conns):
    factory = Factory()
    rec, conn = conns(factory)
    conn.start()
    assert wait_for(lambda: conn.connected)
    conn.stop()
    assert not conn._thread.is_alive()
    assert factory.made[0].closed.is_set()
    assert rec.states == [True, False]
    n = len(factory.made)
    time.sleep(0.1)
    assert len(factory.made) == n  # nothing reconnects after stop


def test_stop_during_backoff_returns_at_once(conns):
    factory = Factory(*[FakeDevice(status={"Err": "905"}) for _ in range(5)])
    rec, conn = conns(factory, backoff_start=30, backoff_max=30)
    conn.start()
    assert wait_for(lambda: len(factory.made) == 1 and conn._device is None)
    start = time.monotonic()
    conn.stop()
    assert time.monotonic() - start < 1 and not conn._thread.is_alive()


def test_the_thread_is_a_daemon(conns):
    rec, conn = conns(Factory())
    conn.start()
    assert conn._thread.daemon


def test_start_twice_is_an_error(conns):
    rec, conn = conns(Factory())
    conn.start()
    with pytest.raises(RuntimeError):
        conn.start()


# --- logging and the repair signal ---------------------------------------------------


def test_the_first_failure_warns_and_the_rest_are_debug(conns, caplog):
    caplog.set_level(logging.DEBUG, logger=connection.__name__)
    factory = Factory(*[FakeDevice(status={"Err": "905"}) for _ in range(30)])
    rec, conn = conns(factory)
    conn.start()
    assert wait_for(lambda: len(factory.made) >= 5)
    conn.stop()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1 and len(debugs) >= 3


def test_a_new_outage_after_a_good_connection_warns_again(conns, caplog):
    caplog.set_level(logging.DEBUG, logger=connection.__name__)
    factory = Factory(
        FakeDevice(inbox=[{"Err": "905"}]),
        FakeDevice(status={"Err": "905"}),
        FakeDevice(inbox=[{"Err": "905"}]),
    )
    rec, conn = conns(factory)
    conn.start()
    assert wait_for(lambda: len(factory.made) >= 4)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 2


def test_logs_never_contain_the_key_the_id_or_the_host(conns, caplog):
    caplog.set_level(logging.DEBUG)
    factory = Factory(
        FakeDevice(status={"Err": "914"}),
        FakeDevice(
            inbox=[OSError(f"connect to {HOST} failed, key {KEY}, {DEVICE_ID}")]
        ),
    )
    rec, conn = conns(factory)
    conn.start()
    assert wait_for(lambda: len(factory.made) >= 3)
    conn.stop()
    text = caplog.text
    assert text
    for private in (KEY, DEVICE_ID, HOST):
        assert private not in text


def test_stalled_turns_on_after_a_long_outage_and_off_on_recovery(conns):
    now = [0.0]
    factory = Factory(
        *[FakeDevice(status={"Err": "901"}) for _ in range(3)], FakeDevice(inbox=[])
    )
    rec, conn = conns(factory, stalled_after=600, clock=lambda: now[0])

    def tick(device_count):
        wait_for(lambda: len(factory.made) >= device_count)

    conn.start()
    tick(2)
    assert rec.stalled == []
    now[0] = 601
    tick(4)
    assert wait_for(lambda: rec.stalled == [True, False])
    assert conn.connected


def test_stalled_is_reported_once(conns):
    now = [1000.0]
    factory = Factory(*[FakeDevice(status={"Err": "901"}) for _ in range(30)])
    rec, conn = conns(factory, stalled_after=10, clock=lambda: now[0])
    conn.start()
    assert wait_for(lambda: len(factory.made) >= 2)
    now[0] = 2000.0
    assert wait_for(lambda: len(factory.made) >= 6)
    assert rec.stalled == [True]


# --- check_reply and probe ---------------------------------------------------------


@pytest.mark.parametrize("reply", [None, {}, {"dps": {"1": 1}}, {"dps": {}}, "text", 5])
def test_check_reply_accepts_data_and_idle(reply):
    check_reply(reply)


@pytest.mark.parametrize("code", ["914", "904"])
def test_check_reply_flags_key_or_version_errors(code):
    with pytest.raises(InvalidKey) as raised:
        check_reply({"Err": code, "Error": "x"})
    assert raised.value.code == code


@pytest.mark.parametrize("code", ["901", "902", "905", "900", 905, None])
def test_check_reply_flags_other_errors_as_unreachable(code):
    with pytest.raises(Unreachable):
        check_reply({"Err": code})


def factory_for(device):
    return lambda *args: device


def test_probe_succeeds_on_a_normal_reply():
    device = FakeDevice()
    probe(DEVICE_ID, HOST, KEY, 3.3, factory_for(device))
    assert device.closed.is_set() and device.calls == ["set_socketTimeout", "status"]


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ({"Err": "914"}, InvalidKey),
        ({"Err": "904"}, InvalidKey),
        ({"Err": "905"}, Unreachable),
        ({"Err": "901"}, Unreachable),
        ({"Err": "902"}, Unreachable),
        (None, Unreachable),
    ],
)
def test_probe_maps_errors(reply, error):
    device = FakeDevice(status=reply)
    device.status = lambda nowait=False, reply=reply: reply
    with pytest.raises(error):
        probe(DEVICE_ID, HOST, KEY, 3.3, factory_for(device))
    assert device.closed.is_set()


def test_probe_turns_exceptions_into_unreachable_without_the_key():
    device = FakeDevice(status=OSError(f"{KEY} {HOST}"))
    with pytest.raises(Unreachable) as raised:
        probe(DEVICE_ID, HOST, KEY, 3.3, factory_for(device))
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert KEY not in str(raised.value)


def test_probe_opens_a_non_persistent_connection():
    seen = []

    def factory(*args):
        seen.append(args)
        return FakeDevice()

    probe(DEVICE_ID, HOST, KEY, 3.4, factory)
    assert seen == [(DEVICE_ID, HOST, KEY, 3.4, False)]


# --- tinytuya's size limit ---------------------------------------------------------


def test_the_payload_limit_is_raised_in_both_places_tinytuya_reads_it():
    assert const.MAX_PAYLOAD_LENGTH >= 65536
    assert message_helper.MAX_PAYLOAD_LENGTH >= 65536
    assert connection.MAX_PAYLOAD_LENGTH == 65536


def test_a_frame_bigger_than_the_stock_limit_passes_the_header_check():
    import struct

    from tinytuya.core import parse_header

    payload_len = 5000  # tinytuya's own limit is 1440
    header = struct.pack(">IIII", 0x000055AA, 1, 8, payload_len)
    assert parse_header(header + bytes(payload_len)).length == payload_len
