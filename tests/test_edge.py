"""Run driver Lua under Lua 5.4 with mocked SmartThings modules, not a real hub."""
from pathlib import Path

import pytest
import yaml
from lupa.lua54 import LuaRuntime

ROOT = Path(__file__).parents[1]


@pytest.fixture
def lua():
    runtime = LuaRuntime(unpack_returned_tuples=True)
    runtime.execute('''
      package.preload["cosock"] = function() return {spawn=function(fn) spawned=fn end, asyncify=function() return {
        request=function(r)
          last_request=r
          if response_error then return nil, "timeout" end
          r.sink("{}"); r.sink(nil)
          return 1, response_code or 200
        end
      } end} end
      package.preload["cosock.socket"] = function() return {} end
      package.preload["ltn12"] = function() return {source={string=function(s) return s end}} end
      package.preload["st.json"] = function() return {
        encode=function(v) encoded_value=v; return v.on and '{"on":true}' or '{"on":false}' end,
        decode=function() return decoded_response or {} end
      } end
    ''')
    runtime.execute(f'package.path = "{ROOT}/edge/src/?.lua;" .. package.path')
    runtime.execute('bridge = require "bridge"')
    return runtime


@pytest.mark.parametrize("ip,valid", [
    ("192.168.1.150", True), ("10.0.0.2", True), ("172.16.0.2", True),
    ("100.64.0.2", False), ("8.8.8.8", False), ("127.0.0.1", False),
    ("192.168.1.999", False), ("192.168.1.2/path", False),
])
def test_lan_address_validation(lua, ip, valid):
    assert lua.globals().bridge.valid_ip(ip) == valid


def test_lua_rejects_stale_or_unknown_state(lua):
    lua.execute('''
      assert(bridge.valid_state({api_version=1,online=true,power=false,observed_at=1000},1000))
      assert(not bridge.valid_state({api_version=1,online=true,power=false,observed_at=100},1000))
      assert(not bridge.valid_state({api_version=1,online=false,power=false,observed_at=1000},1000))
      assert(not bridge.valid_state({api_version=1,online=true,observed_at=1000},1000))
    ''')


def test_lua_does_not_follow_redirect_or_retry(lua):
    lua.execute('''
      local prefs={bridgeIp="192.168.1.2", bridgeToken=string.rep("a",40)}
      response_code=302
      local result, err=bridge.request(prefs,"POST","/v1/power",{on=true})
      assert(result==nil and err=="Bridge HTTP 302")
      assert(last_request.redirect==false)
      assert(last_request.headers.Authorization=="Bearer "..prefs.bridgeToken)
      assert(last_request.source=='{"on":true}')
      response_code=202; decoded_response={accepted=true,confirmed=false}
      assert(bridge.request(prefs,"POST","/v1/power",{on=false}).accepted)
      assert(last_request.source=='{"on":false}')
    ''')


def test_driver_lifecycle_and_no_optimistic_switch(lua):
    lua.execute('''
      package.preload["log"] = function() return {warn=function() end} end
      package.preload["st.capabilities"] = function()
        local caps={
        switch={ID="switch",commands={on={NAME="on"},off={NAME="off"}},
          switch={on=function() return "on" end,off=function() return "off" end}},
        refresh={ID="refresh",commands={refresh={NAME="refresh"}}}
        }
        local specs={micraSteam={"enabled","level"},micraCoffeeTemperature={"target"},micraPrebrew={"mode","onTime","offTime"},micraSteamIntensity={"level"},micraPrebrewTimes={"onTime","offTime"}}
        for name,attrs in pairs(specs) do
          local id="islandventure29551."..name
          local cap={ID=id}
          for _,attr in ipairs(attrs) do
            cap[attr]=function(v) return {capability=id,attribute=attr,value=v} end
          end
          caps[id]=cap
        end
        return caps
      end
      package.preload["st.driver"] = function() return function(name,template)
        driver=template; driver.timers={}; driver.devices={}
        function driver:run() end
        function driver:get_devices() return self.devices end
        function driver:try_create_device(meta) self.created=meta end
        function driver:call_on_schedule(interval,callback)
          local timer={interval=interval,callback=callback}; self.timers[timer]=true; return timer
        end
        function driver:call_with_delay(delay,callback) return self:call_on_schedule(delay,callback) end
        function driver:cancel_timer(timer) self.timers[timer]=nil end
        return driver
      end end
      device={preferences={bridgeIp="192.168.1.2",bridgeToken=string.rep("a",40)},fields={},events={},component_events={},profile={components={steam={id="steam"}}}}
      function device:get_field(k) return self.fields[k] end
      function device:set_field(k,v) self.fields[k]=v end
      function device:online() self.connected=true end
      function device:offline() self.connected=false end
      function device:emit_event(e) self.events[#self.events+1]=e end
      function device:emit_component_event(component, events)
        for _, event in ipairs(events) do
          self.component_events[#self.component_events+1]={component=component.id,event=event}
        end
      end
      function device:supports_capability(cap) return not self.missing_profile end
      function device:try_update_metadata(meta)
        self.requested_profile=meta.profile
        self.metadata_updates=(self.metadata_updates or 0)+1
      end
      require "init"
      decoded_response={api_version=1,online=true,power=false,observed_at=os.time()}
      driver.lifecycle_handlers.init(driver,device)
      assert(device.connected==true and device.events[1]=="off")
      -- Existing devices already have every capability but need the new presentation.
      assert(device.requested_profile=="micra-controls-v3" and device.metadata_updates==1)
      driver.lifecycle_handlers.infoChanged(driver,device)
      assert(device.metadata_updates==1)
      device.events={device.events[1]}
      decoded_response={accepted=true,confirmed=false}; response_code=202
      driver.capability_handlers.switch.on(driver,device)
      assert(#device.events==1) -- No optimistic ON event.
      response_error=true
      device.fields.followup.callback()
      assert(device.connected==false and #device.events==1)
      driver.lifecycle_handlers.removed(driver,device)
      assert(device.fields.poll==nil and device.fields.followup==nil)
      driver.discovery(driver)
      assert(driver.created.type=="LAN" and driver.created.profile=="micra-controls-v3")
      response_error=false; response_code=200; device.events={}
      decoded_response={api_version=1,online=true,power=false,observed_at=os.time(),websocket_connected=true}
      driver.lifecycle_handlers.init(driver,device)
      assert(device.fields.poll.interval==30)
      local original=bridge.request
      bridge.request=function(prefs,method,path,body)
        if path:find("/v1/events",1,true) then return coroutine.yield("waiting") end
        return original(prefs,method,path,body)
      end
      local worker=coroutine.create(spawned)
      local ok, waiting=coroutine.resume(worker)
      assert(ok and waiting=="waiting" and not device.fields.busy)
      -- A push is emitted immediately without running any timer.
      ok, waiting=coroutine.resume(worker,{api_version=1,online=true,power=true,
        observed_at=os.time(),event_cursor="abc-2"})
      assert(ok and waiting=="waiting" and device.events[#device.events]=="on")
      -- A slower HTTP response cannot overwrite a newer push.
      decoded_response={api_version=1,online=true,power=false,observed_at=os.time(),event_cursor="abc-1"}
      driver.capability_handlers.refresh.refresh(driver,device)
      assert(device.events[#device.events]=="on")
      local count=#device.events
      driver.lifecycle_handlers.removed(driver,device)
      coroutine.resume(worker,{api_version=1,online=true,power=false,
        observed_at=os.time(),event_cursor="abc-3"})
      assert(coroutine.status(worker)=="dead" and #device.events==count)
      device.events={};device.fields.stream_id=nil;device.fields.stream_revision=nil
      bridge.request=original
      decoded_response={api_version=1,online=true,power=true,observed_at=os.time(),settings={
        steam={enabled=false,level=2},coffee={target=95},prebrew={mode="PreBrewing",on=2,off=4}}}
      driver.capability_handlers.refresh.refresh(driver,device)
      assert(#device.events==10)
      assert(#device.component_events==1)
      assert(device.component_events[1].component=="steam" and device.component_events[1].event=="off")
      decoded_response.settings.steam.enabled=true
      driver.capability_handlers.refresh.refresh(driver,device)
      assert(device.component_events[2].event=="on")
      -- Migration can deliver a state before the new component arrives.
      device.profile.components.steam=nil
      driver.capability_handlers.refresh.refresh(driver,device)
      assert(#device.component_events==2)
      device.profile.components.steam={id="steam"}
      assert(device.events[2].attribute=="enabled" and device.events[2].value=="off")
      assert(device.events[5].value.value==95 and device.events[5].value.unit=="C")
      assert(device.events[4].capability=="islandventure29551.micraSteamIntensity" and device.events[4].value==2)
      assert(device.events[8].capability=="islandventure29551.micraPrebrewTimes" and device.events[8].value.value==2)
      local count=#device.events
      decoded_response={accepted=true,confirmed=false};response_code=202
      driver.capability_handlers["islandventure29551.micraSteam"].setEnabled(driver,device,{args={value="on"}})
      assert(encoded_value.setting=="steam_enabled" and encoded_value.value==true)
      driver.capability_handlers["islandventure29551.micraSteam"].setEnabled(driver,device,{args={value="toggle"}})
      assert(encoded_value.setting=="steam_toggle" and encoded_value.value==true)
      assert(last_request.url:find("/v1/settings",1,true) and #device.events==count)
      driver.capability_handlers.switch.off(driver,device,{component="steam"})
      assert(encoded_value.setting=="steam_enabled" and encoded_value.value==false)
      assert(last_request.url:find("/v1/settings",1,true))
      driver.capability_handlers.switch.on(driver,device,{component="steam"})
      assert(encoded_value.setting=="steam_enabled" and encoded_value.value==true)
      driver.capability_handlers.switch.on(driver,device,{component="main"})
      assert(encoded_value.on==true and last_request.url:find("/v1/power",1,true))
      driver.capability_handlers.switch.off(driver,device,{component="main"})
      assert(encoded_value.on==false and last_request.url:find("/v1/power",1,true))
      driver.capability_handlers["islandventure29551.micraSteam"].setEnabled(driver,device,{args={value="on"}})
      assert(last_request.url:find("/v1/settings",1,true) and #device.events==count)
      driver.capability_handlers["islandventure29551.micraCoffeeTemperature"].setTarget(driver,device,{args={value=93.5}})
      assert(encoded_value.setting=="coffee_temperature" and encoded_value.value==93.5)
      driver.capability_handlers["islandventure29551.micraPrebrew"].setOffTime(driver,device,{args={value=4.5}})
      assert(encoded_value.setting=="prebrew_off" and encoded_value.value==4.5)
      driver.capability_handlers["islandventure29551.micraSteamIntensity"].setLevel(driver,device,{args={value=3}})
      assert(encoded_value.setting=="steam_level" and encoded_value.value==3)
      driver.capability_handlers["islandventure29551.micraPrebrewTimes"].setOnTime(driver,device,{args={value=2.5}})
      assert(encoded_value.setting=="prebrew_on" and encoded_value.value==2.5)
      driver.capability_handlers["islandventure29551.micraPrebrewTimes"].setOffTime(driver,device,{args={value=4.5}})
      assert(encoded_value.setting=="prebrew_off" and encoded_value.value==4.5)
      response_code=200; device.events={}
      decoded_response={api_version=1,online=true,power=true,observed_at=os.time(),
        cleaning={status="Cleaning",lastStarted="2026-11-14 09:00"},
        statistics={today="3회",week="14회",totalCoffee="2,121회",totalFlush="3,128회",updatedAt="11/14 09:00"},
        recent={latest="11/14 08:00 · 27.6초",previous="—",third="—",updatedAt="11/14 09:00"}}
      driver.capability_handlers.refresh.refresh(driver,device)
      assert(#device.events==1)
      -- All new capabilities are display-only.
      assert(driver.capability_handlers["islandventure29551.micraCleaning"]==nil)
      device.missing_profile=true
      driver.lifecycle_handlers.init(driver,device)
      assert(device.requested_profile=="micra-controls-v3")
      driver.lifecycle_handlers.removed(driver,device)
    ''')


def test_edge_profile_matches_driver():
    profile = yaml.safe_load((ROOT / "edge/profiles/micra.yaml").read_text())
    assert profile["name"] == "micra-bridge"
    assert {c["id"] for c in profile["components"][0]["capabilities"]} == {"switch", "refresh"}
    prefs = {p["name"]: p for p in profile["preferences"]}
    assert prefs["bridgeToken"]["definition"]["stringType"] == "password"
    expanded = yaml.safe_load((ROOT / "edge/profiles/micra-controls.yaml").read_text())
    assert expanded['name'] == 'micra-controls-v3'
    assert {c['id'] for c in expanded['components'][0]['capabilities']} == {
        'switch', 'refresh', 'islandventure29551.micraSteam',
        'islandventure29551.micraCoffeeTemperature', 'islandventure29551.micraPrebrew',
        'islandventure29551.micraSteamIntensity', 'islandventure29551.micraPrebrewTimes'}


@pytest.mark.parametrize('section', ['conditions', 'actions'])
def test_automation_controls_have_one_entry_per_setting(section):
    """Routine editors must not expose both legacy and split control rows."""
    import json
    from collections import Counter

    config = json.loads((ROOT / 'presentation/micra-controls.json').read_text())
    rows = []
    for entry in config['automation'][section]:
        if entry['component'] == 'steam':
            assert section == 'conditions' and entry['capability'] == 'switch'
            rows.append({'label': '스팀 보일러'})
        elif entry['capability'].startswith('islandventure29551.'):
            name = entry['capability'].split('.', 1)[1]
            presentation = json.loads((ROOT / f'capabilities/{name}.presentation.json').read_text())
            rows.extend(presentation['automation'][section])
    assert Counter(row['label'] for row in rows) == Counter({
        '스팀 보일러': 1, '스팀 강도': 1, '커피 목표 온도 (℃)': 1,
        '프리브루잉': 1, '프리브루잉 물 공급 시간 (초)': 1,
        '프리브루잉 대기 시간 (초)': 1,
    })


def test_native_steam_condition_preserves_custom_controls():
    import json
    config = json.loads((ROOT / 'presentation/micra-controls.json').read_text())
    profile = yaml.safe_load((ROOT / 'edge/profiles/micra-controls.yaml').read_text())
    steam = next(c for c in profile['components'] if c['id'] == 'steam')
    assert steam['label'] == '스팀 보일러'
    assert steam['capabilities'] == [{'id': 'switch', 'version': 1}]
    assert profile['metadata']['vid']
    # Embedded config would override the manual presentation and show duplicate rows.
    assert all('config' not in cap for c in profile['components'] for cap in c['capabilities'])
    assert sum(e['component'] == 'steam' for e in config['automation']['conditions']) == 1
    for section in [config['detailView'], config['automation']['actions']]:
        assert not any(e['component'] == 'steam' for e in section)
        assert any(e['capability'] == 'islandventure29551.micraSteam' for e in section)


def test_steam_toggle_is_an_action_not_a_reported_state():
    import json
    definition = json.loads((ROOT / 'capabilities/micraSteam.definition.json').read_text())
    presentation = json.loads((ROOT / 'capabilities/micraSteam.presentation.json').read_text())
    config = json.loads((ROOT / 'presentation/micra-controls.json').read_text())
    assert definition['attributes']['enabled']['schema']['properties']['value']['enum'] == ['on', 'off']
    assert definition['commands']['setEnabled']['arguments'][0]['schema']['enum'] == ['on', 'off', 'toggle']
    action = presentation['automation']['actions'][0]['list']
    assert action['command'] == 'setEnabled'
    assert [(a['key'], a['value']) for a in action['alternatives']] == [
        ('on', '켜짐'), ('off', '꺼짐'), ('toggle', '켜짐 또는 꺼짐')]
    entry = next(e for e in config['automation']['actions'] if e['capability'].endswith('.micraSteam'))
    assert entry['values'][0]['enabledValues'] == ['on', 'off', 'toggle']
