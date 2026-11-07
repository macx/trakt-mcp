"""Entry point: ``trakt-mcp [serve|set-passphrase]`` or ``python -m trakt_mcp``.

* ``serve`` (default) runs the HTTP server.
* ``set-passphrase`` sets the owner passphrase that every sign-in asks for
  (prompted twice, or read from stdin with ``--stdin``).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

import uvicorn
from pydantic import ValidationError

from trakt_mcp.auth.passphrase import hash_passphrase
from trakt_mcp.auth.provider import PASSPHRASE_SETTING
from trakt_mcp.config import Settings
from trakt_mcp.server import build_app
from trakt_mcp.storage import Store


def _settings() -> Settings:
    try:
        return Settings()  # values come from the environment
    except ValidationError as exc:
        # Print field names and problems only - never the (possibly secret) values.
        problems = "\n".join(
            f"  {'.'.join(map(str, err['loc'])).upper()}: {err['msg']}" for err in exc.errors()
        )
        sys.exit(f"trakt-mcp: invalid configuration\n{problems}")


def _serve(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = build_app(settings)
    uvicorn.run(
        app.asgi,
        host=settings.host,
        port=settings.port,
        # URLs are built from PUBLIC_URL, never from request headers, so
        # X-Forwarded-* from the reverse proxy is not needed and is ignored.
        proxy_headers=False,
        server_header=False,
        # Access logs would record request IDs and OAuth codes from query strings.
        access_log=False,
        log_level=settings.log_level.lower(),
    )


async def _store_passphrase(settings: Settings, passphrase: str) -> None:
    store = Store(settings.database_path)
    store.open()
    try:
        await store.set_setting(PASSPHRASE_SETTING, hash_passphrase(passphrase))
    finally:
        store.close()


def _read_passphrase(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("New passphrase: ")
    if getpass.getpass("Repeat passphrase: ") != first:
        sys.exit("trakt-mcp: the passphrases do not match")
    return first


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="trakt-mcp", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the server (default)")
    set_pass = commands.add_parser("set-passphrase", help="set the owner passphrase")
    set_pass.add_argument("--stdin", action="store_true", help="read it from stdin")
    args = parser.parse_args(argv)

    settings = _settings()
    if args.command == "set-passphrase":
        passphrase = _read_passphrase(args.stdin)
        try:
            asyncio.run(_store_passphrase(settings, passphrase))
        except ValueError as exc:
            sys.exit(f"trakt-mcp: {exc}")
        print("Passphrase saved. It is required for every new sign-in.")
    else:
        _serve(settings)


if __name__ == "__main__":
    main()
