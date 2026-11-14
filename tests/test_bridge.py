import asyncio
import json
import stat
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pylamarzocco.const import MachineMode, MachineState, ModelCode, WidgetType
from pylamarzocco.models import MachineStatus, Thing, ThingDashboardConfig

from micra_bridge.backend import BridgeError, MachineBackend, connect, snapshot
from micra_bridge.config import save_private
from micra_bridge.server import create_app

TOKEN = "test-only-token-" + "a" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}


class FakeMachine:
    def __init__(self):
        self.dashboard = ThingDashboardConfig(serial_number="TEST", connected=True)
        self.dashboard.config[WidgetType.CM_MACHINE_STATUS] = MachineStatus(
            mode=MachineMode.STANDBY, status=MachineState.STANDBY,
            available_modes=[MachineMode.STANDBY, MachineMode.BREWING_MODE], next_status=None,
        )
        self.get_dashboard = AsyncMock()
        self.set_power = AsyncMock(return_value=True)


@pytest.fixture
def machine():
    return FakeMachine()


@pytest.fixture
async def http_client(machine):
    clients = []

    async def make(control=False):
        backend = MachineBackend(machine, cache_seconds=0)
        client = TestClient(TestServer(create_app(backend, TOKEN, control)))
        await client.start_server()
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()


async def test_status_authentication_and_no_secret_leak(http_client, machine):
    client = await http_client()
    assert (await client.get("/v1/status")).status == 401
    machine.get_dashboard.assert_not_called()
    response = await client.get("/v1/status", headers=AUTH)
    data = await response.json()
    assert data["power"] is False and data["online"] is True
    assert data["control_enabled"] is False
    assert "serial" not in json.dumps(data)
    assert "token" not in json.dumps(data)
    assert response.headers["Cache-Control"] == "no-store"


async def test_read_only_rejects_commands(http_client, machine):
    client = await http_client()
    response = await client.post("/v1/power", headers=AUTH, json={"on": True})
    assert response.status == 403
    machine.set_power.assert_not_called()


@pytest.mark.parametrize("body", [{"on": "false"}, {"on": 1}, {}, [], {"on": True, "extra": 1}])
async def test_rejects_ambiguous_power_commands(http_client, machine, body):
    client = await http_client(control=True)
    response = await client.post("/v1/power", headers=AUTH, json=body)
    assert response.status == 400
    machine.set_power.assert_not_called()


async def test_accepted_is_not_confirmed_and_next_read_is_authoritative(http_client, machine):
    client = await http_client(control=True)
    response = await client.post("/v1/power", headers=AUTH, json={"on": True})
    assert response.status == 202
    assert await response.json() == {"accepted": True, "confirmed": False}
    machine.set_power.assert_awaited_once_with(True)
    # Fake machine hasn't changed its actual mode, so we must still show off.
    data = await (await client.get("/v1/status", headers=AUTH)).json()
    assert data["power"] is False


async def test_offline_does_not_send_command(http_client, machine):
    machine.dashboard.connected = False
    client = await http_client(control=True)
    response = await client.post("/v1/power", headers=AUTH, json={"on": True})
    assert response.status == 503
    assert (await response.json())["error"] == "machine_offline"
    machine.set_power.assert_not_called()
    data = await (await client.get("/v1/status", headers=AUTH)).json()
    assert data["online"] is False and data["power"] is None


async def test_timeout_never_replays_command(http_client, machine):
    machine.set_power.side_effect = TimeoutError("SECRET-DO-NOT-RETURN")
    client = await http_client(control=True)
    response = await client.post("/v1/power", headers=AUTH, json={"on": True})
    assert response.status == 503
    assert "SECRET" not in await response.text()
    machine.set_power.assert_awaited_once()
    await client.get("/v1/status", headers=AUTH)
    machine.set_power.assert_awaited_once()


async def test_false_upstream_result_is_rejected(http_client, machine):
    machine.set_power.return_value = False
    client = await http_client(control=True)
    response = await client.post("/v1/power", headers=AUTH, json={"on": False})
    assert response.status == 503


async def test_failed_refresh_clears_old_state(machine):
    backend = MachineBackend(machine, cache_seconds=0)
    assert (await backend.status())["online"]
    machine.get_dashboard.side_effect = RuntimeError("raw account data")
    with pytest.raises(BridgeError, match="upstream_unavailable"):
        await backend.status()
    assert backend.cached is None
    machine.get_dashboard.side_effect = None
    assert (await backend.status())["online"]


async def test_backend_timeout_is_bounded(machine):
    async def slow():
        await asyncio.sleep(10)
    machine.get_dashboard.side_effect = slow
    backend = MachineBackend(machine, timeout=0.01)
    with pytest.raises(BridgeError, match="upstream_unavailable"):
        await backend.status()


async def test_read_failure_reconnects_without_replaying_power(machine, caplog):
    replacement = FakeMachine()
    reconnect = AsyncMock(return_value=replacement)
    backend = MachineBackend(machine, cache_seconds=0, reconnect=reconnect)
    machine.get_dashboard.side_effect = RuntimeError("SECRET upstream response")
    assert (await backend.status())["online"] is True
    assert backend.machine is replacement
    reconnect.assert_awaited_once()
    machine.set_power.assert_not_awaited()
    replacement.set_power.assert_not_awaited()
    assert "SECRET" not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_failed_reconnect_is_throttled_and_never_returns_stale_cache(machine):
    reconnect = AsyncMock(side_effect=RuntimeError("secret"))
    backend = MachineBackend(machine, cache_seconds=0, reconnect=reconnect)
    assert (await backend.status())["online"] is True
    machine.get_dashboard.side_effect = RuntimeError()
    for _ in range(2):
        with pytest.raises(BridgeError, match="upstream_unavailable"):
            await backend.status()
        assert backend.cached is None
    reconnect.assert_awaited_once()
    backend.last_reconnect -= 61
    replacement = FakeMachine()
    reconnect.side_effect = None
    reconnect.return_value = replacement
    assert (await backend.status())["online"] is True
    assert reconnect.await_count == 2


async def test_reconnect_shares_read_timeout_budget(machine):
    async def slow_reconnect():
        await asyncio.sleep(10)
    machine.get_dashboard.side_effect = RuntimeError()
    backend = MachineBackend(machine, timeout=0.01, reconnect=slow_reconnect)
    with pytest.raises(BridgeError, match="upstream_unavailable"):
        await backend.status()


async def test_failed_power_is_not_retried_or_reconnected(machine):
    reconnect = AsyncMock()
    backend = MachineBackend(machine, reconnect=reconnect)
    machine.set_power.side_effect = TimeoutError()
    with pytest.raises(BridgeError, match="command_outcome_unknown"):
        await backend.set_power(True)
    machine.set_power.assert_awaited_once_with(True)
    reconnect.assert_not_awaited()


def test_unknown_mode_is_not_falsely_off(machine):
    machine.dashboard.config.clear()
    assert snapshot(machine)["power"] is None
    assert snapshot(machine)["online"] is False


def test_private_config_is_atomic_and_private(tmp_path):
    path = tmp_path / "config.json"
    save_private(path, {"password": "first"})
    save_private(path, {"password": "second"})
    assert json.loads(path.read_text())["password"] == "second"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]


async def test_registration_key_survives_restart_and_failed_registration(tmp_path, monkeypatch):
    from micra_bridge import backend as module
    client = AsyncMock()
    client.list_things.return_value = [Thing(serial_number="MICRA-TEST", model_code=ModelCode.LINEA_MICRA)]
    keys = []

    def factory(**kwargs):
        keys.append(kwargs["installation_key"].installation_id)
        return client

    monkeypatch.setattr(module, "LaMarzoccoCloudClient", factory)
    path = tmp_path / "key.json"
    config = {"username": "fake@example.invalid", "password": "test"}
    client.async_register_client.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await connect(config, path, object())
    assert json.loads(path.read_text())["registered"] is False
    client.async_register_client.side_effect = None
    backend = await connect(config, path, object())
    assert backend.machine.serial_number == "MICRA-TEST"
    assert json.loads(path.read_text())["registered"] is True
    await connect(config, path, object())
    assert len(set(keys)) == 1
    assert client.async_register_client.await_count == 2


async def test_multiple_micras_require_explicit_selection(tmp_path, monkeypatch):
    from micra_bridge import backend as module
    client = AsyncMock()
    client.list_things.return_value = [Thing(serial_number="A"), Thing(serial_number="B")]
    monkeypatch.setattr(module, "LaMarzoccoCloudClient", lambda **kwargs: client)
    config = {"username": "fake@example.invalid", "password": "test"}
    path = tmp_path / "key.json"
    with pytest.raises(BridgeError, match="multiple_micras"):
        await connect(config, path, object())
    client.get_thing_dashboard.return_value = ThingDashboardConfig(serial_number="B")
    selected = await connect({**config, "serial": "B"}, path, object())
    assert selected.machine.serial_number == "B"


@pytest.mark.parametrize("model,returned_serial,error", [
    (ModelCode.LINEA_MICRA, "MR-TEST", None),
    (ModelCode.LINEA_MINI_R, "MR-TEST", "selected_machine_is_not_micra"),
    (ModelCode.LINEA_MICRA, "ANOTHER", "serial_mismatch"),
])
async def test_explicit_serial_works_without_listing_but_validates_identity(
    tmp_path, monkeypatch, model, returned_serial, error,
):
    from micra_bridge import backend as module
    client = AsyncMock()
    client.list_things.return_value = []
    client.get_thing_dashboard.return_value = ThingDashboardConfig(
        serial_number=returned_serial, model_code=model,
    )
    monkeypatch.setattr(module, "LaMarzoccoCloudClient", lambda **kwargs: client)
    config = {"username": "fake@example.invalid", "password": "test", "serial": "MR-TEST"}
    if error:
        with pytest.raises(BridgeError, match=error):
            await connect(config, tmp_path / "key.json", object())
    else:
        result = await connect(config, tmp_path / "key.json", object())
        assert result.machine.serial_number == "MR-TEST"
    client.list_things.assert_not_awaited()
    client.get_thing_dashboard.assert_awaited_once_with("MR-TEST")
    client.set_power.assert_not_awaited()


async def test_event_endpoint_requires_auth_and_validates_cursor(http_client):
    client = await http_client()
    assert (await client.get('/v1/events')).status == 401
    assert (await client.get('/v1/events?after=invalid', headers=AUTH)).status == 400
    response = await client.get('/v1/events', headers=AUTH)
    assert response.status == 200
    data = await response.json()
    assert data['event_cursor'] and data['power'] is False
