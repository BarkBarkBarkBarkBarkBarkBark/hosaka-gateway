from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from hosaka.paths import resolve_paths
from hosaka_gateway.auth import Auth, configure


def main():
    parser = argparse.ArgumentParser(description="Hosaka's local extension web gateway")
    commands = parser.add_subparsers(dest="command", required=True)
    cfg = commands.add_parser("configure", help="Set gateway password privately; invalidates old sessions")
    cfg.add_argument("--password-stdin", action="store_true", help="Read gateway password from standard input")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8086)
    serve.add_argument("--dev", action="store_true", help="Loopback-only development mode; auto-authorizes local browser sessions")
    serve.add_argument("--allow-lan", action="store_true")
    serve.add_argument("--ssl-keyfile")
    serve.add_argument("--ssl-certfile")
    args = parser.parse_args()
    try:
        auth_path = resolve_paths().root / "gateway" / "auth.json"
        if args.command == "configure":
            if args.password_stdin:
                password = sys.stdin.readline().rstrip("\r\n")
            else:
                password = getpass.getpass("Gateway password (12+ characters): ")
                if password != getpass.getpass("Confirm password: "):
                    raise ValueError("Passwords do not match.")
            configure(auth_path, password)
            print("Gateway configured. Existing browser sessions are invalidated.")
            return
        if not 1024 <= args.port <= 65535:
            raise ValueError("Use an unprivileged port between 1024 and 65535.")
        loopback = args.host in {"127.0.0.1", "::1", "localhost"}
        if args.dev and not loopback:
            raise ValueError("--dev is only permitted when bound to loopback.")
        if not loopback and (
            not args.allow_lan or not args.ssl_keyfile or not args.ssl_certfile
        ):
            raise ValueError("LAN binding requires --allow-lan, --ssl-keyfile and --ssl-certfile.")
        if args.dev and not auth_path.exists():
            configure(auth_path, secrets.token_urlsafe(24))
        import uvicorn
        from hosaka_gateway.server import create_app

        print(f"Hosaka Gateway: {'https' if args.ssl_certfile else 'http'}://{args.host}:{args.port}/", flush=True)
        uvicorn.run(
            create_app(auth=Auth(auth_path), dev_mode=args.dev),
            host=args.host, port=args.port, proxy_headers=False,
            ssl_keyfile=args.ssl_keyfile, ssl_certfile=args.ssl_certfile,
        )
    except (OSError, ValueError) as exc:
        print(f"Gateway failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
