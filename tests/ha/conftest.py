import pytest

from custom_components.tuya_vacuum_local_map import connection


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _fast_connection(monkeypatch):
    """Quick heartbeats and retries, so tests need not wait for real intervals."""
    monkeypatch.setattr(connection, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(connection, "BACKOFF_START", 0.01)
    monkeypatch.setattr(connection, "BACKOFF_MAX", 0.04)


@pytest.fixture
def factory(monkeypatch):
    """Patch the device factory; tests append the devices they want."""
    from ..fakes import Factory

    fake = Factory()
    monkeypatch.setattr(connection, "_tinytuya_device", fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    from custom_components.tuya_vacuum_local_map import coordinator

    from .common import CLOCK

    CLOCK[0] = 1000.0
    monkeypatch.setattr(coordinator, "_monotonic", lambda: CLOCK[0])
    return CLOCK
