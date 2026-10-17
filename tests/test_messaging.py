import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from messaging import AccessError, Registry, load_registry
from register_session import register

# Unit tests must never load real credentials or start model discovery.
with patch("dotenv.load_dotenv"), patch.dict(os.environ, {
    "LLAMA_SCRIPTS_PATH": "", "RAISE_HAND_SESSION_FILE": "", "MCP_SESSION_TOKEN": "",
}):
    import server


ROOT = Path(__file__).resolve().parents[1]


def policy():
    return {
        "sessions": {s: {"name": s.title(), "token_sha256": hashlib.sha256(f"secret-{s}".encode()).hexdigest()}
                     for s in ("claude", "codex", "antigravity")},
        "channels": {
            "public": {"id": "111", "visibility": "public"},
            "private": {"id": "222", "visibility": "private", "allowed_sessions": ["claude", "codex"]},
        },
    }


class MessagingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="raise-hand-test-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "channels.json"
        self.config.write_text(json.dumps(policy()), encoding="utf-8")
        self.requests = []
        self.history = {"111": [], "222": []}
        self.serial = 9
        real_client = httpx.AsyncClient
        self.client_patch = patch.object(server.httpx, "AsyncClient", side_effect=lambda **kw: real_client(
            transport=httpx.MockTransport(self.discord), **kw))
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.env_patch = patch.multiple(server, DISCORD_BOT_TOKEN="never-return-this-token", DISCORD_CHANNEL_ID="111",
                                        MESSAGING_ENABLED=True, REGISTRY=Registry(self.config, "secret-claude"))
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def discord(self, request):
        self.requests.append(request)
        parts = request.url.path.split("/")
        channel = parts[4]
        if request.method == "POST":
            self.serial += 1
            body = json.loads(request.content)
            reference = body.get("message_reference", {})
            if reference and not any(m["id"] == reference["message_id"] for m in self.history[channel]):
                return httpx.Response(404, json={"message": "Unknown message"})
            msg = {"id": str(self.serial), "content": body["content"], "author": {"id": "999", "username": "bot", "bot": True},
                   "message_reference": reference}
            self.history[channel].append(msg)
            return httpx.Response(200, json=msg)
        if request.method == "GET":
            messages = self.history[channel]
            after, before = request.url.params.get("after"), request.url.params.get("before")
            if after:
                messages = [m for m in messages if int(m["id"]) > int(after)]
            if before:
                messages = [m for m in messages if int(m["id"]) < int(before)]
            messages = sorted(messages, key=lambda m: int(m["id"]))
            limit = int(request.url.params.get("limit", 50))
            messages = messages[:limit] if after else messages[-limit:]
            return httpx.Response(200, json=list(reversed(messages)))
        raise AssertionError(request.method)

    def session(self, name):
        return patch.object(server, "REGISTRY", Registry(self.config, f"secret-{name}"))

    async def test_all_sessions_exchange_public_and_identity_is_bound(self):
        for name in ("claude", "codex", "antigravity"):
            with self.session(name):
                result = await server.send_message("public", "hello")
                self.assertEqual(result["session"]["id"], name)
                self.assertIn(f"| {name}]", result["message"]["content"])
        for name in ("claude", "codex", "antigravity"):
            with self.session(name):
                result = await server.read_messages("public")
                self.assertEqual(len(result["messages"]), 3)
        self.assertTrue(all(r.url.path.startswith("/api/v10/channels/111/messages") for r in self.requests))

    async def test_private_exchange_reply_and_excluded_session_denied_before_http(self):
        sent = await server.send_message("private", "plan")
        with self.session("codex"):
            reply = await server.send_message("private", "agreed", sent["message"]["id"])
            self.assertEqual(reply["message"]["reply_to"], sent["message"]["id"])
            self.assertEqual(len((await server.read_messages("private"))["messages"]), 2)
        count = len(self.requests)
        with self.session("antigravity"):
            for call in (server.read_messages("private"), server.send_message("private", "intrusion"),
                         server.read_messages("222"), server.send_message("unknown", "intrusion")):
                with self.assertRaises(ToolError):
                    await call
        self.assertEqual(len(self.requests), count)
        body = json.loads(self.requests[1].content)
        self.assertEqual(body["message_reference"]["channel_id"], "222")
        self.assertEqual(body["allowed_mentions"], {"parse": [], "replied_user": False})

    async def test_independent_reads_pagination_and_human_messages(self):
        self.history["111"].append({"id": "9", "content": "human note", "author": {"id": "123", "username": "human"},
                                    "referenced_message": {"content": "never leak embedded private data"}})
        await server.send_message("public", "agent note")
        await server.send_message("public", "second note")
        first = await server.read_messages("public", after="0", limit=2)
        self.assertEqual([m["id"] for m in first["messages"]], ["9", "10"])
        self.assertFalse(first["messages"][0]["author"]["bot"])
        self.assertNotIn("referenced_message", first["messages"][0])
        with self.session("codex"):
            self.assertEqual((await server.read_messages("public", after="0", limit=2))["messages"], first["messages"])
        self.assertEqual([m["id"] for m in (await server.read_messages("public", after=first["next_after"]))["messages"]], ["11"])
        self.assertEqual((await server.read_messages("public", after="11"))["next_after"], "11")
        latest = await server.read_messages("public", limit=1)
        self.assertEqual(latest["next_before"], "11")
        self.assertEqual(len((await server.read_messages("public", before="11"))["messages"]), 2)

    async def test_validation_and_cross_channel_reply(self):
        for args in ({"limit": 0}, {"after": "../222"}, {"after": "1", "before": "2"}):
            with self.assertRaises(ToolError):
                await server.read_messages("public", **args)
        for message in ("", " ", "x" * 2000, "😀" * 1000):
            with self.assertRaises(ToolError):
                await server.send_message("public", message)
        self.assertEqual(self.requests, [])
        private = await server.send_message("private", "private note")
        with self.assertRaises(ToolError):
            await server.send_message("public", "reply", private["message"]["id"])

    async def test_empty_initial_poll_preserves_the_first_burst(self):
        first = await server.read_messages("public", limit=2)
        self.assertEqual(first["next_after"], "0")
        for text in ("first", "second", "third"):
            await server.send_message("public", text)
        page = await server.read_messages("public", after=first["next_after"], limit=2)
        last = await server.read_messages("public", after=page["next_after"], limit=2)
        self.assertEqual([m["id"] for m in page["messages"] + last["messages"]], ["10", "11", "12"])

    async def test_revocation_unknown_session_and_malformed_config_fail_closed(self):
        data = policy()
        data["channels"]["private"]["allowed_sessions"] = []
        self.config.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ToolError):
            await server.read_messages("private")
        with self.session("unregistered"), self.assertRaises(ToolError):
            await server.read_messages("public")
        for body in ("{}", "invalid JSON"):
            self.config.write_text(body, encoding="utf-8")
            with self.assertRaises(ToolError):
                await server.send_message("public", "hello")
        self.config.unlink()
        with self.assertRaises(ToolError):
            await server.read_messages("public")
        self.assertEqual(self.requests, [])

    async def test_ambiguous_policies_are_rejected(self):
        for mutate in (
            lambda d: d["channels"].update(alias={"id": "222", "visibility": "public"}),
            lambda d: d["sessions"]["codex"].update(token_sha256=d["sessions"]["claude"]["token_sha256"]),
            lambda d: d["channels"]["private"].update(allowed_sessions="claude"),
            lambda d: d["channels"]["private"].update(visibility="typo"),
            lambda d: d["channels"].update(alias={"id": "0222", "visibility": "public"}),
            lambda d: d["channels"]["public"].update(id="0"),
            lambda d: d["channels"]["public"].update(id="18446744073709551616"),
        ):
            data = policy()
            mutate(data)
            self.config.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(AccessError):
                load_registry(self.config)

    async def test_duplicate_policy_keys_fail_closed(self):
        body = json.dumps(policy())
        for malformed in (
            body.replace('"visibility": "private"', '"visibility": "private", "visibility": "public"'),
            body[:-1] + ', "channels": {"private": {"id": "222", "visibility": "public"}}}',
        ):
            self.config.write_text(malformed, encoding="utf-8")
            with self.session("antigravity"), self.assertRaises(ToolError):
                await server.read_messages("private")
        self.assertEqual(self.requests, [])

    async def test_errors_do_not_echo_credentials(self):
        failure = httpx.ConnectError("never-return-this-token secret-claude")
        with patch.object(server.httpx, "AsyncClient") as client:
            client.return_value.__aenter__.return_value.post = AsyncMock(side_effect=failure)
            client.return_value.__aenter__.return_value.get = AsyncMock(side_effect=failure)
            for call in (server.send_message("public", "hello"), server.read_messages("public")):
                with self.assertRaises(ToolError) as error:
                    await call
                self.assertNotIn("never-return-this-token", str(error.exception))
                self.assertNotIn("secret-claude", str(error.exception))
            self.assertNotIn("never-return-this-token", await server.raise_hand("help"))

    async def test_raise_hand_still_waits_for_human_reply(self):
        reply = {"id": "99", "author": {"username": "operator"}, "content": "try again", "message_reference": {"message_id": "10"}}
        with patch.object(server.asyncio, "sleep", new=AsyncMock()), \
                patch.object(server, "get_new_messages", new=AsyncMock(return_value=[reply])), \
                patch.object(server, "add_reaction", new=AsyncMock()):
            self.assertEqual(await server.raise_hand("help"), "Human (operator) replied: try again")
        with patch.object(server, "DISCORD_CHANNEL_ID", "222"), self.session("antigravity"):
            count = len(self.requests)
            self.assertIn("unavailable", await server.raise_hand("help"))
            self.assertEqual(len(self.requests), count)

    async def test_raise_hand_legacy_mode_and_tiny_llm(self):
        with patch.object(server, "MESSAGING_ENABLED", False), patch.object(server, "CONFIG_PATH", self.config.with_name("missing.json")), \
                patch.object(server.asyncio, "sleep", new=AsyncMock()), \
                patch.object(server, "get_new_messages", new=AsyncMock(return_value=[])), \
                patch.object(server, "get_message", new=AsyncMock(return_value={"reactions": [{"emoji": {"name": "👍"}, "count": 1}]})), \
                patch.object(server, "add_reaction", new=AsyncMock()):
            self.assertEqual(await server.raise_hand("help"), "Human reacted with: 👍")
        response = httpx.Response(200, json={"choices": [{"message": {"content": "local reply"}}]}, request=httpx.Request("POST", "http://127.0.0.1"))
        with patch.object(server, "_MODEL_PORTS", {"small": "11400"}), \
                patch.object(server.httpx, "AsyncClient") as client:
            client.return_value.__aenter__.return_value.post = AsyncMock(return_value=response)
            self.assertEqual(await server.tiny_llm_task("rewrite"), "local reply")

    async def test_raise_hand_scans_past_a_busy_page(self):
        self.history["111"] = [
            {"id": str(i), "content": "unrelated", "author": {"bot": False}}
            for i in range(11, 116)
        ] + [{"id": "116", "content": "answer", "author": {"username": "operator"},
              "message_reference": {"message_id": "10"}}]
        with patch.object(server.asyncio, "sleep", new=AsyncMock()), \
                patch.object(server, "get_message", new=AsyncMock(return_value={"reactions": []})), \
                patch.object(server, "add_reaction", new=AsyncMock()):
            self.assertEqual(await server.raise_hand("help"), "Human (operator) replied: answer")
        reads = [r for r in self.requests if r.method == "GET"]
        self.assertEqual([r.url.params["after"] for r in reads], ["10", "110"])

    async def test_policy_created_after_startup_prevents_legacy_bypass_and_stays_required(self):
        with patch.object(server, "MESSAGING_ENABLED", False), \
                patch.object(server, "CONFIG_PATH", self.config), \
                patch.object(server, "DISCORD_CHANNEL_ID", "222"), self.session("antigravity"):
            self.assertIn("unavailable", await server.raise_hand("blocked"))
            self.config.unlink()
            self.assertIn("configuration", await server.raise_hand("still blocked"))
        self.assertEqual(self.requests, [])

    async def test_mcp_schema_has_no_caller_controlled_identity(self):
        async with Client(server.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            self.assertEqual(set(tools), {"send_message", "read_messages", "raise_hand", "tiny_llm_task"})
            self.assertEqual(set(tools["send_message"].inputSchema["properties"]), {"channel", "message", "reply_to"})
            result = await client.call_tool("send_message", {"channel": "public", "message": "hello"})
            self.assertFalse(result.is_error)

    async def test_registration_creates_unique_credentials_without_storing_raw_token_in_registry(self):
        first, first_path = register("Planner", self.config)
        second, second_path = register("Planner", self.config)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first_path.read_text(), second_path.read_text())
        token = first_path.read_text().strip().split("=", 1)[1]
        self.assertNotIn(token, self.config.read_text())
        self.assertEqual(Registry(self.config, token).authorize("public")[0]["id"], first)

    async def test_failed_registration_removes_partial_files(self):
        original = self.config.read_text()
        with patch.object(Path, "replace", side_effect=PermissionError("test-only failure")):
            with self.assertRaises(OSError):
                register("Planner", self.config)
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(list(self.config.parent.glob(".channels.json.*.tmp")), [])
        self.assertEqual(list((self.config.parent / ".sessions").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
