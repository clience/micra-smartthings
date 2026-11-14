"""Use the upstream public API; do not reimplement its authentication protocol."""

import asyncio
import json
import logging
import re
import time
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from aiohttp import ClientSession
from pylamarzocco import LaMarzoccoCloudClient, LaMarzoccoMachine
from pylamarzocco.const import MachineMode, ModelCode, WidgetType
from pylamarzocco.util import InstallationKey, generate_installation_key

from .config import save_private
from .settings import controls, prepare_command, validate_input

_LOGGER = logging.getLogger(__name__)


class BridgeError(Exception):
    """A fixed, credential-free error code suitable for API responses."""


def snapshot(machine: LaMarzoccoMachine) -> dict:
    dashboard = machine.dashboard
    status = dashboard.config.get(WidgetType.CM_MACHINE_STATUS)
    mode = getattr(status, "mode", None)
    power = None
    if mode == MachineMode.STANDBY:
        power = False
    elif mode in (MachineMode.BREWING_MODE, MachineMode.ECO_MODE):
        power = True
    connected = dashboard.connected is True
    return {
        "api_version": 1,
        "model": "Linea Micra",
        "online": connected and power is not None,
        "power": power if connected else None,
        "mode": str(mode) if mode is not None else None,
        "observed_at": int(time.time()),
        "transport": "cloud",
        "settings": controls(dashboard.config) if connected else {},
    }


async def connect(config: dict, key_path: Path, session: ClientSession):
    async def reconnect():
        return await _connect_machine(config, key_path, session)

    return MachineBackend(await reconnect(), reconnect=reconnect)


async def _connect_machine(config: dict, key_path: Path, session: ClientSession):
    if key_path.exists():
        record = json.loads(key_path.read_text())
        key = InstallationKey.from_json(record["key"])
    else:
        key = generate_installation_key(str(uuid.uuid4()).lower())
        record = {"key": key.to_json(), "registered": False}
        save_private(key_path, record)
    client = LaMarzoccoCloudClient(
        username=config["username"], password=config["password"],
        installation_key=key, client=session,
    )
    if not record.get("registered"):
        await client.async_register_client()
        record["registered"] = True
        save_private(key_path, record)
    selected = config.get("serial")
    if selected:
        if not isinstance(selected, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", selected):
            raise BridgeError("invalid_serial")
        # Some accounts return an empty /things list while the explicitly
        # selected machine's dashboard is accessible. Validate it directly.
        machine = LaMarzoccoMachine(selected, cloud_client=client)
        await machine.get_dashboard()
        if machine.dashboard.serial_number != selected:
            raise BridgeError("serial_mismatch")
        if machine.dashboard.model_code != ModelCode.LINEA_MICRA:
            raise BridgeError("selected_machine_is_not_micra")
        return machine
    things = await client.list_things()
    machines = [thing for thing in things if thing.model_code == ModelCode.LINEA_MICRA]
    if not machines:
        raise BridgeError("micra_not_found")
    if len(machines) != 1:
        raise BridgeError("multiple_micras_set_serial_in_config")
    machine = LaMarzoccoMachine(machines[0].serial_number, cloud_client=client)
    return machine


class MachineBackend:
    def __init__(self, machine, timeout: float = 20, cache_seconds: float = 15,
                 reconnect=None, reconnect_interval: float = 60):
        self.machine = machine
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.lock = asyncio.Lock()
        self.cached = None
        self.cached_at = 0.0
        self.reconnect = reconnect
        self.reconnect_interval = reconnect_interval
        self.last_reconnect = float("-inf")
        self.websocket_connected = False
        self.websocket_events = 0
        self.last_websocket_event = None
        self._streaming = False
        self._ws_tasks = set()
        self._ws_task = None
        self._revision = 0
        self._stream_id = uuid.uuid4().hex
        self._changed = asyncio.Event()
        self.lan_event_waiters = 0
        self._widgets = deepcopy(machine.dashboard.config)
        self._last_http_at = time.monotonic()

    def _notify(self):
        self._revision += 1
        self._changed.set()
        self._changed = asyncio.Event()

    def start(self):
        if self._streaming:
            return
        self._streaming = True
        self._start_websocket()

    def _start_websocket(self):
        if self._ws_task is not None:
            self._ws_task.cancel()
        self.websocket_connected = False
        if not self._streaming:
            return
        self._ws_task = asyncio.create_task(self._watch(self.machine))
        self._ws_tasks.add(self._ws_task)
        self._ws_task.add_done_callback(self._ws_tasks.discard)

    async def close(self):
        self._streaming = False
        self.websocket_connected = False
        tasks = list(self._ws_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch(self, machine):
        delay = 5

        def connected():
            nonlocal delay
            if self._streaming and self.machine is machine:
                self.websocket_connected = True
                self.cached = None
                self._notify()
                delay = 5
                _LOGGER.warning("Cloud WebSocket connected")

        def disconnected():
            if self.machine is machine:
                self.websocket_connected = False
                self.cached = None
                self._notify()

        def updated(config):
            if not self._streaming or self.machine is not machine:
                return
            relevant = {WidgetType.CM_MACHINE_STATUS, WidgetType.CM_COFFEE_BOILER,
                        WidgetType.CM_STEAM_BOILER_LEVEL, WidgetType.CM_PRE_BREWING}
            removed_keys = {item.code for item in getattr(config, "removed_widgets", [])}
            if config.connected and not relevant.intersection(set(config.config) | removed_keys):
                return
            # Upstream replaces dashboard widgets on partial pushes and mutates
            # command values optimistically. Merge into our own confirmed copy.
            for key in relevant.intersection(config.config):
                self._widgets[key] = deepcopy(config.config[key])
            for removed in getattr(config, "removed_widgets", []):
                self._widgets.pop(removed.code, None)
            dashboard = SimpleNamespace(connected=config.connected, config=self._widgets)
            self.cached = snapshot(SimpleNamespace(dashboard=dashboard))
            self.cached_at = time.monotonic()
            self._notify()
            self.websocket_events += 1
            self.last_websocket_event = int(time.time())

        while self._streaming and self.machine is machine:
            try:
                await machine.connect_dashboard_websocket(
                    update_callback=updated, connect_callback=connected,
                    disconnect_callback=disconnected, auto_reconnect=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning("Cloud WebSocket failed (%s)", type(exc).__name__)
            finally:
                disconnected()
            if (not self._streaming or self.machine is not machine
                    or asyncio.current_task().cancelling()):
                return
            # The library can return after a network/authentication error.
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)

    def _status_result(self, state):
        return {**state, "websocket_connected": self.websocket_connected,
                "websocket_events": self.websocket_events,
                "last_websocket_event": self.last_websocket_event,
                "event_cursor": f"{self._stream_id}-{self._revision}",
                "lan_event_waiters": self.lan_event_waiters}

    async def events(self, after: str, wait_seconds: float = 20):
        # One held LAN request returns immediately on a change, without a poll delay.
        # Keep the whole operation below the Edge HTTP client's 45-second timeout.
        try:
            async with asyncio.timeout(35):
                state = await self.status()
                if state["event_cursor"] != after:
                    return state
                changed = self._changed
                self.lan_event_waiters += 1
                try:
                    try:
                        await asyncio.wait_for(changed.wait(), wait_seconds)
                    except TimeoutError:
                        pass
                finally:
                    self.lan_event_waiters -= 1
                return await self.status()
        except TimeoutError:
            raise BridgeError("event_wait_timeout") from None

    async def _read(self) -> dict:
        # Discard the old cache before attempting a read; failures never look fresh.
        self.cached = None
        revision = self._revision
        try:
            async with asyncio.timeout(self.timeout):
                try:
                    await self.machine.get_dashboard()
                except Exception as exc:
                    # Log only the exception class, never upstream response/account data.
                    _LOGGER.warning("Dashboard read failed (%s)", type(exc).__name__)
                    now = time.monotonic()
                    if self.reconnect is None or now - self.last_reconnect < self.reconnect_interval:
                        raise
                    self.last_reconnect = now
                    # Recover a stale cloud client using the same installation key.
                    # This retries reads only; power commands are never replayed.
                    replacement = await self.reconnect()
                    await replacement.get_dashboard()
                    self.machine = replacement
                    self._widgets = deepcopy(replacement.dashboard.config)
                    # Discard any old-client push that arrived during reconnection.
                    self.cached = None
                    self._start_websocket()
                    _LOGGER.warning("Cloud connection recovered")
            self._last_http_at = time.monotonic()
            # A push received while HTTP was in flight is newer than that read.
            if self._revision != revision and self.cached is not None:
                return dict(self.cached)
            result = snapshot(self.machine)
            self._widgets = deepcopy(self.machine.dashboard.config)
        except Exception:
            raise BridgeError("upstream_unavailable") from None
        self.cached = result
        self.cached_at = time.monotonic()
        self._notify()
        return dict(result)

    async def status(self) -> dict:
        # Push snapshots are immutable dicts; serve them even while a command or
        # HTTP recovery holds the lock. The library's optimistic dashboard is not used.
        cache_seconds = 30 if self._streaming else self.cache_seconds
        if (self.cached is not None and time.monotonic() - self.cached_at < cache_seconds
                and (not self._streaming or time.monotonic() - self._last_http_at < 30)):
            return self._status_result(self.cached)
        async with self.lock:
            if (self.cached is not None and time.monotonic() - self.cached_at < cache_seconds
                    and (not self._streaming or time.monotonic() - self._last_http_at < 30)):
                return self._status_result(self.cached)
            return self._status_result(await self._read())

    async def set_power(self, enabled: bool) -> None:
        async with self.lock:
            state = await self._read()
            if not state["online"]:
                raise BridgeError("machine_offline")
            await self._command(lambda: self.machine.set_power(enabled))

    async def set_setting(self, setting, value):
        validate_input(setting, value)
        async with self.lock:
            state = await self._read()
            if not state["online"]:
                raise BridgeError("machine_offline")
            command = prepare_command(self.machine, state['settings'], setting, value)
            await self._command(command)

    async def _command(self, command):
        self.cached = None
        try:
            async with asyncio.timeout(self.timeout):
                accepted = await command()
        except Exception:
            # The command may already have reached the machine. Never retry it.
            raise BridgeError("command_outcome_unknown_refresh_status") from None
        if accepted is not True:
            raise BridgeError("command_not_confirmed")
        # Only an authoritative read/push may update our state, never the
        # library's optimistic dashboard mutation after a command.
