# Raise Hand MCP Server

This MCP Server integrates with a Discord bot to allow AI agents to send a distress call or "raise their hand" when they encounter an issue they cannot solve, or when they need human input. The agent will pause, post a message to Discord, and wait for a human to reply or react. Once the human replies to the message or reacts with a valid emoji (👍, 👎, ✅, ❌) in Discord, the agent captures the response and resumes its work.

It also exposes `tiny_llm_task`, which offloads small, cheap tasks to a local [llama-cpp-scripts](https://github.com/jjjpanda/llama-cpp-scripts) chat model instead of the main model. On startup, this server runs that project's `npm run models` to list available models in the tool's description; the `npm run chat` server itself is started on demand on the first `tiny_llm_task` call, and stopped after `LLAMA_IDLE_TIMEOUT` seconds of inactivity.

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
3. Select the following Bot Permissions: `Read Messages/View Channels`, `Send Messages`, `Read Message History`, and `Add Reactions`.
4. Copy the generated URL and paste it into your browser to invite the bot to your desired Discord server.

### 3. Get the Channel ID
1. In Discord, go to **User Settings -> Advanced** and turn on **Developer Mode**.
2. Right-click the channel where you want the bot to post messages and click **Copy Channel ID**.

### 4. Configure the Environment
Create a `.env` file in the root of this project and add your token and channel ID:
```
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_CHANNEL_ID=your_channel_id_here

# Optional: enables the tiny_llm_task tool
# https://github.com/jjjpanda/llama-cpp-scripts
LLAMA_SCRIPTS_PATH=/absolute/path/to/llama-cpp-scripts
LLAMA_CHAT_PORT=8080
LLAMA_IDLE_TIMEOUT=300
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

#### Option A: Testing (MCP Inspector)
Run the MCP Inspector to test the server interactively in your browser:
```bash
npx @modelcontextprotocol/inspector .venv/Scripts/python server.py
```

#### Option B: Regular Running (For MCP Clients)
To use this server with an MCP client (like Claude for Desktop, Cursor, or your custom AI agent workflows), configure the client to run the Python script directly. The server automatically communicates via standard input/output (stdio).

For example, in `claude_desktop_config.json`, you would add:
```json
{
  "mcpServers": {
    "raise-hand-mcp": {
      "command": "/absolute/path/to/raise-hand-mcp/.venv/Scripts/python",
      "args": [
        "/absolute/path/to/raise-hand-mcp/server.py"
      ],
      "env": {
        "DISCORD_BOT_TOKEN": "your_bot_token_here",
        "DISCORD_CHANNEL_ID": "your_channel_id_here"
      }
    }
  }
}
```
*(Make sure to use the absolute paths for the `python` executable inside the `.venv` directory and for `server.py`.)*

#### Option C: Claude Code
To add this server to Claude Code, you can use the `claude mcp add` command. Since the server automatically loads your `.env` file, you don't need to pass your tokens in the terminal. Just provide the absolute path to your virtual environment's Python executable and the `server.py` script:

```bash
claude mcp add raise-hand-mcp /absolute/path/to/raise-hand-mcp/.venv/Scripts/python /absolute/path/to/raise-hand-mcp/server.py
```
