# Genie

Claude-powered Discord bot. `/ask`, `/reset`, `/ping`, or just `@Genie` it.
Keeps the last 10 exchanges per channel as context.

## Setup (≈5 min)
1. https://discord.com/developers/applications → **New Application** → name it `Genie`.
2. **Bot** tab → Reset Token → copy it. Turn on **Message Content Intent**.
3. **OAuth2 → URL Generator** → scopes `bot`, `applications.commands`;
   permissions `Send Messages`, `Read Message History`, `View Channels`. Open the URL, add to your server.
4. `cp .env.example .env` and fill in `DISCORD_TOKEN`, `ANTHROPIC_API_KEY`,
   and `DISCORD_GUILD_ID` (right-click server → Copy Server ID, with Developer Mode on).
5. Run:
   ```
   pip install -r requirements.txt
   python bot.py
   ```
   or `docker build -t genie . && docker run --env-file .env genie`

## Hosting
Needs an always-on process (it holds a websocket to Discord), so serverless like Vercel won't work.
Use a small VPS, Fly.io, Railway, or a machine at home.
