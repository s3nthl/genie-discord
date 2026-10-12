"""GitHub -> Discord bridge.

Claude drops a post into the repo's posts/ folder. Genie polls that folder,
posts each one to its Discord channel, then deletes the files from the repo.
Nothing is written to Railway's disk; the post only lives in memory while sending.

Post format (posts/<name>.json):
{
  "channel": "chai-digest",            # channel name (or numeric channel ID)
  "content": "message text",           # optional, Discord markdown, any length
  "images": ["posts/<name>.png"]       # optional, repo paths of images to attach
}
"""
import asyncio
import base64
import io
import json
import logging
import os

import aiohttp
import discord

log = logging.getLogger("genie.bridge")

API = "https://api.github.com"
REPO = os.getenv("GENIE_POSTS_REPO", "s3nthl/genie-discord")
BRANCH = os.getenv("GENIE_POSTS_BRANCH", "main")
FOLDER = os.getenv("GENIE_POSTS_DIR", "posts")
INTERVAL = int(os.getenv("GENIE_POSTS_INTERVAL", "60"))
TOKEN = os.getenv("GITHUB_TOKEN")  # fine-grained token: Contents read/write on this repo only


def _chunks(text: str, size: int = 1990):
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text:
        yield text


class PostBridge:
    def __init__(self, client: discord.Client, guild_id: int | None):
        self.client = client
        self.guild_id = guild_id
        self.done: set[str] = set()  # blob SHAs already posted, guards against double-posting

    def _headers(self):
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if TOKEN:
            h["Authorization"] = f"Bearer {TOKEN}"
        return h

    async def run(self):
        if not TOKEN:
            log.warning("GITHUB_TOKEN not set: post bridge disabled")
            return
        await self.client.wait_until_ready()
        log.info("Post bridge watching %s/%s every %ss", REPO, FOLDER, INTERVAL)
        async with aiohttp.ClientSession(headers=self._headers()) as s:
            while not self.client.is_closed():
                try:
                    await self._tick(s)
                except Exception:
                    log.exception("bridge tick failed")
                await asyncio.sleep(INTERVAL)

    async def _list(self, s):
        async with s.get(f"{API}/repos/{REPO}/contents/{FOLDER}", params={"ref": BRANCH}) as r:
            if r.status == 404:
                return []  # folder doesn't exist = nothing queued
            r.raise_for_status()
            return await r.json()

    async def _get(self, s, path):
        async with s.get(f"{API}/repos/{REPO}/contents/{path}", params={"ref": BRANCH}) as r:
            r.raise_for_status()
            d = await r.json()
            return base64.b64decode(d["content"]), d["sha"]

    async def _delete(self, s, path, sha):
        body = {"message": f"genie: posted and removed {path}", "sha": sha, "branch": BRANCH}
        async with s.delete(f"{API}/repos/{REPO}/contents/{path}", json=body) as r:
            if r.status not in (200, 404):
                log.warning("delete %s -> HTTP %s", path, r.status)

    def _channel(self, ref):
        ref = str(ref).lstrip("#")
        if ref.isdigit():
            return self.client.get_channel(int(ref))
        guilds = [self.client.get_guild(self.guild_id)] if self.guild_id else self.client.guilds
        for g in guilds:
            if g:
                ch = discord.utils.get(g.text_channels, name=ref)
                if ch:
                    return ch
        return None

    async def _tick(self, s):
        items = await self._list(s)
        posts = sorted((i for i in items if i["name"].endswith(".json")), key=lambda i: i["name"])
        for item in posts:
            if item["sha"] in self.done:
                continue
            raw, sha = await self._get(s, item["path"])
            try:
                post = json.loads(raw)
            except json.JSONDecodeError:
                log.error("bad JSON in %s, leaving it for a fix", item["path"])
                continue
            ch = self._channel(post.get("channel", ""))
            if ch is None:
                log.error("channel %r not found for %s", post.get("channel"), item["path"])
                continue

            files, image_shas = [], []
            for p in post.get("images", []):
                data, isha = await self._get(s, p)
                files.append(discord.File(io.BytesIO(data), filename=os.path.basename(p)))
                image_shas.append((p, isha))

            parts = list(_chunks(post.get("content", "")))
            if files:
                await ch.send(content=parts[0] if parts else None, files=files)
                parts = parts[1:]
            for part in parts:
                await ch.send(part)
            self.done.add(sha)
            log.info("posted %s to #%s", item["path"], ch.name)

            # Ephemeral: remove the post and its images from the repo.
            for p, isha in image_shas:
                await self._delete(s, p, isha)
            await self._delete(s, item["path"], sha)
