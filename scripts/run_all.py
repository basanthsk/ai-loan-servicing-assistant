"""Start the mock systems (and, once built, the gateway) on their own ports.

Run: poetry run poe up              # everything
     poetry run poe up --services   # mock systems only

Each app gets its own uvicorn server and port inside one Python process, so
calls between them are real HTTP requests. Works the same on Windows, macOS
and Linux. Stop with Ctrl+C.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from core.settings import get_settings  # noqa: E402

SYSTEMS = ["crm", "loans", "payments", "policy", "serviceops", "cards"]


def servers(include_gateway: bool) -> list[uvicorn.Server]:
    s = get_settings()
    out = []
    for name in SYSTEMS:
        try:
            module = importlib.import_module(f"services.{name}")
        except ModuleNotFoundError:
            continue  # system not built yet
        port = urlparse(s.system_url(name)).port
        out.append(
            uvicorn.Server(
                uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="warning")
            )
        )
        print(f"  {name:11} http://127.0.0.1:{port}")
    if include_gateway:
        try:
            gateway = importlib.import_module("gateway.app")
            out.append(
                uvicorn.Server(
                    uvicorn.Config(
                        gateway.app, host="127.0.0.1", port=s.gateway_port, log_level="info"
                    )
                )
            )
            print(
                f"  {'gateway':11} http://127.0.0.1:{s.gateway_port}  <- open this in your browser"
            )
        except ModuleNotFoundError:
            pass
    return out


async def main_async(include_gateway: bool) -> None:
    if not (ROOT / get_settings().db_path).exists():
        print("No database yet; run `poetry run poe seed` first.")
        raise SystemExit(1)
    print("Starting:")
    group = servers(include_gateway)
    await asyncio.gather(*(srv.serve() for srv in group))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--services", action="store_true", help="start only the mock systems")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(include_gateway=not args.services))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
