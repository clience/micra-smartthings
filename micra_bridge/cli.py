"""Interactive credentials stay on the user's machine, outside source control."""

import argparse
import asyncio
import getpass
import json
import logging
import secrets
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

from .backend import BridgeError, connect
from .config import load_config, save_private
from .server import create_app


def configure(path: Path):
    old = load_config(path) if path.exists() else {}
    username = input("La Marzocco 이메일: ").strip()
    password = getpass.getpass("La Marzocco 비밀번호 (화면에 표시되지 않음): ")
    serial = input("Micra 시리얼 번호 (한 대면 Enter): ").strip()
    if not username or not password:
        raise ValueError("Email and password are required")
    save_private(path, {
        "username": username, "password": password, "serial": serial,
        "bridge_token": old.get("bridge_token") or secrets.token_urlsafe(32),
    })
    print(f"저장 완료: {path}. 비밀번호·토큰은 이 파일에 로컬 저장됩니다 (권한 600).")


async def run(args):
    config = load_config(args.config)
    async with ClientSession(timeout=ClientTimeout(total=20)) as session:
        async with asyncio.timeout(60):
            backend = await connect(config, args.config.parent / "installation-key.json", session)
        if args.command == "diagnose":
            print(json.dumps(await backend.status(), indent=2, ensure_ascii=False))
            return
        app = create_app(backend, config["bridge_token"], args.enable_control)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, args.host, args.port).start()
            backend.start()
            mode = "전원 제어 허용" if args.enable_control else "조회 전용"
            print(f"Micra bridge: {args.host}:{args.port} ({mode})", flush=True)
            await asyncio.Event().wait()
        finally:
            await backend.close()
            await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description="Linea Micra → SmartThings Edge bridge")
    parser.add_argument("--config", type=Path, default=Path(".local/config.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("configure", help="로컬 터미널에서 계정 정보 입력")
    sub.add_parser("diagnose", help="로그인·Micra 상태 조회, 제어 명령 없음")
    sub.add_parser("show-token", help="Edge 설정용 토큰을 본인 터미널에 표시")
    serve = sub.add_parser("serve", help="중계 서버 실행")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    serve.add_argument("--enable-control", action="store_true")
    args = parser.parse_args()
    # Upstream exceptions/logs can include account API responses. Our output is allowlisted.
    logging.getLogger("pylamarzocco").disabled = True
    logging.getLogger("pylamarzocco").addHandler(logging.NullHandler())
    logging.getLogger("pylamarzocco").propagate = False
    try:
        if args.command == "configure":
            configure(args.config)
        elif args.command == "show-token":
            print(load_config(args.config)["bridge_token"])
        else:
            asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except BridgeError as exc:
        parser.exit(1, f"진단 실패: {exc}\n")
    except Exception as exc:
        parser.exit(1, f"실패 ({type(exc).__name__}). 설정 파일·계정 로그인·네트워크를 확인하세요.\n")


if __name__ == "__main__":
    main()
