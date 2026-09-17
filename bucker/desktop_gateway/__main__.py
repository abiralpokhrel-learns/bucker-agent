"""Loopback-only child process. stdout is JSON-lines lifecycle protocol."""

import argparse
import json
import os
import socket

from . import create_app


def main():
    parser = argparse.ArgumentParser(description="Isolated Bucker desktop gateway")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    try:
        if not 0 <= args.port <= 65535:
            raise ValueError("invalid port")
        app = create_app(
            token=os.environ.get("BUCKER_DESKTOP_TOKEN", ""),
            connections=json.loads(os.environ.get("BUCKER_DESKTOP_CONNECTIONS", "")),
        )
    except (ValueError, TypeError):
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {
                        "type": "configuration_error",
                        "message": "invalid desktop gateway configuration",
                    },
                }
            ),
            flush=True,
        )
        return 2

    import uvicorn

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                print(
                    json.dumps(
                        {
                            "status": "ready",
                            "host": "127.0.0.1",
                            "port": sock.getsockname()[1],
                            "pid": os.getpid(),
                        }
                    ),
                    flush=True,
                )

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # No --host option; remote binding is deliberately impossible.
        sock.bind(("127.0.0.1", args.port))
        server = ReadyServer(
            uvicorn.Config(
                app, host="127.0.0.1", port=args.port, log_level="critical", access_log=False
            )
        )
        server.run(sockets=[sock])
    except OSError:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {
                        "type": "bind_error",
                        "message": "could not bind loopback gateway",
                    },
                }
            ),
            flush=True,
        )
        return 2
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
