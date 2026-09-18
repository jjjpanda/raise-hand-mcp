import os
import asyncio
import subprocess
import urllib.parse
from fastmcp import FastMCP
import httpx
from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path)

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
LLAMA_SCRIPTS_PATH = os.getenv("LLAMA_SCRIPTS_PATH")
LLAMA_DEFAULT_PORT = os.getenv("LLAMA_CHAT_PORT", "11400")

def _fetch_models() -> dict[str, str]:
    """Run `npm run models:all --silent` to get {model_name: port} mapping."""
    if not LLAMA_SCRIPTS_PATH:
        return {}
    try:
        result = subprocess.run(
            ["npm", "run", "models:all", "--silent"],
            cwd=LLAMA_SCRIPTS_PATH,
            capture_output=True, text=True, shell=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        models = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith(":"):
                parts = line.split(None, 1)
                if len(parts) == 2:
                    models[parts[1]] = parts[0].lstrip(":")
        return models
    except Exception:
        return {}

_MODEL_PORTS = _fetch_models()

if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
    print("Warning: DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must be set in environment.", flush=True)

# Initialize FastMCP server
mcp = FastMCP("Raise Hand / Distress Call Server")

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
        if not _MODEL_PORTS:
            return "Error: no models found. Check LLAMA_SCRIPTS_PATH and that `npm run models:all` works."
        model = next(iter(_MODEL_PORTS))

    port = _MODEL_PORTS.get(model, LLAMA_DEFAULT_PORT)
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Error calling local llama chat server on port {port}: {str(e)}"

if _MODEL_PORTS:
    _tool = asyncio.run(mcp.get_tool("tiny_llm_task"))
    _tool.description += f"\n\nAvailable models: {', '.join(f'{m} (:{p})' for m, p in _MODEL_PORTS.items())}"

if __name__ == "__main__":
    mcp.run()
