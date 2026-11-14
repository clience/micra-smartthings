local Driver = require "st.driver"
local capabilities = require "st.capabilities"
local log = require "log"
local bridge = require "bridge"
local cosock = require "cosock"
local socket = require "cosock.socket"
local steam = capabilities["islandventure29551.micraSteam"]
local coffee = capabilities["islandventure29551.micraCoffeeTemperature"]
local prebrew = capabilities["islandventure29551.micraPrebrew"]
local intensity = capabilities["islandventure29551.micraSteamIntensity"]
local times = capabilities["islandventure29551.micraPrebrewTimes"]

local function apply_state(device, state, err)
  if state and type(state.event_cursor) == "string" then
    local stream, revision = state.event_cursor:match("^([a-f0-9]+)%-(%d+)$")
    revision = tonumber(revision)
    if stream and revision then
      if device:get_field("stream_id") == stream
        and revision < (device:get_field("stream_revision") or 0) then return end
      device:set_field("stream_id", stream)
      device:set_field("stream_revision", revision)
    end
  end
  if not bridge.valid_state(state, os.time()) then
    device:offline()
    if err then log.warn(err) end
    return
  end
  device:online()
  device:emit_event(state.power and capabilities.switch.switch.on()
    or capabilities.switch.switch.off())
  local settings = state.settings or {}
  if settings.steam and device:supports_capability(steam) then
    if type(settings.steam.enabled) == "boolean" then
      device:emit_event(steam.enabled(settings.steam.enabled and "on" or "off"))
    end
    local level = settings.steam.level
    if type(level) == "number" and level >= 1 and level <= 3 and level % 1 == 0 then
      device:emit_event(steam.level(level))
      if device:supports_capability(intensity) then device:emit_event(intensity.level(level)) end
    end
  end
  if settings.coffee and device:supports_capability(coffee) then
    local target = settings.coffee.target
    if type(target) == "number" and target >= 80 and target <= 100 then
      device:emit_event(coffee.target({value=target,unit="C"}))
    end
  end
  if settings.prebrew and device:supports_capability(prebrew) then
    local current = settings.prebrew
    if current.mode == "PreBrewing" or current.mode == "Disabled" then
      device:emit_event(prebrew.mode(current.mode))
    end
    if type(current.on) == "number" and current.on >= 1 and current.on <= 9 then
      device:emit_event(prebrew.onTime({value=current.on,unit="s"}))
      if device:supports_capability(times) then device:emit_event(times.onTime({value=current.on,unit="s"})) end
    end
    if type(current.off) == "number" and current.off >= 1 and current.off <= 9 then
      device:emit_event(prebrew.offTime({value=current.off,unit="s"}))
      if device:supports_capability(times) then device:emit_event(times.offTime({value=current.off,unit="s"})) end
    end
  end
end

local function refresh(driver, device)
  if device:get_field("busy") then return end
  device:set_field("busy", true)
  local stream, revision = device:get_field("stream_id"), device:get_field("stream_revision")
  local state, err = bridge.request(device.preferences, "GET", "/v1/status")
  device:set_field("busy", false)
  if not state and (stream ~= device:get_field("stream_id")
    or revision ~= device:get_field("stream_revision")) then return end
  apply_state(device, state, err)
end

local function listen(device)
  local generation = {}
  device:set_field("listener", generation)
  cosock.spawn(function()
    local cursor = ""
    while device:get_field("listener") == generation do
      local state, err = bridge.request(device.preferences, "GET", "/v1/events?after=" .. cursor)
      if device:get_field("listener") ~= generation then return end
      if state and type(state.event_cursor) == "string"
        and #state.event_cursor <= 64 and state.event_cursor:match("^[a-f0-9]+%-%d+$") then
        cursor = state.event_cursor
        apply_state(device, state, err)
      else
        -- Independent fallback timer still refreshes status. Never block commands
        -- behind the held event request or mark a healthy fallback offline.
        log.warn(err or "Invalid bridge event response")
        socket.sleep(5)
      end
    end
  end, "micra-events")
end

local function send_command(driver, device, path, body)
  if device:get_field("busy") then
    log.warn("Micra is busy; command was not queued")
    return
  end
  device:set_field("busy", true)
  local response, err = bridge.request(device.preferences, "POST", path, body)
  device:set_field("busy", false)
  if not response or response.accepted ~= true then
    log.warn(err or "Command not accepted; read status before trying again")
  end
  -- Never report the requested state optimistically or retry a power command.
  local previous = device:get_field("followup")
  if previous then driver:cancel_timer(previous) end
  device:set_field("followup", driver:call_with_delay(3, function()
    device:set_field("followup", nil)
    refresh(driver, device)
  end))
end

local function power(driver, device, enabled)
  send_command(driver, device, "/v1/power", {on=enabled})
end

local function setting_handler(name, is_switch)
  return function(driver, device, command)
    local value = command.args.value
    if is_switch then
      if value ~= "on" and value ~= "off" then return end
      value = value == "on"
    end
    send_command(driver, device, "/v1/settings", {setting=name,value=value})
  end
end

local function stop_timers(driver, device)
  -- An old listener exits after its bounded request returns; it cannot emit events.
  device:set_field("listener", nil)
  for _, field in ipairs({"poll", "followup"}) do
    local timer = device:get_field(field)
    if timer then driver:cancel_timer(timer) end
    device:set_field(field, nil)
  end
end

local function configure(driver, device)
  stop_timers(driver, device)
  if not device:supports_capability(times) then
    device:try_update_metadata({profile="micra-controls"})
  end
  local interval = math.max(30, math.min(300, tonumber(device.preferences.pollSeconds) or 30))
  device:set_field("poll", driver:call_on_schedule(interval, function()
    refresh(driver, device)
  end))
  refresh(driver, device)
  listen(device)
end

local function discovery(driver)
  for _, device in pairs(driver:get_devices()) do
    if device.device_network_id == "micra-lan-bridge-v1" then return end
  end
  driver:try_create_device({
    type = "LAN",
    device_network_id = "micra-lan-bridge-v1",
    label = "Linea Micra",
    profile = "micra-controls",
    manufacturer = "Personal Bridge",
    model = "Linea Micra Bridge",
    vendor_provided_label = "Linea Micra",
  })
end

Driver("micra-lan-bridge", {
  discovery = discovery,
  supported_capabilities = { capabilities.switch, capabilities.refresh, steam, coffee, prebrew, intensity, times },
  lifecycle_handlers = {
    init = configure,
    infoChanged = configure,
    removed = stop_timers,
  },
  capability_handlers = {
    [intensity.ID] = {setLevel = setting_handler("steam_level")},
    [times.ID] = {
      setOnTime = setting_handler("prebrew_on"),
      setOffTime = setting_handler("prebrew_off"),
    },
    [steam.ID] = {
      setEnabled = setting_handler("steam_enabled", true),
      setLevel = setting_handler("steam_level"),
    },
    [coffee.ID] = { setTarget = setting_handler("coffee_temperature") },
    [prebrew.ID] = {
      setMode = setting_handler("prebrew_mode"),
      setOnTime = setting_handler("prebrew_on"),
      setOffTime = setting_handler("prebrew_off"),
    },
    [capabilities.switch.ID] = {
      [capabilities.switch.commands.on.NAME] = function(driver, device) power(driver, device, true) end,
      [capabilities.switch.commands.off.NAME] = function(driver, device) power(driver, device, false) end,
    },
    [capabilities.refresh.ID] = {
      [capabilities.refresh.commands.refresh.NAME] = refresh,
    },
  },
}):run()
