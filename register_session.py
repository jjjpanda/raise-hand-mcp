"""Operator-only helper: register a new identity without printing its secret."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import uuid

from messaging import AccessError, load_registry


def register(name: str, config: Path) -> tuple[str, Path]:
    if not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise AccessError("Name must contain 1-80 characters and no control characters.")
    data = load_registry(config)
    session_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    data["sessions"][session_id] = {
        "name": name,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }
    secret_dir = config.parent / ".sessions"
    secret_dir.mkdir(exist_ok=True, mode=0o700)
    session_file = secret_dir / f"{session_id}.env"
    temporary = config.with_name(f".{config.name}.{session_id}.tmp")
    created = False
    try:
        with session_file.open("x", encoding="utf-8") as stream:
            created = True
            os.chmod(session_file, 0o600)
            stream.write(f"MCP_SESSION_TOKEN={token}\n")
        # Atomic replacement prevents readers from observing a partial JSON file.
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(config)
    except OSError:
        temporary.unlink(missing_ok=True)
        if created:
            session_file.unlink(missing_ok=True)
        raise
    return session_id, session_file.resolve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("channels.json"))
    args = parser.parse_args()
    try:
        session_id, session_file = register(args.name, args.config.resolve())
    except (AccessError, OSError):
        parser.exit(1, "Registration failed. Check the name, shared config, and file permissions.\n")
    print(f"Session ID: {session_id}\nRAISE_HAND_SESSION_FILE={session_file}")
