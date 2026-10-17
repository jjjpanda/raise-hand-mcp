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
    async def test_three_processes_share_main_chat_and_focused_threads(self):
        history = {"111": [], "222": []}
        threads = {}
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
                    if len(parsed.path.split("/")) == 5:
                        self.respond(threads[channel])
                        return
                    messages = history[channel][:]
                if "after" in params:
                    messages = [m for m in messages if int(m["id"]) > int(params["after"][0])]
                if "before" in params:
                    messages = [m for m in messages if int(m["id"]) < int(params["before"][0])]
                limit = int(params.get("limit", [50])[0])
                messages = messages[:limit] if "after" in params else messages[-limit:]
                self.respond(list(reversed(messages)))

            def do_POST(self):
                channel = self.path.split("/")[4]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with lock:
                    calls.append(("POST", channel))
                    if self.path.endswith("/threads"):
                        source_id = self.path.split("/")[6]
                        source = next(m for m in history[channel] if m["id"] == source_id)
                        topic = {"id": source_id, "name": body["name"], "parent_id": channel, "type": 11}
                        # Discord may omit the optional thread object from history.
                        source["flags"] = 32
                        threads[source_id] = topic
                        history[source_id] = [{
                            "id": str(10 + sum(map(len, history.values()))),
                            "channel_id": source_id, "type": 21, "content": "",
                            "author": source["author"],
                            "message_reference": {"message_id": source_id, "channel_id": channel},
                            "referenced_message": dict(source),
                        }]
                        self.respond(topic)
                        return
                    message = {"id": str(10 + sum(map(len, history.values()))), "channel_id": channel, "content": body["content"],
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
                        result = await client.call_tool("send_message", {"channel": "main", "message": f"hello from {name}"})
                        self.assertEqual(result.data["session"]["id"], name)
                    for client in clients.values():
                        result = await client.call_tool("read_messages", {"channel": "main"})
                        self.assertEqual(len(result.data["messages"]), 3)
                    discussion = await clients["claude"].call_tool("start_thread", {
                        "channel": "main", "message_id": "10", "name": "Implementation plan"})
                    topic = discussion.data["thread"]["id"]
                    first = await clients["claude"].call_tool("send_message", {
                        "channel": "main", "thread_id": topic, "message": "plan"})
                    await clients["codex"].call_tool("send_message", {
                        "channel": "main", "thread_id": topic, "message": "agreed", "reply_to": first.data["message"]["id"]})
                    for client in clients.values():
                        result = await client.call_tool("read_messages", {"channel": "main", "thread_id": topic})
                        self.assertEqual(len(result.data["messages"]), 3)
                        opener = result.data["messages"][0]
                        self.assertEqual(opener["type"], 21)
                        self.assertEqual(opener["reply_channel_id"], "111")
                        self.assertIn("hello from claude", opener["referenced_message"]["content"])
                    await clients["antigravity"].call_tool("send_message", {
                        "channel": "main", "thread_id": topic, "message": "joining the discussion"})
                    main = await clients["codex"].call_tool("read_messages", {"channel": "main"})
                    self.assertEqual(len(main.data["messages"]), 3)
                    self.assertEqual(main.data["messages"][0]["thread_id"], topic)
                    # Revoking a credential still applies to all running processes.
                    data = policy()
                    del data["sessions"]["codex"]
                    config.write_text(json.dumps(data), encoding="utf-8")
                    count = len(calls)
                    denied = await clients["codex"].call_tool("read_messages", {
                        "channel": "main", "thread_id": topic}, raise_on_error=False)
                    self.assertTrue(denied.is_error)
                    self.assertEqual(len(calls), count)
        finally:
            await asyncio.to_thread(http.shutdown)
            http.server_close()
            thread.join(timeout=5)
