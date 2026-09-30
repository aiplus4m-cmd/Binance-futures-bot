import asyncio
import logging
import os

from .app import App
from .config import load_secrets


async def main():
    app = App(load_secrets())
    try:
        await app.run()
    finally:
        await app.close()


if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("data/bot.log", encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
