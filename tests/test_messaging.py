import hashlib
import io
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
from register_session import main as register_main, register

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
            "main": {"id": "111"},
            "other": {"id": "222"},
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
        self.threads = {}
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
            body = json.loads(request.content)
            if parts[-1] == "threads":
                source = next((m for m in self.history[channel] if m["id"] == parts[6]), None)
                if source is None:
                    return httpx.Response(404)
                if source["id"] in self.threads:
                    return httpx.Response(400)
                thread = {"id": source["id"], "name": body["name"], "type": 11, "parent_id": channel}
                self.threads[thread["id"]] = thread
                source["thread"] = thread
                source["flags"] = 32
                self.serial += 1
                self.history[thread["id"]] = [{
                    "id": str(self.serial), "channel_id": thread["id"], "type": 21,
                    "content": "", "author": source["author"],
                    "message_reference": {"message_id": source["id"], "channel_id": channel},
                    "referenced_message": dict(source),
                }]
                return httpx.Response(201, json=thread)
            self.serial += 1
            reference = body.get("message_reference", {})
            if reference and not any(m["id"] == reference["message_id"] for m in self.history[channel]):
                return httpx.Response(404, json={"message": "Unknown message"})
            msg = {"id": str(self.serial), "channel_id": channel, "content": body["content"], "author": {"id": "999", "username": "bot", "bot": True},
                   "message_reference": reference}
            self.history[channel].append(msg)
            return httpx.Response(200, json=msg)
        if request.method == "GET":
            if len(parts) == 5:
                if channel in self.threads:
                    return httpx.Response(200, json=self.threads[channel])
                return httpx.Response(404)
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
                result = await server.send_message("main", "hello")
                self.assertEqual(result["session"]["id"], name)
                self.assertIn(f"| {name}]", result["message"]["content"])
        for name in ("claude", "codex", "antigravity"):
            with self.session(name):
                result = await server.read_messages("main")
                self.assertEqual(len(result["messages"]), 3)
        self.assertTrue(all(r.url.path.startswith("/api/v10/channels/111/messages") for r in self.requests))

    async def test_focused_thread_is_readable_by_every_session_and_separate_from_main(self):
        sent = await server.send_message("main", "Codex and Claude: discuss the implementation here")
        thread = await server.start_thread("main", sent["message"]["id"], "Implementation plan")
        thread_id = thread["thread"]["id"]
        first = await server.send_message("main", "plan", thread_id=thread_id)
        with self.session("codex"):
            reply = await server.send_message("main", "agreed", first["message"]["id"], thread_id=thread_id)
            self.assertEqual(reply["message"]["reply_to"], first["message"]["id"])
        with self.session("antigravity"):
            history = await server.read_messages("main", thread_id=thread_id)
            self.assertEqual(len(history["messages"]), 3)
            opener = history["messages"][0]
            self.assertEqual(opener["type"], 21)
            self.assertEqual(opener["channel_id"], thread_id)
            self.assertEqual(opener["reply_channel_id"], "111")
            self.assertEqual(opener["referenced_message"]["content"], sent["message"]["content"])
            self.assertEqual(history["thread_id"], thread_id)
            await server.send_message("main", "I can join too", thread_id=thread_id)
        main = await server.read_messages("main")
        self.assertEqual(len(main["messages"]), 1)
        self.assertEqual(main["messages"][0]["thread_id"], thread_id)
        self.assertEqual(len((await server.read_messages("main", thread_id=thread_id))["messages"]), 4)
        bodies = [json.loads(r.content) for r in self.requests if r.method == "POST" and r.url.path.endswith("/messages")]
        self.assertEqual(bodies[2]["message_reference"]["channel_id"], thread_id)
        self.assertEqual(bodies[2]["allowed_mentions"], {"parse": [], "replied_user": False})

    async def test_attached_thread_id_is_discovered_without_optional_thread_object(self):
        for flags in (32, 32 | 4096):
            self.history["111"] = [{"id": "10", "content": "task", "flags": flags}]
            result = await server.read_messages("main")
            self.assertEqual(result["messages"][0]["thread_id"], "10")
        self.history["111"] = [{"id": "11", "type": 18,
                                "message_reference": {"channel_id": "10"}}]
        self.assertEqual((await server.read_messages("main"))["messages"][0]["thread_id"], "10")

    async def test_thread_starter_context_is_limited_to_the_validated_parent(self):
        sent = await server.send_message("main", "original task")
        topic = (await server.start_thread("main", sent["message"]["id"], "Plan"))["thread"]["id"]
        opener = self.history[topic][0]
        for reference_channel, source_channel, source_id in (("222", "222", topic),
                                                            ("111", "222", topic),
                                                            ("111", "111", "999")):
            opener["message_reference"]["channel_id"] = reference_channel
            opener["referenced_message"].update(channel_id=source_channel, id=source_id)
            result = await server.read_messages("main", thread_id=topic)
            self.assertNotIn("referenced_message", result["messages"][0])
        opener["message_reference"]["channel_id"] = "111"
        opener["referenced_message"] = None
        result = await server.read_messages("main", thread_id=topic)
        self.assertIsNone(result["messages"][0]["referenced_message"])
        del opener["referenced_message"]
        result = await server.read_messages("main", thread_id=topic)
        self.assertNotIn("referenced_message", result["messages"][0])

    async def test_threads_stay_with_their_parent_channel_and_validate_input(self):
        sent = await server.send_message("main", "plan")
        thread_id = (await server.start_thread("main", sent["message"]["id"], "Plan"))["thread"]["id"]
        with self.assertRaises(ToolError):
            await server.start_thread("main", sent["message"]["id"], "Duplicate")
        for call in (server.read_messages("other", thread_id=thread_id),
                     server.send_message("other", "wrong parent", thread_id=thread_id)):
            with self.assertRaisesRegex(ToolError, "selected channel"):
                await call
        for name in ("", " ", "x" * 101):
            with self.assertRaises(ToolError):
                await server.start_thread("main", "10", name)
        count = len(self.requests)
        for call in (server.read_messages("main", thread_id="../222"),
                     server.send_message("main", "bad ID", thread_id="../222"),
                     server.read_messages("222"), server.send_message("unknown", "bad alias")):
            with self.assertRaises(ToolError):
                await call
        self.assertEqual(len(self.requests), count)
        self.assertEqual(len(self.history["222"]), 0)

    async def test_legacy_visibility_fields_do_not_restrict_conversation_access(self):
        data = policy()
        data["channels"]["other"].update(visibility="private", allowed_sessions=["claude"])
        self.config.write_text(json.dumps(data), encoding="utf-8")
        with self.session("antigravity"):
            await server.send_message("other", "shared conversation")
            self.assertEqual(len((await server.read_messages("other"))["messages"]), 1)

    async def test_independent_reads_pagination_and_human_messages(self):
        self.history["111"].append({"id": "9", "content": "human note", "author": {"id": "123", "username": "human"},
                                    "referenced_message": {"content": "never leak embedded private data"}})
        await server.send_message("main", "agent note")
        await server.send_message("main", "second note")
        first = await server.read_messages("main", after="0", limit=2)
        self.assertEqual([m["id"] for m in first["messages"]], ["9", "10"])
        self.assertFalse(first["messages"][0]["author"]["bot"])
        self.assertNotIn("referenced_message", first["messages"][0])
        with self.session("codex"):
            self.assertEqual((await server.read_messages("main", after="0", limit=2))["messages"], first["messages"])
        self.assertEqual([m["id"] for m in (await server.read_messages("main", after=first["next_after"]))["messages"]], ["11"])
        self.assertEqual((await server.read_messages("main", after="11"))["next_after"], "11")
        latest = await server.read_messages("main", limit=1)
        self.assertEqual(latest["next_before"], "11")
        self.assertEqual(len((await server.read_messages("main", before="11"))["messages"]), 2)

    async def test_validation_and_cross_channel_reply(self):
        for args in ({"limit": 0}, {"after": "../222"}, {"after": "1", "before": "2"}):
            with self.assertRaises(ToolError):
                await server.read_messages("main", **args)
        for message in ("", " ", "x" * 2000, "😀" * 1000):
            with self.assertRaises(ToolError):
                await server.send_message("main", message)
        self.assertEqual(self.requests, [])
        private = await server.send_message("other", "private note")
        with self.assertRaises(ToolError):
            await server.send_message("main", "reply", private["message"]["id"])

    async def test_empty_initial_poll_preserves_the_first_burst(self):
        first = await server.read_messages("main", limit=2)
        self.assertEqual(first["next_after"], "0")
        for text in ("first", "second", "third"):
            await server.send_message("main", text)
        page = await server.read_messages("main", after=first["next_after"], limit=2)
        last = await server.read_messages("main", after=page["next_after"], limit=2)
        self.assertEqual([m["id"] for m in page["messages"] + last["messages"]], ["10", "11", "12"])

    async def test_revocation_unknown_session_and_malformed_config_fail_closed(self):
        data = policy()
        del data["sessions"]["claude"]
        self.config.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ToolError):
            await server.read_messages("other")
        with self.session("unregistered"), self.assertRaises(ToolError):
            await server.read_messages("main")
        for body in ("{}", "invalid JSON"):
            self.config.write_text(body, encoding="utf-8")
            with self.assertRaises(ToolError):
                await server.send_message("main", "hello")
        self.config.unlink()
        with self.assertRaises(ToolError):
            await server.read_messages("main")
        self.assertEqual(self.requests, [])

    async def test_ambiguous_policies_are_rejected(self):
        for mutate in (
            lambda d: d["channels"].update(alias={"id": "222"}),
            lambda d: d["sessions"]["codex"].update(token_sha256=d["sessions"]["claude"]["token_sha256"]),
            lambda d: d["channels"].update(alias={"id": "0222"}),
            lambda d: d["channels"]["main"].update(id="0"),
            lambda d: d["channels"]["main"].update(id="18446744073709551616"),
        ):
            data = policy()
            mutate(data)
            self.config.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(AccessError):
                load_registry(self.config)

    async def test_duplicate_policy_keys_fail_closed(self):
        body = json.dumps(policy())
        for malformed in (
            body.replace('"id": "222"', '"id": "222", "id": "333"'),
            body[:-1] + ', "channels": {"other": {"id": "222"}}}',
        ):
            self.config.write_text(malformed, encoding="utf-8")
            with self.session("antigravity"), self.assertRaises(ToolError):
                await server.read_messages("other")
        self.assertEqual(self.requests, [])

    async def test_errors_do_not_echo_credentials(self):
        failure = httpx.ConnectError("never-return-this-token secret-claude")
        with patch.object(server.httpx, "AsyncClient") as client:
            client.return_value.__aenter__.return_value.post = AsyncMock(side_effect=failure)
            client.return_value.__aenter__.return_value.get = AsyncMock(side_effect=failure)
            for call in (server.send_message("main", "hello"), server.read_messages("main")):
                with self.assertRaises(ToolError) as error:
                    await call
                self.assertNotIn("never-return-this-token", str(error.exception))
                self.assertNotIn("secret-claude", str(error.exception))
            self.assertNotIn("never-return-this-token", await server.raise_hand("help"))

    async def test_discord_http_errors_are_actionable_without_echoing_upstream_text(self):
        cases = (
            (401, {}, {}, "DISCORD_BOT_TOKEN"),
            (403, {}, {}, "channel permissions"),
            (404, {}, {}, "channel ID and reply ID"),
            (429, {"Retry-After": "12.5"}, {"retry_after": 99}, "Retry after 12.5 seconds"),
            (429, {"Retry-After": "secret-claude"}, {"retry_after": 2.25}, "Retry after 2.25 seconds"),
            (429, {}, {"retry_after": "never-return-this-token"}, "Retry later"),
            (503, {}, {}, "temporarily unavailable"),
        )
        for status, headers, body, expected in cases:
            with self.subTest(status=status, expected=expected):
                response = httpx.Response(status, headers=headers,
                    json={"message": "never-return-this-token secret-claude", **body},
                    request=httpx.Request("GET", "https://discord.com/api/v10/channels/111/messages"))
                with patch.object(server.httpx, "AsyncClient") as client:
                    client.return_value.__aenter__.return_value.get = AsyncMock(return_value=response)
                    client.return_value.__aenter__.return_value.post = AsyncMock(return_value=response)
                    for call in (server.read_messages("main"), server.send_message("main", "hello")):
                        with self.assertRaises(ToolError) as error:
                            await call
                        self.assertIn(str(status), str(error.exception))
                        self.assertIn(expected, str(error.exception))
                        self.assertNotIn("never-return-this-token", str(error.exception))
                        self.assertNotIn("secret-claude", str(error.exception))

    async def test_rate_limit_delay_rejects_invalid_numbers(self):
        for value in (True, None, -1, "NaN", "Infinity", [], {}):
            with self.subTest(value=value):
                response = httpx.Response(429, json={"retry_after": value})
                self.assertIsNone(server._retry_after(response))

    async def test_raise_hand_waits_for_rate_limit_then_receives_reply(self):
        response = httpx.Response(429, json={"retry_after": 12.5},
            request=httpx.Request("GET", "https://discord.com/api/v10/channels/111/messages"))
        failure = httpx.HTTPStatusError("secret-claude", request=response.request, response=response)
        reply = {"id": "99", "author": {"username": "operator"}, "content": "try again",
                 "message_reference": {"message_id": "10"}}
        with patch.object(server.asyncio, "sleep", new=AsyncMock()) as sleep, \
                patch.object(server, "get_new_messages", new=AsyncMock(side_effect=[failure, [reply]])), \
                patch.object(server, "add_reaction", new=AsyncMock()), patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(await server.raise_hand("help"), "Human (operator) replied: try again")
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [5, 12.5])

    async def test_raise_hand_stops_polling_when_discord_denies_access(self):
        response = httpx.Response(403, request=httpx.Request("GET", "https://discord.com/api/v10/channels/111/messages"))
        failure = httpx.HTTPStatusError("secret-claude", request=response.request, response=response)
        with patch.object(server.asyncio, "sleep", new=AsyncMock()), \
                patch.object(server, "get_new_messages", new=AsyncMock(side_effect=failure)) as read:
            result = await server.raise_hand("help")
        self.assertIn("channel permissions", result)
        self.assertNotIn("secret-claude", result)
        self.assertEqual(read.await_count, 1)

    async def test_raise_hand_still_waits_for_human_reply(self):
        reply = {"id": "99", "author": {"username": "operator"}, "content": "try again", "message_reference": {"message_id": "10"}}
        with patch.object(server.asyncio, "sleep", new=AsyncMock()), \
                patch.object(server, "get_new_messages", new=AsyncMock(return_value=[reply])), \
                patch.object(server, "add_reaction", new=AsyncMock()):
            self.assertEqual(await server.raise_hand("help"), "Human (operator) replied: try again")
        with patch.object(server, "DISCORD_CHANNEL_ID", "333"), self.session("antigravity"):
            count = len(self.requests)
            self.assertIn("not configured", await server.raise_hand("help"))
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
                patch.object(server, "DISCORD_CHANNEL_ID", "333"), self.session("antigravity"):
            self.assertIn("not configured", await server.raise_hand("blocked"))
            self.config.unlink()
            self.assertIn("configuration", await server.raise_hand("still blocked"))
        self.assertEqual(self.requests, [])

    async def test_mcp_schema_has_no_caller_controlled_identity(self):
        async with Client(server.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            self.assertEqual(set(tools), {"send_message", "read_messages", "start_thread", "raise_hand", "tiny_llm_task"})
            self.assertEqual(set(tools["send_message"].inputSchema["properties"]), {"channel", "message", "reply_to", "thread_id"})
            result = await client.call_tool("send_message", {"channel": "main", "message": "hello"})
            self.assertFalse(result.is_error)

    async def test_registration_creates_unique_credentials_without_storing_raw_token_in_registry(self):
        first, first_path = register("Planner", self.config)
        second, second_path = register("Planner", self.config)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first_path.read_text(), second_path.read_text())
        token = first_path.read_text().strip().split("=", 1)[1]
        self.assertNotIn(token, self.config.read_text())
        self.assertEqual(Registry(self.config, token).authorize("main")[0]["id"], first)

    async def test_registration_cli_uses_shared_config_and_prints_usable_client_config(self):
        for explicit_config in (False, True):
            with self.subTest(explicit_config=explicit_config):
                args = ["Planner", "--client", "claude-code"]
                env_path = self.config
                if explicit_config:
                    env_path = self.config.with_name("missing.json")
                    args += ["--config", str(self.config)]
                with patch("register_session.load_dotenv"), \
                        patch.dict(os.environ, {"RAISE_HAND_CONFIG": str(env_path)}), \
                        patch("sys.stdout", new=io.StringIO()) as output:
                    register_main(args)
                text = output.getvalue()
                config = json.loads(text[text.index("{"):])["mcpServers"]["raise-hand-mcp"]
                self.assertTrue(Path(config["command"]).is_file())
                self.assertEqual(Path(config["args"][0]), ROOT / "server.py")
                self.assertEqual(Path(config["env"]["RAISE_HAND_CONFIG"]), self.config)
                secret = Path(config["env"]["RAISE_HAND_SESSION_FILE"])
                token = secret.read_text().strip().split("=", 1)[1]
                self.assertNotIn(token, text)
                self.assertEqual(Registry(self.config, token).authorize("other")[0]["name"], "Planner")

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
