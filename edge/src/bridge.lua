local http = require "cosock".asyncify "socket.http"
local socket = require "cosock.socket"
local ltn12 = require "ltn12"
local json = require "st.json"

local M = {}

function M.valid_ip(ip)
  if type(ip) ~= "string" then return false end
  local a, b, c, d = ip:match("^(%d+)%.(%d+)%.(%d+)%.(%d+)$")
  a, b, c, d = tonumber(a), tonumber(b), tonumber(c), tonumber(d)
  if not a or not b or not c or not d then return false end
  if a > 255 or b > 255 or c > 255 or d > 255 then return false end
  return a == 10 or (a == 172 and b >= 16 and b <= 31) or (a == 192 and b == 168)
end

function M.request(preferences, method, path, body)
  local ip, port, token = preferences.bridgeIp, tonumber(preferences.bridgePort) or 8765,
    preferences.bridgeToken
  if not M.valid_ip(ip) or port < 1 or port > 65535 or port % 1 ~= 0
    or type(token) ~= "string" or #token < 32 or #token > 128
    or token:find("[^%w_%-]") then
    return nil, "Configure bridge LAN IP, port and access token"
  end
  local payload = body and json.encode(body) or nil
  local chunks, length = {}, 0
  local request = {
    url = "http://" .. ip .. ":" .. port .. path,
    method = method,
    redirect = false,
    headers = {
      ["Authorization"] = "Bearer " .. token,
      ["Accept"] = "application/json",
      ["Connection"] = "close",
    },
    create = function()
      local sock = socket.tcp()
      -- LuaSocket HTTP sets its own timeout, so override that per connection.
      return setmetatable({
        settimeout = function() return sock:settimeout(45) end,
      }, { __index = function(_, key)
        return function(_, ...) return sock[key](sock, ...) end
      end })
    end,
    sink = function(chunk)
      if chunk then
        length = length + #chunk
        if length > 8192 then return nil, "Response too large" end
        chunks[#chunks + 1] = chunk
      end
      return 1
    end,
  }
  if payload then
    request.source = ltn12.source.string(payload)
    request.headers["Content-Type"] = "application/json"
    request.headers["Content-Length"] = tostring(#payload)
  end
  local ok, result, code = pcall(http.request, request)
  if not ok or not result then return nil, "Bridge request failed" end
  if tonumber(code) ~= 200 and tonumber(code) ~= 202 then
    return nil, "Bridge HTTP " .. tostring(tonumber(code) or "error")
  end
  local decoded, value = pcall(json.decode, table.concat(chunks))
  if not decoded or type(value) ~= "table" then return nil, "Invalid bridge response" end
  return value
end

function M.valid_state(state, now)
  return type(state) == "table" and state.api_version == 1
    and state.online == true and type(state.power) == "boolean"
    and type(state.observed_at) == "number"
    and state.observed_at >= now - 90 and state.observed_at <= now + 60
end

return M
