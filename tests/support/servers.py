"""Run an ASGI app on a real socket (for multi-process tests)."""

from __future__ import annotations

import socket
import threading
import time

import uvicorn


class LiveServer:
    def __init__(self, asgi_app):
        self.app = asgi_app
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(asgi_app, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            assert time.time() < deadline, "live server failed to start"
            time.sleep(0.02)
        return self

    def __exit__(self, exc_type, *_):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        # The thread is a daemon, so it can't block interpreter exit. Report a failed
        # shutdown, but don't mask an exception that's already propagating.
        if self.thread.is_alive() and exc_type is None:
            raise AssertionError(f"live server {self.url} did not shut down within 10s")
