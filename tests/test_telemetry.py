from unittest.mock import AsyncMock
from pylamarzocco.const import WidgetType
from pylamarzocco.models import BackFlush
from micra_bridge.backend import MachineBackend
from test_websocket import StreamingMachine


async def test_statistics_removed_from_background_work_and_status():
    machine = StreamingMachine()
    machine.get_coffee_and_flush_counter = AsyncMock()
    machine.get_coffee_and_flush_trend = AsyncMock()
    machine.get_last_coffee = AsyncMock()
    machine.dashboard.config[WidgetType.CM_BACK_FLUSH] = BackFlush(status="Off")
    backend = MachineBackend(machine)
    backend.start(); await machine.ready.wait()
    try:
        state = await backend.status()
        assert not {'statistics', 'recent', 'cleaning'}.intersection(state)
        machine.get_coffee_and_flush_counter.assert_not_awaited()
        machine.get_coffee_and_flush_trend.assert_not_awaited()
        machine.get_last_coffee.assert_not_awaited()
    finally: await backend.close()
