"""Three real stdio MCP processes sharing one local Discord HTTP simulator."""

import asyncio
from contextlib import AsyncExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from test_messaging import policy, ROOT


class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_processes_share_history_and_enforce_acl(self):
        history = {"111": [], "222": []}
        calls = []
        lock = threading.Lock()

        class Discord(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                channel = parsed.path.split("/")[4]
                params = parse_qs(parsed.query)
                with lock:
                    calls.append(("GET", channel))
                    messages = history[channel][:]
                if "after" in params:
                    messages = [m for m in messages if int(m["id"]) > int(params["after"][0])]
                self.respond(list(reversed(messages)))

            def do_POST(self):
                channel = self.path.split("/")[4]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with lock:
                    calls.append(("POST", channel))
                    message = {"id": str(10 + sum(map(len, history.values()))), "content": body["content"],
                               "author": {"id": "999", "username": "bot", "bot": True},
                               "message_reference": body.get("message_reference", {})}
                    history[channel].append(message)
                self.respond(message)

            def respond(self, body):
                encoded = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        http = ThreadingHTTPServer(("127.0.0.1", 0), Discord)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="raise-hand-test-", dir=ROOT) as directory:
                config = Path(directory) / "channels.json"
                config.write_text(json.dumps(policy()), encoding="utf-8")
                async with AsyncExitStack() as stack:
                    clients = {}
                    for name in ("claude", "codex", "antigravity"):
                        # These are dummy test credentials, never the real .env.
                        session_file = Path(directory) / f"{name}.env"
                        session_file.write_text(f"MCP_SESSION_TOKEN=secret-{name}\n", encoding="utf-8")
                        transport = StdioTransport(
                            command=sys.executable, args=[str(ROOT / "tests" / "stdio_fixture.py")], cwd=str(ROOT),
                            env={"DISCORD_BOT_TOKEN": "test-only-token", "DISCORD_CHANNEL_ID": "222",
                                 "LLAMA_SCRIPTS_PATH": "", "RAISE_HAND_CONFIG": str(config),
                                 "RAISE_HAND_SESSION_FILE": str(session_file),
                                 "TEST_DISCORD_URL": f"http://127.0.0.1:{http.server_port}"},
                        )
                        clients[name] = await stack.enter_async_context(Client(transport, timeout=20, init_timeout=30))
                    for name, client in clients.items():
                        result = await client.call_tool("send_message", {"channel": "public", "message": f"hello from {name}"})
                        self.assertEqual(result.data["session"]["id"], name)
                    for client in clients.values():
                        result = await client.call_tool("read_messages", {"channel": "public"})
                        self.assertEqual(len(result.data["messages"]), 3)
                    private = await clients["claude"].call_tool("send_message", {"channel": "private", "message": "plan"})
                    await clients["codex"].call_tool("send_message", {"channel": "private", "message": "agreed", "reply_to": private.data["message"]["id"]})
                    for name in ("claude", "codex"):
                        result = await clients[name].call_tool("read_messages", {"channel": "private"})
                        self.assertEqual(len(result.data["messages"]), 2)
                    count = len(calls)
                    for tool, arguments in (
                        ("read_messages", {"channel": "private"}),
                        ("send_message", {"channel": "private", "message": "blocked"}),
                    ):
                        result = await clients["antigravity"].call_tool(tool, arguments, raise_on_error=False)
                        self.assertTrue(result.is_error)
                    legacy = await clients["antigravity"].call_tool("raise_hand", {"message": "blocked"})
                    self.assertIn("unavailable", legacy.data)
                    self.assertEqual(len(calls), count)
                    # Change the shared policy while all processes remain running.
                    data = policy()
                    data["channels"]["private"]["allowed_sessions"] = ["claude"]
                    config.write_text(json.dumps(data), encoding="utf-8")
                    denied = await clients["codex"].call_tool("read_messages", {"channel": "private"}, raise_on_error=False)
                    self.assertTrue(denied.is_error)
                    self.assertEqual(len(calls), count)
        finally:
            await asyncio.to_thread(http.shutdown)
            http.server_close()
            thread.join(timeout=5)
