# Raise Hand MCP Server

This MCP Server integrates with a Discord bot to allow AI agents to send a distress call or "raise their hand" when they encounter an issue they cannot solve, or when they need human input. The agent will pause, post a message to Discord, and wait for a human to reply. Once the human replies in Discord, the agent captures the response and resumes its work.

## Setup Instructions

### 1. Create a Discord Bot
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** and give it a name.
3. Go to the **Bot** tab and click **Add Bot** (if not already added).
4. Under **Privileged Gateway Intents**, turn on **Message Content Intent** (so it can read human replies).
5. Copy the **Token** (you will need this later). Do not share this token!

### 2. Invite the Bot to your Server
1. In the Developer Portal, go to **OAuth2 -> URL Generator**.
2. Select the `bot` scope.
3. Select the following Bot Permissions: `Read Messages/View Channels`, `Send Messages`, and `Read Message History`.
4. Copy the generated URL and paste it into your browser to invite the bot to your desired Discord server.

### 3. Get the Channel ID
1. In Discord, go to **User Settings -> Advanced** and turn on **Developer Mode**.
2. Right-click the channel where you want the bot to post messages and click **Copy Channel ID**.

### 4. Configure the Environment
Create a `.env` file in the root of this project and add your token and channel ID:
```
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_CHANNEL_ID=your_channel_id_here
```

### 5. Running the MCP Server
Install the dependencies:
```bash
python -m venv .venv
# On Windows
.venv\Scripts\activate
# On Linux/macOS
source .venv/bin/activate
pip install -r requirements.txt
```

Run the MCP Inspector to test:
```bash
npx @modelcontextprotocol/inspector .venv/Scripts/python server.py
```
