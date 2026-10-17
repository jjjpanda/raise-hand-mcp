"""Operator-only helper: register a new identity without printing its secret."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import uuid

from dotenv import load_dotenv

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


def client_config(client: str, config: Path, session_file: Path) -> str:
    """Render a config snippet containing credential paths, never credential values."""
    # Preserve the venv executable even when it is a symlink on Unix.
    command = Path(sys.executable).absolute().as_posix()
    script = Path(__file__).with_name("server.py").resolve().as_posix()
    env = {
        "RAISE_HAND_CONFIG": config.resolve().as_posix(),
        "RAISE_HAND_SESSION_FILE": session_file.resolve().as_posix(),
    }
    if client == "codex":
        return "\n".join([
            "[mcp_servers.raise-hand-mcp]",
            f"command = {json.dumps(command, ensure_ascii=False)}",
            f"args = [{json.dumps(script, ensure_ascii=False)}]",
            "tool_timeout_sec = 600",
            "",
            "[mcp_servers.raise-hand-mcp.env]",
            *(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in env.items()),
        ])
    server = {"command": command, "args": [script], "env": env}
    if client == "claude-code":
        server["type"] = "stdio"
    return json.dumps({"mcpServers": {"raise-hand-mcp": server}}, indent=2)


def main(argv: list[str] | None = None) -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--config", type=Path,
                        default=os.getenv("RAISE_HAND_CONFIG") or Path(__file__).with_name("channels.json"))
    parser.add_argument("--client", choices=("claude-code", "codex", "antigravity"),
                        help="Print a ready-to-paste MCP config for this client.")
    args = parser.parse_args(argv)
    config = args.config.resolve()
    try:
        session_id, session_file = register(args.name, config)
    except AccessError as error:
        parser.exit(1, f"Registration failed: {error}\n")
    except OSError:
        parser.exit(1, "Registration failed. Check shared config and file permissions.\n")
    print(f"Session ID: {session_id}\nRAISE_HAND_SESSION_FILE={session_file}")
    if args.client:
        print(f"\nMerge this into your {args.client} MCP configuration, then reconnect:\n")
        print(client_config(args.client, config, session_file))


if __name__ == "__main__":
    main()
