"""Shared, file-backed authorization; Discord remains the only message store."""

import hashlib
import hmac
import json
import re
from pathlib import Path


class AccessError(ValueError):
    """Safe to return to an MCP caller (never contains config or credentials)."""


def snowflake(value: str) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,19}", value)
            or int(value) > 2**64 - 1):
        raise AccessError("Invalid Discord message or channel ID.")
    return value


def _unique_keys(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate policy key")
        result[key] = value
    return result


def load_registry(path: Path) -> dict:
    """Validate the whole policy so malformed/ambiguous rules fail closed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
        sessions, channels = data["sessions"], data["channels"]
        if not isinstance(sessions, dict) or not isinstance(channels, dict):
            raise ValueError
        hashes, channel_ids = set(), set()
        for session_id, session in sessions.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session_id):
                raise ValueError
            name, digest = session["name"], session["token_sha256"]
            if (not isinstance(name, str) or not name.strip() or len(name) > 80
                    or any(ord(c) < 32 for c in name)):
                raise ValueError
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError
            if digest in hashes:
                raise ValueError
            hashes.add(digest)
        for alias, channel in channels.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", alias):
                raise ValueError
            channel_id = snowflake(channel["id"])
            if channel_id == "0" or channel_id in channel_ids:
                raise ValueError  # Cannot alias a private channel as public.
            channel_ids.add(channel_id)
            if channel["visibility"] not in ("public", "private"):
                raise ValueError
            allowed = channel.get("allowed_sessions", [])
            if (not isinstance(allowed, list)
                    or any(not isinstance(s, str) or s not in sessions for s in allowed)):
                raise ValueError
        return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise AccessError("Shared messaging configuration is missing or invalid.") from None


class Registry:
    def __init__(self, path: Path, token: str):
        self.path = path
        self._token = token

    def authorize(self, channel: str, *, by_id: bool = False) -> tuple[dict, dict]:
        # Reload for every operation: separate processes see policy changes.
        data = load_registry(self.path)
        digest = hashlib.sha256(self._token.encode()).hexdigest()
        identity = None
        for session_id, session in data["sessions"].items():
            if self._token and hmac.compare_digest(digest, session["token_sha256"]):
                identity = {"id": session_id, "name": session["name"]}
                break
        if identity is None:
            raise AccessError("Session is not registered or its credential is invalid.")
        if by_id:
            selected = next((c for c in data["channels"].values() if c["id"] == channel), None)
        else:
            selected = data["channels"].get(channel)
        if selected is None or (selected["visibility"] == "private"
                               and identity["id"] not in selected.get("allowed_sessions", [])):
            raise AccessError("Channel is unavailable to this session.")
        return identity, selected
