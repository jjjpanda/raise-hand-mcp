import os
import asyncio
import urllib.parse
from fastmcp import FastMCP
import httpx
from dotenv import load_dotenv

# Load environment variables from .env file in the same directory as this script
dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path)

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
async def raise_hand(reason: str, message: str) -> str:
    """
    Call this tool when you need human assistance, encounter a problem you cannot solve,
    or need input on how to proceed. This will post a message to a human and pause your
    execution until the human replies. Consider this a "distress call" to get help from a human operator.
    
    Args:
        reason: A short summary of why you are raising your hand (e.g., "Need confirmation on API key", "Stuck on weird bug").
        message: The full detailed message you want to send to the human.
    
    Returns:
        The response from the human or emoji reaction to your message.
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

if __name__ == "__main__":
    mcp.run()
