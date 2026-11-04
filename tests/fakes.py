"""Fakes shared by the connection and Home Assistant tests."""

import threading
import time

KEY = "0123456789abcdef"
DEVICE_ID = "example-device-id"  # made up
HOST = "203.0.113.7"  # a documentation address


class FakeDevice:
    """Scripted like tinytuya.Device. Everything that could write is recorded."""

    WRITES = (
        "set_status",
        "set_value",
        "set_multiple_values",
        "send",
        "turn_on",
        "turn_off",
        "updatedps",
        "set_dpsUsed",
        "generate_payload",
    )

    def __init__(self, status=None, inbox=()):
        self.status_reply = status if status is not None else {"dps": {"1": True}}
        self.inbox = list(inbox)
        self.calls = []
        self.closed = threading.Event()

    def status(self, nowait=False):
        self.calls.append("status")
        return self._reply(self.status_reply)

    def receive(self):
        self.calls.append("receive")
        if self.inbox:
            return self._reply(self.inbox.pop(0))
        time.sleep(0.003)  # a socket timeout: tinytuya returns None
        return None

    def heartbeat(self, nowait=True):
        self.calls.append("heartbeat")
        return None

    def set_socketTimeout(self, seconds):
        self.calls.append("set_socketTimeout")

    def set_socketRetryLimit(self, limit):
        self.calls.append("set_socketRetryLimit")

    def set_socketRetryDelay(self, delay):
        self.calls.append("set_socketRetryDelay")

    def close(self):
        self.closed.set()

    @staticmethod
    def _reply(value):
        if isinstance(value, Exception):
            raise value
        return value

    def __getattr__(self, name):
        if name in self.WRITES:

            def write(*args, **kwargs):
                self.calls.append(name)
                raise AssertionError(f"{name} was called on the device")

            return write
        raise AttributeError(name)


class Factory:
    def __init__(self, *devices):
        self.devices = list(devices)
        self.made = []
        self.args = []

    def __call__(self, device_id, host, key, version, persist):
        self.args.append((device_id, host, key, version, persist))
        device = self.devices.pop(0) if self.devices else FakeDevice()
        self.made.append(device)
        return device


def wait_for(condition, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return False
