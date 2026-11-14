import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pylamarzocco import LaMarzoccoMachine
from pylamarzocco.const import WidgetType, SteamTargetLevel, PreExtractionMode
from pylamarzocco.models import MachineStatus, CoffeeBoiler, SteamBoilerLevel, PreBrewing

from micra_bridge.backend import MachineBackend, BridgeError, snapshot
from micra_bridge.settings import InvalidSetting, validate_input
from micra_bridge.server import create_app

TOKEN = 'settings-test-only-' + 'x' * 32
AUTH = {'Authorization': 'Bearer ' + TOKEN}


def widgets():
    # Sanitized shape/limits observed on the user's Micra gateway 7.5.3.
    return {
        WidgetType.CM_MACHINE_STATUS: MachineStatus.from_dict({
            'status': 'PoweredOn', 'mode': 'BrewingMode', 'availableModes': ['BrewingMode','StandBy'], 'nextStatus': None}),
        WidgetType.CM_COFFEE_BOILER: CoffeeBoiler.from_dict({
            'status': 'HeatingUp', 'enabled': True, 'enabledSupported': False,
            'targetTemperature': 95.0, 'targetTemperatureMin': 80, 'targetTemperatureMax': 100, 'targetTemperatureStep': 0.1}),
        WidgetType.CM_STEAM_BOILER_LEVEL: SteamBoilerLevel.from_dict({
            'status': 'Off', 'enabled': False, 'enabledSupported': True,
            'targetLevel': 'Level2', 'targetLevelSupported': True}),
        WidgetType.CM_PRE_BREWING: PreBrewing.from_dict({
            'availableModes': ['PreBrewing','Disabled'], 'mode': 'PreBrewing', 'doseIndexSupported': False,
            'times': {'PreInfusion': [], 'PreBrewing': [{
                'secondsMin': {'In':1.0,'Out':1.0}, 'secondsMax': {'In':9.0,'Out':9.0},
                'secondsStep': {'In':0.1,'Out':0.1}, 'doseIndex':'ByGroup', 'seconds':{'In':2.0,'Out':4.0}}]}}),
    }


@pytest.fixture
def machine():
    m=LaMarzoccoMachine('TEST')
    m.dashboard.connected=True
    m.dashboard.config=widgets()
    m.get_dashboard=AsyncMock()
    for name in ['set_steam','set_steam_level','set_coffee_target_temperature','set_pre_extraction_mode','set_pre_extraction_times']:
        setattr(m,name,AsyncMock(return_value=True))
    return m


@pytest.mark.parametrize('setting,value,method,args',[
    ('steam_enabled',True,'set_steam',(True,)),
    ('steam_level',3,'set_steam_level',(SteamTargetLevel.LEVEL_3,)),
    ('coffee_temperature',93.5,'set_coffee_target_temperature',(93.5,)),
    ('prebrew_mode','Disabled','set_pre_extraction_mode',(PreExtractionMode.DISABLED,)),
    ('prebrew_on',2.5,'set_pre_extraction_times',(2.5,4.0)),
    ('prebrew_off',4.5,'set_pre_extraction_times',(2.0,4.5)),
])
async def test_control_mapping_preserves_other_setting(machine,setting,value,method,args):
    backend=MachineBackend(machine)
    await backend.set_setting(setting,value)
    getattr(machine,method).assert_awaited_once_with(*args)
    assert backend.cached is None  # Never report the requested value optimistically.


@pytest.mark.parametrize('setting,value',[
    ('steam_enabled','true'),('steam_enabled',1),('steam_level',True),('steam_level',1.5),
    ('steam_level',4),('coffee_temperature',100.1),('coffee_temperature',93.05),
    ('coffee_temperature',93.1),('prebrew_on',2.1),('prebrew_off',4.1),
    ('coffee_temperature',float('nan')),('coffee_temperature',float('inf')),
    ('coffee_temperature','95'),('prebrew_on',0),('prebrew_off',9.1),
    ('prebrew_on',2.01),('prebrew_mode','PreInfusion'),('prebrew_mode',[]),
    ('update_firmware',True),([],True),
])
async def test_invalid_values_never_reach_upstream(machine,setting,value):
    with pytest.raises(InvalidSetting):
        await MachineBackend(machine).set_setting(setting,value)
    machine.get_dashboard.assert_not_awaited()


async def test_machine_limits_and_capabilities_are_checked_again(machine):
    backend=MachineBackend(machine)
    machine.dashboard.config[WidgetType.CM_COFFEE_BOILER].target_temperature_max=94
    with pytest.raises(InvalidSetting):await backend.set_setting('coffee_temperature',95)
    machine.set_coffee_target_temperature.assert_not_awaited()
    machine.dashboard.config[WidgetType.CM_STEAM_BOILER_LEVEL].enabled_supported=False
    with pytest.raises(InvalidSetting):await backend.set_setting('steam_enabled',True)
    machine.set_steam.assert_not_awaited()
    machine.dashboard.config[WidgetType.CM_PRE_BREWING].mode=PreExtractionMode.DISABLED
    with pytest.raises(InvalidSetting):await backend.set_setting('prebrew_on',3)
    machine.set_pre_extraction_times.assert_not_awaited()


async def test_other_prebrew_time_comes_from_fresh_read(machine):
    async def read():
        machine.dashboard.config[WidgetType.CM_PRE_BREWING].times.pre_brewing[0].seconds.seconds_out=7
    machine.get_dashboard.side_effect=read
    await MachineBackend(machine).set_setting('prebrew_on',3)
    machine.set_pre_extraction_times.assert_awaited_once_with(3.0,7)


async def test_offline_and_command_failure_never_replay(machine):
    backend=MachineBackend(machine)
    machine.dashboard.connected=False
    with pytest.raises(BridgeError,match='machine_offline'):await backend.set_setting('steam_enabled',True)
    machine.set_steam.assert_not_awaited()
    machine.dashboard.connected=True
    machine.set_steam.side_effect=TimeoutError('secret')
    with pytest.raises(BridgeError,match='command_outcome_unknown'):await backend.set_setting('steam_enabled',True)
    machine.set_steam.assert_awaited_once()


async def test_setting_http_auth_read_only_and_validation(machine):
    for control in (False,True):
        async with TestClient(TestServer(create_app(MachineBackend(machine),TOKEN,control))) as client:
            body={'setting':'coffee_temperature','value':93.5}
            assert (await client.post('/v1/settings',json=body)).status==401
            response=await client.post('/v1/settings',headers=AUTH,json=body)
            assert response.status==(202 if control else 403)
            if control:
                assert await response.json()=={'accepted':True,'confirmed':False}
                assert (await client.post('/v1/settings',headers=AUTH,json={'setting':'coffee_temperature','value':1000})).status==400
                assert (await client.post('/v1/settings',headers=AUTH,json={})).status==400
                assert (await client.post('/v1/settings',headers=AUTH,data='text')).status==415


async def test_partial_setting_push_is_immediate_and_never_merges_optimistic_state(machine):
    callbacks={};ready=asyncio.Event()
    async def watch(**kwargs):
        callbacks.update(kwargs);kwargs['connect_callback']();ready.set()
        await asyncio.Event().wait()
    machine.connect_dashboard_websocket=watch
    backend=MachineBackend(machine)
    backend.start();await ready.wait()
    try:
        before=await backend.status()
        # An optimistic mutation in the library must never leak into our confirmed copy.
        machine.dashboard.config[WidgetType.CM_COFFEE_BOILER].target_temperature=99
        update=deepcopy(widgets()[WidgetType.CM_STEAM_BOILER_LEVEL]);update.enabled=True
        waiter=asyncio.create_task(backend.events(before['event_cursor']))
        await asyncio.sleep(0)
        callbacks['update_callback'](SimpleNamespace(connected=True,config={WidgetType.CM_STEAM_BOILER_LEVEL:update}))
        after=await asyncio.wait_for(waiter,0.2)
        assert after['power'] is True and after['settings']['steam']['enabled'] is True
        assert after['settings']['coffee']['target']==95
        assert after['settings']['prebrew']['off']==4
    finally:await backend.close()
