import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pylamarzocco.const import MachineMode, MachineState, WidgetType
from pylamarzocco.models import MachineStatus, ThingDashboardConfig

from micra_bridge.backend import MachineBackend


class StreamingMachine:
    def __init__(self):
        self.dashboard = ThingDashboardConfig(serial_number='TEST', connected=True)
        self.dashboard.config = self.config(MachineMode.STANDBY)
        self.get_dashboard = AsyncMock()
        self.set_power = AsyncMock(return_value=True)
        self.callbacks = None
        self.ready = asyncio.Event()
        self.finish = asyncio.Event()
        self.stopped = False

    @staticmethod
    def config(mode):
        return {WidgetType.CM_MACHINE_STATUS: MachineStatus(
            mode=mode, status=MachineState.STANDBY,
            available_modes=[MachineMode.STANDBY, MachineMode.BREWING_MODE], next_status=None)}

    async def connect_dashboard_websocket(self, **callbacks):
        self.callbacks = callbacks
        callbacks['connect_callback']()
        self.ready.set()
        try:
            await self.finish.wait()
        except asyncio.CancelledError:
            pass  # Upstream also swallows cancellation before returning.
        finally:
            self.stopped = True
            callbacks['disconnect_callback']()

    def push(self, mode=MachineMode.BREWING_MODE, connected=True, has_state=True):
        self.callbacks['update_callback'](SimpleNamespace(
            connected=connected, config=self.config(mode) if has_state else {}))


@pytest.fixture
async def stream():
    machine = StreamingMachine()
    backend = MachineBackend(machine, cache_seconds=0)
    backend.start()
    await machine.ready.wait()
    yield machine, backend
    await backend.close()
    assert machine.stopped


async def test_push_updates_status_without_http_and_offline_is_authoritative(stream):
    machine, backend = stream
    machine.push()
    state = await backend.status()
    assert state['power'] is True and state['websocket_connected']
    assert state['websocket_events'] == 1
    machine.get_dashboard.assert_not_awaited()
    machine.push(connected=False, has_state=False)
    state = await backend.status()
    assert not state['online'] and state['power'] is None


async def test_ack_and_partial_widgets_do_not_refresh_old_state(stream):
    machine, backend = stream
    machine.push()
    old_time = backend.cached_at
    machine.push(has_state=False)
    assert backend.cached_at == old_time and backend.websocket_events == 1
    backend.cached_at -= 31
    state = await backend.status()
    machine.get_dashboard.assert_awaited_once()
    assert state['power'] is False


async def test_disconnect_falls_back_to_rest(stream):
    machine, backend = stream
    machine.push()
    machine.callbacks['disconnect_callback']()
    state = await backend.status()
    assert state['websocket_connected'] is False
    assert state['power'] is False
    machine.get_dashboard.assert_awaited_once()


async def test_push_wins_over_older_inflight_http(stream):
    machine, backend = stream
    async def read_with_push():
        machine.push()
    machine.get_dashboard.side_effect = read_with_push
    state = await backend.status()
    assert state['power'] is True


async def test_reauthentication_replaces_websocket_and_ignores_old_callbacks(stream):
    old, backend = stream
    replacement = StreamingMachine()
    replacement.get_dashboard.side_effect = lambda: old.push(MachineMode.BREWING_MODE)
    backend.reconnect = AsyncMock(return_value=replacement)
    old.get_dashboard.side_effect = RuntimeError('private response')
    assert (await backend.status())['power'] is False
    await replacement.ready.wait()
    replacement.push(MachineMode.STANDBY)
    old.push(MachineMode.BREWING_MODE)
    assert (await backend.status())['power'] is False
    assert backend.websocket_connected
    await asyncio.sleep(0)
    assert old.stopped
    await backend.close()
    assert replacement.stopped


async def test_command_cannot_create_optimistic_cached_state(stream):
    machine, backend = stream
    async def optimistic_command(enabled):
        machine.dashboard.config = machine.config(MachineMode.BREWING_MODE)
        return True
    machine.set_power.side_effect = optimistic_command
    await backend.set_power(True)
    assert backend.cached is None
    machine.set_power.assert_awaited_once_with(True)


async def test_supervisor_retries_when_library_returns(monkeypatch):
    machine = StreamingMachine()
    calls = []
    real_sleep = asyncio.sleep
    async def ended(**kwargs):
        calls.append(1)
        if len(calls) == 3:
            backend._streaming = False
    machine.connect_dashboard_websocket = ended
    delays = []
    async def sleep(delay):
        delays.append(delay)
        await real_sleep(0)
    monkeypatch.setattr('micra_bridge.backend.asyncio.sleep', sleep)
    backend = MachineBackend(machine)
    backend.start()
    await backend._ws_task
    assert len(calls) == 3 and delays == [5, 10]
    await backend.close()


async def test_lan_wait_returns_immediately_on_push_even_with_command_lock(stream):
    machine, backend = stream
    initial = await backend.status()
    waiter = asyncio.create_task(backend.events(initial['event_cursor']))
    await asyncio.sleep(0)
    assert not waiter.done() and backend.lan_event_waiters == 1
    async with backend.lock:
        machine.push()
        result = await asyncio.wait_for(waiter, 0.2)
    assert result['power'] is True
    assert result['event_cursor'] != initial['event_cursor']
    assert backend.lan_event_waiters == 0


async def test_lan_wait_heartbeat_and_restart_cursor(stream):
    machine, backend = stream
    state = await backend.status()
    result = await backend.events(state['event_cursor'], wait_seconds=0.01)
    assert result['event_cursor'] == state['event_cursor']
    assert backend.lan_event_waiters == 0
    # A cursor from a previous process must return immediately, even if its number is larger.
    result = await asyncio.wait_for(backend.events('deadbeef-999'), 0.2)
    assert result['event_cursor'] == state['event_cursor']


async def test_cancelled_lan_wait_does_not_leak_waiter(stream):
    _, backend = stream
    state = await backend.status()
    task = asyncio.create_task(backend.events(state['event_cursor']))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.lan_event_waiters == 0
