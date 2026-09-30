"""Minimal Telegram Bot API client (long polling, HTML messages)."""
import asyncio
import logging

import httpx

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.enabled = bool(token and chat_id)
        self.chat_id = str(chat_id)
        self.http = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}", timeout=40)
        self.offset = 0

    async def close(self):
        await self.http.aclose()

    async def send(self, text: str):
        if not self.enabled:
            log.info("[telegram disabled] %s", text)
            return
        for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]:
            try:
                r = await self.http.post("/sendMessage", json={
                    "chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                    "disable_web_page_preview": True})
                if r.status_code != 200:
                    log.warning("telegram send %s: %s", r.status_code, r.text)
            except httpx.HTTPError as e:
                log.warning("telegram send failed: %s", e)

    async def set_commands(self, commands: list[tuple[str, str]]):
        if self.enabled:
            await self.http.post("/setMyCommands", json={
                "commands": [{"command": c, "description": d} for c, d in commands]})

    async def poll(self, handler):
        """Call handler(command, args) for messages from the configured chat only."""
        if not self.enabled:
            return
        while True:
            try:
                r = await self.http.get("/getUpdates", params={"offset": self.offset, "timeout": 30})
                for upd in r.json().get("result", []):
                    self.offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue
                    text = (msg.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue
                    parts = text.split()
                    cmd = parts[0][1:].split("@")[0].lower()
                    try:
                        reply = await handler(cmd, parts[1:])
                    except Exception as e:
                        log.exception("command %s", cmd)
                        reply = f"⚠️ Lỗi: {e}"
                    if reply:
                        await self.send(reply)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram poll: %s", e)
                await asyncio.sleep(5)
