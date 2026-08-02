from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from bidpilot.config import Settings
from bidpilot.db import Database
from bidpilot.source_auth import SourceAuthManager
from bidpilot.sources import CECBidSource


async def run(source_id: str, visible_seconds: float) -> None:
    root = Path("tmp") / "source-auth-smoke"
    settings = Settings(
        data_dir=root / "data",
        database_path=root / "smoke.db",
    )
    sources = [CECBidSource(settings)]
    manager = SourceAuthManager(Database(settings.database_path), settings, sources)
    try:
        session = await manager.start(source_id)
        print(f"{session.status}: {session.source_name}", flush=True)
        await asyncio.sleep(visible_seconds)
    finally:
        await manager.close_all()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="打开并关闭真实可见授权浏览器；不读取或保存 Cookie"
    )
    parser.add_argument("--source", choices=("cecbid",), default="cecbid")
    parser.add_argument("--visible-seconds", type=float, default=3.0)
    args = parser.parse_args()
    asyncio.run(run(args.source, max(0.5, min(args.visible_seconds, 15))))


if __name__ == "__main__":
    main()
