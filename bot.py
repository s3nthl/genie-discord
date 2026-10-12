"""Genie: a Claude-powered Discord bot.

/ask <question>  ask Genie (public reply)
/reset           clear Genie's memory for this channel
/ping            health check
@Genie <text>    mention it in any channel to chat
/digest [image]  post today's Chai Digest embed from digest.json (admins only)
"""
import os
import json
import logging
from collections import defaultdict, deque

import discord
from discord import app_commands
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
MODEL = os.getenv("GENIE_MODEL", "claude-sonnet-5-5")
MAX_TOKENS = int(os.getenv("GENIE_MAX_TOKENS", "1024"))
HISTORY_TURNS = int(os.getenv("GENIE_HISTORY_TURNS", "10"))
GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional: instant slash-command sync to one server
SYSTEM = os.getenv(
    "GENIE_SYSTEM",
    "You are Genie, a sharp, concise assistant living in a Discord server. "
    "Multiple people may talk to you; each message is prefixed with the speaker's name. "
    "Keep answers short and use Discord markdown.",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("genie")

claude = AsyncAnthropic()  # reads ANTHROPIC_API_KEY
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))


class Genie(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed for @mention chat
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()  # global sync can take up to an hour to appear

    async def on_ready(self):
        log.info("Genie online as %s (model=%s)", self.user, MODEL)


bot = Genie()


async def ask_claude(channel_id: int, speaker: str, prompt: str) -> str:
    h = history[channel_id]
    user_msg = {"role": "user", "content": f"{speaker}: {prompt}"}
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[*h, user_msg],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip() or "_(no answer)_"
    h.append(user_msg)  # only store the turn once it succeeded
    h.append({"role": "assistant", "content": text})
    return text


def chunks(text: str, size: int = 1990):
    """Split on Discord's 2000-char limit, preferring line breaks."""
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text:
        yield text


@bot.tree.command(name="ask", description="Ask Genie anything")
@app_commands.describe(question="What do you wish to know?")
async def ask(interaction: discord.Interaction, question: str):
    await interaction.response.defer(thinking=True)
    try:
        answer = await ask_claude(interaction.channel_id, interaction.user.display_name, question)
    except Exception as e:
        log.exception("ask failed")
        await interaction.followup.send(f"Genie stumbled: `{type(e).__name__}`")
        return
    for part in chunks(f"> {question}\n{answer}"):
        await interaction.followup.send(part)


@bot.tree.command(name="reset", description="Clear Genie's memory for this channel")
async def reset(interaction: discord.Interaction):
    history.pop(interaction.channel_id, None)
    await interaction.response.send_message("Memory wiped for this channel.", ephemeral=True)


@bot.tree.command(name="ping", description="Check Genie is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong ({round(bot.latency * 1000)} ms)", ephemeral=True)


DIGEST_FILE = os.getenv("GENIE_DIGEST_FILE", "digest.json")
MOOD_COLORS = {"up": 0x8FE0B5, "flat": 0xD8D2C4, "down": 0xF2A08F}
SIGNAL_WORDS = {1: "🟢 Helping", 0: "⚪ Neutral", -1: "🔴 Hurting"}


def build_digest_embed(d: dict) -> discord.Embed:
    signals = d["signals"]  # {"Fed": 1, "Yields": 0, ...}
    score = sum(signals.values())
    if score >= 3:
        mood, key = "Confident", "up"
    elif score > 0:
        mood, key = "Mixed, tilting up", "up"
    elif score == 0:
        mood, key = "Mixed, no clear lean", "flat"
    elif score > -3:
        mood, key = "Mixed, tilting down", "down"
    else:
        mood, key = "Defensive", "down"
    e = discord.Embed(
        title=f"☕ Chai Digest · {d['date']}",
        description=f"**Market mood: {mood}** (net {score:+d} of 6 signals)",
        color=MOOD_COLORS[key],
    )
    for name, val in signals.items():
        e.add_field(name=name, value=SIGNAL_WORDS[max(-1, min(1, int(val)))], inline=True)
    for name in ("Fed", "Money", "Watch", "SPY"):
        if d.get(name.lower()):
            e.add_field(name=name, value=d[name.lower()], inline=False)
    e.set_footer(text="ChaiStreet · Not financial advice")
    return e


@bot.tree.command(name="digest", description="Post today's Chai Digest")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(image="Optional card image to attach")
async def digest(interaction: discord.Interaction, image: discord.Attachment | None = None):
    try:
        with open(DIGEST_FILE, encoding="utf-8") as f:
            embed = build_digest_embed(json.load(f))
    except Exception as e:
        await interaction.response.send_message(f"Couldn't read {DIGEST_FILE}: `{e}`", ephemeral=True)
        return
    if image:
        embed.set_image(url=image.url)
    await interaction.response.send_message(embed=embed)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or bot.user not in message.mentions:
        return
    prompt = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
    if not prompt:
        await message.reply("You rang? Ask me something.")
        return
    async with message.channel.typing():
        try:
            answer = await ask_claude(message.channel.id, message.author.display_name, prompt)
        except Exception as e:
            log.exception("mention failed")
            await message.reply(f"Genie stumbled: `{type(e).__name__}`")
            return
    parts = list(chunks(answer))
    await message.reply(parts[0], mention_author=False)
    for part in parts[1:]:
        await message.channel.send(part)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
