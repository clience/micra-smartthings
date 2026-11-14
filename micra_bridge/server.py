"""Small authenticated LAN API, with control disabled unless explicitly enabled."""

import hmac
import re

from aiohttp import web

from .backend import BridgeError
from .settings import InvalidSetting


def create_app(backend, token: str, enable_control: bool = False) -> web.Application:
    if len(token) < 32 or not token.isascii():
        raise ValueError("A random ASCII token of at least 32 characters is required")

    @web.middleware
    async def authenticate(request, handler):
        supplied = request.headers.get("Authorization", "").encode("utf-8")
        expected = ("Bearer " + token).encode("ascii")
        if not hmac.compare_digest(supplied, expected):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            response = await handler(request)
        except BridgeError as exc:
            response = web.json_response({"error": str(exc)}, status=503)
        except InvalidSetting as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except web.HTTPException:
            raise
        except Exception:
            response = web.json_response({"error": "internal_error"}, status=500)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def status(request):
        result = await backend.status()
        return web.json_response({**result, "control_enabled": enable_control})

    async def power(request):
        if not enable_control:
            return web.json_response({"error": "read_only"}, status=403)
        if request.content_type != "application/json":
            return web.json_response({"error": "expected_json"}, status=415)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(body, dict) or set(body) != {"on"} or type(body["on"]) is not bool:
            return web.json_response({"error": "expected_boolean_on"}, status=400)
        await backend.set_power(body["on"])
        return web.json_response({"accepted": True, "confirmed": False}, status=202)

    async def events(request):
        after = request.query.get("after", "")
        if len(after) > 64 or (after and not re.fullmatch(r"[a-f0-9]+-[0-9]+", after)):
            return web.json_response({"error": "invalid_cursor"}, status=400)
        result = await backend.events(after)
        return web.json_response({**result, "control_enabled": enable_control})

    async def setting(request):
        if not enable_control:
            return web.json_response({"error": "read_only"}, status=403)
        if request.content_type != "application/json":
            return web.json_response({"error": "expected_json"}, status=415)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(body, dict) or set(body) != {"setting", "value"}:
            return web.json_response({"error": "expected_setting_and_value"}, status=400)
        await backend.set_setting(body['setting'], body['value'])
        return web.json_response({"accepted": True, "confirmed": False}, status=202)

    app = web.Application(middlewares=[authenticate], client_max_size=1024)
    app.router.add_get("/v1/status", status)
    app.router.add_get("/v1/events", events)
    app.router.add_post("/v1/power", power)
    app.router.add_post("/v1/settings", setting)
    return app
