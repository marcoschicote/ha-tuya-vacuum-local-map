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
