"""Two isolated HTTP listeners sharing one runtime and one shutdown lifecycle."""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
from contextlib import ExitStack, contextmanager
from typing import Iterator

import uvicorn
from fastapi import FastAPI

logger = logging.getLogger("plex_proxy")


class ListenerServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The coordinator shuts down both listeners together.
        yield


async def serve(app: FastAPI, proxy_port: int, admin_port: int) -> None:
    if proxy_port == admin_port:
        raise ValueError("Admin and proxy ports must be different")
    with ExitStack() as stack:
        sockets = []
        for port in (proxy_port, admin_port):
            sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.listen(128)
            sock.setblocking(False)
            sockets.append(sock)
        servers = [
            ListenerServer(uvicorn.Config(
                target, lifespan="off", proxy_headers=False,
                server_header=False, date_header=False,
            ))
            for target in (app, app.state.admin_app)
        ]
        loop = asyncio.get_running_loop()

        def shutdown() -> None:
            for server in servers:
                server.should_exit = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown)
        try:
            async with app.router.lifespan_context(app):
                tasks = [asyncio.create_task(server.serve(sockets=[sock])) for server, sock in zip(servers, sockets)]
                try:
                    while not all(server.started for server in servers):
                        if any(task.done() for task in tasks):
                            shutdown()
                            await asyncio.gather(*tasks)
                            raise RuntimeError("HTTP listeners failed to start")
                        await asyncio.sleep(0.05)
                    logger.info("Proxy listener ready on port %s; admin listener ready on port %s", proxy_port, admin_port)
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    shutdown()
                    await asyncio.gather(*tasks)
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
