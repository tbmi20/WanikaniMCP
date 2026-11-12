"""Command line: ``wanikani-mcp [serve|sync|stats]``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .client import WaniKaniClient, WaniKaniError
from .db import Database, default_path
from .leeches import LeechConfig
from .metrics import format_stats, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wanikani-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server over stdio (default)")
    sub.add_parser("sync", help="sync WaniKani data into the local cache and print what changed")
    stats_cmd = sub.add_parser("stats", help="print usage evidence and progress since the first sync")
    stats_cmd.add_argument("--json", action="store_true", help="print raw JSON")
    args = parser.parse_args(argv)

    if args.command in (None, "serve"):
        from .server import create_server

        create_server().run()
        return 0

    db = Database()
    try:
        if args.command == "sync":
            from .sync import sync

            async def run() -> dict:
                async with WaniKaniClient() as client:
                    return await sync(client, db, LeechConfig.from_env())

            try:
                summary = asyncio.run(run())
            except WaniKaniError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        elif args.command == "stats":
            data = stats(db)
            print(json.dumps(data, indent=2) if args.json else format_stats(data))
            print(f"\n(cache: {default_path()})" if not args.json else "", file=sys.stderr)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
