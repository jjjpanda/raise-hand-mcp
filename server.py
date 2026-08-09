import os
import asyncio
from mcp.server.fastmcp import FastMCP
import httpx
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

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

@mcp.tool()
async def raise_hand(reason: str, message: str) -> str:
    """
    Call this tool when you need human assistance, encounter a problem you cannot solve,
    or need input on how to proceed. This will post a message to a human and pause your
    execution until the human replies.
    
    Args:
        reason: A short summary of why you are raising your hand (e.g., "Need confirmation on API key", "Stuck on weird bug").
        message: The full detailed message you want to send to the human.
    
    Returns:
        The response from the human.
    """
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return "Error: Discord bot token or channel ID not configured on the server."
    
    formatted_message = f"**🤖 AI Agent Distress Call**\n**Reason:** {reason}\n**Message:**\n{message}\n\n*Please reply in this channel to resume the agent.*"
    
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
                new_messages = await get_new_messages(client, bot_msg_id)
                if new_messages:
                    # Sort messages by ID/timestamp to get the oldest one that appeared after our bot message
                    new_messages.sort(key=lambda m: m["id"])
                    
                    # We look for a message that is not from a bot
                    for msg in new_messages:
                        if not msg.get("author", {}).get("bot", False):
                            reply_text = msg.get("content", "")
                            human_author = msg.get("author", {}).get("username", "Unknown human")
                            return f"Human ({human_author}) replied: {reply_text}"
            except Exception as e:
                # If there's a temporary network issue, we can just continue polling,
                # but let's print it so we can debug.
                print(f"Error polling Discord: {e}", flush=True)

if __name__ == "__main__":
    mcp.run()
