"""Command line entry point.

    colabconnect serve        # web UI + MCP (http://127.0.0.1:8420, MCP at /mcp)
    colabconnect mcp-stdio    # stdio MCP proxy to a running `serve`, for stdio-only clients
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import webbrowser

from . import config


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .api import create_app

    config.PORT = args.port
    with socket.socket() as probe:
        if probe.connect_ex((config.HOST, config.PORT)) == 0:
            raise SystemExit(
                f"Le port {config.PORT} est déjà utilisé par une autre application. "
                "Choisissez-en un autre avec --port (ou COLABCONNECT_PORT)."
            )
    url = f"http://{config.HOST}:{config.PORT}"
    print(f"ColabConnect  ->  {url}")
    print(f"MCP endpoint  ->  {url}/mcp")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    from .updater import RESTART_EXIT_CODE

    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host=config.HOST, port=config.PORT, log_level=args.log_level))
    restart_requested = False

    def request_restart() -> None:
        nonlocal restart_requested
        restart_requested = True
        server.should_exit = True

    # Used by the self-updater; the launchers relaunch on this exit code.
    app.api.state.request_restart = request_restart
    server.run()
    if restart_requested:
        print("Redémarrage après mise à jour…")
        sys.exit(RESTART_EXIT_CODE)


def _mcp_stdio(args: argparse.Namespace) -> None:
    from fastmcp.server import create_proxy

    proxy = create_proxy(args.url, name="colabconnect")
    proxy.run()  # stdio transport


def main() -> None:
    parser = argparse.ArgumentParser(prog="colabconnect")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Lance l'interface web et le serveur MCP")
    serve.add_argument("--port", type=int, default=config.PORT)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=_serve)

    stdio = sub.add_parser("mcp-stdio", help="Proxy MCP stdio vers un serveur ColabConnect lancé")
    stdio.add_argument("--url", default=f"http://{config.HOST}:{config.PORT}/mcp")
    stdio.set_defaults(func=_mcp_stdio)

    args = parser.parse_args()
    if not args.command:
        args = parser.parse_args(["serve"])
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args.func(args)
