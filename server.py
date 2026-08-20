import atexit
import os
import asyncio
import subprocess
import tempfile
import time
import urllib.parse
from fastmcp import FastMCP
import httpx
from dotenv import load_dotenv

# Load environment variables from .env file in the same directory as this script
dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path)

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
LLAMA_SCRIPTS_PATH = os.getenv("LLAMA_SCRIPTS_PATH")
LLAMA_CHAT_PORT = os.getenv("LLAMA_CHAT_PORT", "8080")
LLAMA_IDLE_TIMEOUT = float(os.getenv("LLAMA_IDLE_TIMEOUT", "300"))

if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
    print("Warning: DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must be set in environment.", flush=True)

# Initialize FastMCP server
mcp = FastMCP("Raise Hand / Distress Call Server")

_llama_locks: dict[str, asyncio.Lock] = {}
_llama_processes: dict[str, subprocess.Popen] = {}
_llama_last_used: dict[str, float] = {}
_llama_watchdogs: dict[str, asyncio.Task] = {}
_llama_active: dict[str, int] = {}

def _fetch_preset_models() -> list[str]:
    """Run `npm run models` in llama-cpp-scripts to list the models in the preset,
    without starting the chat server. Returns [] on any failure."""
    if not LLAMA_SCRIPTS_PATH:
        return []
    try:
        result = subprocess.run(
            ["npm", "run", "models", "--silent"],
            cwd=LLAMA_SCRIPTS_PATH,
            capture_output=True,
            text=True,
            shell=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        return []

async def _server_reachable(client: httpx.AsyncClient, port: str) -> bool:
    try:
        await client.get(f"http://127.0.0.1:{port}/v1/models")
        return True
    except httpx.HTTPError:
        return False

def _kill_llama_process(proc: subprocess.Popen):
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        proc.terminate()

def _cleanup_llama_processes():
    """Kill any still-running llama child processes so they don't outlive this server."""
    for proc in _llama_processes.values():
        _kill_llama_process(proc)

atexit.register(_cleanup_llama_processes)

async def _llama_idle_watchdog(script: str):
    """Kill the server started for `script` after LLAMA_IDLE_TIMEOUT seconds without use."""
    while True:
        await asyncio.sleep(5)
        if _llama_active.get(script, 0) > 0:
            continue  # a request is in flight; don't count this as idle time
        last_used = _llama_last_used.get(script)
        if last_used is None or time.monotonic() - last_used >= LLAMA_IDLE_TIMEOUT:
            proc = _llama_processes.pop(script, None)
            _llama_last_used.pop(script, None)
            _llama_watchdogs.pop(script, None)
            if proc:
                _kill_llama_process(proc)
            return

async def _ensure_llama_server(script: str, port: str) -> str | None:
    """Start `npm run <script>` in llama-cpp-scripts if nothing is already answering on
    `port` (this or another session may already own it), then wait until it responds.
    If this process is the one that started it, it is killed after LLAMA_IDLE_TIMEOUT
    seconds without a call; a server found already running is left alone.

    Returns None once the server is reachable, or an error string."""
    if not LLAMA_SCRIPTS_PATH:
        return "Error: LLAMA_SCRIPTS_PATH not configured on the server."

    lock = _llama_locks.setdefault(script, asyncio.Lock())
    async with lock:
        async with httpx.AsyncClient(timeout=2) as client:
            if await _server_reachable(client, port):
                if script in _llama_processes:
                    _llama_last_used[script] = time.monotonic()
                return None

            log_path = os.path.join(tempfile.gettempdir(), f"llama-{script}.log")
            try:
                proc = subprocess.Popen(
                    ["npm", "run", script, "--", port],
                    cwd=LLAMA_SCRIPTS_PATH,
                    stdout=open(log_path, "w"),
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    shell=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception as e:
                return f"Error starting 'npm run {script}': {e}"

            for _ in range(60):
                await asyncio.sleep(1)
                if await _server_reachable(client, port):
                    _llama_processes[script] = proc
                    _llama_last_used[script] = time.monotonic()
                    if script not in _llama_watchdogs:
                        _llama_watchdogs[script] = asyncio.create_task(_llama_idle_watchdog(script))
                    return None

        _kill_llama_process(proc)
        return f"Error: '{script}' server did not become ready within 60s. Check {log_path}"

async def send_discord_message(client: httpx.AsyncClient, message: str):
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "content": message
    }
    response = await client.post(url, headers=headers, json=payload)
    response.raise_for_status()
    return response.json()

async def get_new_messages(client: httpx.AsyncClient, after_message_id: str):
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
    }
    params = {
        "after": after_message_id
    }
    response = await client.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json()

async def get_message(client: httpx.AsyncClient, message_id: str):
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
    }
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

async def add_reaction(client: httpx.AsyncClient, message_id: str, emoji: str):
    encoded_emoji = urllib.parse.quote(emoji)
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}/reactions/{encoded_emoji}/@me"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
    }
    response = await client.put(url, headers=headers)
    response.raise_for_status()

@mcp.tool()
async def raise_hand(message: str) -> str:
    """
    Quick tool to request help from, or notify, a trusted operator. Use when stuck,
    confused, need assistance, or to report something unexpected. This is anonymous and not visible to the user.
    Examples: 'I'm stuck in a loop', 'This feature isn't working', 'I need help understanding
    something', 'A user reported a bug I can't reproduce', 'Something concerning happened'.

    Effect: Posts a message to the 3rd party operator's Discord and pauses execution until given reply.

    Args:
        message: The message to send. Must be under 150 characters.

    Returns:
        The operator's reply text, or the emoji reaction.
    """
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return "Error: Discord bot token or channel ID not configured on the server."

    if len(message) >= 150:
        return "Error: message must be under 150 characters. Reword it shorter and try again."

    formatted_message = f"{message}. *(Please reply to this directly!)*"
    
    async with httpx.AsyncClient() as client:
        try:
            # Post the initial message to Discord
            bot_msg = await send_discord_message(client, formatted_message)
            bot_msg_id = bot_msg["id"]
        except Exception as e:
            return f"Error sending message to Discord: {str(e)}"
        
        # Poll for a reply
        while True:
            await asyncio.sleep(5)  # Poll every 5 seconds
            try:
                # 1. Check for explicit text replies
                new_messages = await get_new_messages(client, bot_msg_id)
                if new_messages:
                    # Sort messages by ID/timestamp to get the oldest one that appeared after our bot message
                    new_messages.sort(key=lambda m: m["id"])
                    
                    for msg in new_messages:
                        if not msg.get("author", {}).get("bot", False):
                            # Verify the message is explicitly replying to our bot's message
                            msg_ref = msg.get("message_reference", {})
                            if msg_ref.get("message_id") == bot_msg_id:
                                reply_text = msg.get("content", "")
                                human_author = msg.get("author", {}).get("username", "Unknown human")
                                
                                # Acknowledge by reacting to the human's message
                                try:
                                    await add_reaction(client, msg["id"], "✅")
                                except Exception:
                                    pass # Ignore if we lack permission to react
                                    
                                return f"Human ({human_author}) replied: {reply_text}"
                
                # 2. Check for reactions on the bot's own message
                bot_msg_current = await get_message(client, bot_msg_id)
                reactions = bot_msg_current.get("reactions", [])
                valid_emojis = ["👍", "👎", "✅", "❌"]
                
                for reaction in reactions:
                    emoji_name = reaction.get("emoji", {}).get("name")
                    count = reaction.get("count", 0)
                    me = reaction.get("me", False)
                    
                    # If someone else reacted with a valid emoji
                    if emoji_name in valid_emojis and ((count > 1) or (count == 1 and not me)):
                        # Acknowledge by reacting to our own message
                        try:
                            await add_reaction(client, bot_msg_id, "✅")
                        except Exception:
                            pass
                            
                        return f"Human reacted with: {emoji_name}"
                        
            except Exception as e:
                # If there's a temporary network issue, we can just continue polling,
                # but let's print it so we can debug.
                print(f"Error polling Discord: {e}", flush=True)

@mcp.tool()
async def tiny_llm_task(prompt: str, model: str | None = None) -> str:
    """
    Offload a small, cheap task to the local model instead of the main
    model. Good for lightweight work: short rewrites, formatting, classification,
    simple extraction. Not for complex reasoning.

    Args:
        prompt: The task/instruction to send to the local model.
        model: Optional model name. See this tool's description for available models; omit to use the first available model.

    Returns:
        The local model's reply.
    """
    if not model:
        if not _AVAILABLE_MODELS:
            return "Error: no models configured. Check LLAMA_MODELS_PRESET in llama-cpp-scripts."
        model = _AVAILABLE_MODELS[0]

    error = await _ensure_llama_server("chat", LLAMA_CHAT_PORT)
    if error:
        return error

    payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}

    _llama_active["chat"] = _llama_active.get("chat", 0) + 1
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"http://127.0.0.1:{LLAMA_CHAT_PORT}/v1/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Error calling local llama chat server: {str(e)}"
    finally:
        _llama_active["chat"] -= 1
        _llama_last_used["chat"] = time.monotonic()

_AVAILABLE_MODELS = _fetch_preset_models()
if _AVAILABLE_MODELS:
    _tool = asyncio.run(mcp.get_tool("tiny_llm_task"))
    _tool.description = f"{_tool.description}\n\nAvailable models: {', '.join(_AVAILABLE_MODELS)}"

if __name__ == "__main__":
    mcp.run()
