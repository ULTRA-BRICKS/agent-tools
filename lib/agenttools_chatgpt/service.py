"""Loopback authenticated control API; handlers serialize durable controls in threads.

Reached by the CLI service. No prompts, cookies, screenshots or tokens leave this
process; clients receive state metadata only. Enabling apply requires CLI startup.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import Store
from .urls import canonical_chat


class BoundedServer(ThreadingHTTPServer):
    connection_timeout = 5
    max_handlers = 16
    daemon_threads = True

    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(self.max_handlers)
        super().__init__(*args)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self.connection_timeout)
        return request, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class ControlServer:
    def __init__(self, store: Store, token: str, port: int = 8790):
        if len(token) < 32:
            raise ValueError("Control token must have at least 32 characters")
        self.store, self.token = store, token
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authorized(self):
                supplied = self.headers.get("Authorization", "")
                if not hmac.compare_digest(
                    supplied.encode(), ("Bearer " + owner.token).encode()
                ):
                    self.reply(401, {"error": "Authentication required"})
                    return False
                return True

            def do_GET(self):
                if self.authorized():
                    self.reply(
                        200, owner.store.status()
                    ) if self.path == "/status" else self.reply(
                        404, {"error": "Unknown route"}
                    )

            def do_POST(self):
                if not self.authorized():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("Expected bounded JSON body")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise TypeError("Expected JSON object")
                    chat = body.get("chat", "*")
                    if not isinstance(chat, str) or not chat:
                        raise ValueError("chat must be a nonempty string")
                    if chat != "*":
                        chat = canonical_chat(chat)
                    if self.path == "/stop":
                        owner.store.stop(chat)
                    elif self.path == "/resume":
                        owner.store.resume(chat)
                    elif self.path == "/sol-opt-in":
                        owner.store.opt_in_sol(chat)
                    elif self.path == "/sol-opt-out":
                        owner.store.opt_in_sol(chat, False)
                    elif self.path == "/quota-clear":
                        owner.store.clear_quota()
                    elif self.path in ("/watch", "/unwatch"):
                        if not chat.startswith("https://chatgpt.com/c/") or chat == "*":
                            raise ValueError(
                                "Watch requires canonical permanent chat URL"
                            )
                        owner.store.set_control(
                            "watch:" + chat, "1" if self.path == "/watch" else "0"
                        )
                    else:
                        self.reply(404, {"error": "Unknown route"})
                        return
                    self.reply(200, {"ok": True})
                except (ValueError, TypeError):
                    self.reply(400, {"error": "Invalid control request"})
                except (sqlite3.Error, OSError):
                    self.reply(
                        503,
                        {
                            "error": "Control persistence unavailable; action not confirmed"
                        },
                    )

        self.server = BoundedServer(("127.0.0.1", port), Handler)
        self.server.timeout = 5
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()
