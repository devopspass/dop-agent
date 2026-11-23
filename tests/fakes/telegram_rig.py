"""Shared Telegram test rig: strict FakeTelegram + real adapter/poller, no sleeps."""
import tempfile
from pathlib import Path

import message_store
from fake_telegram import FakeTelegram
from telegram_adapter import TelegramAdapter
from telegram_api import ChatRateLimiter, TelegramApi
from telegram_poller import TelegramPoller

FORUM = -1001234567890
USER = 4242


async def no_sleep(_s):
    return None


class Rig:
    def __init__(self, forum=True):
        self.server = FakeTelegram(forum_chat_id=FORUM if forum else None)
        self.sleeps = []

        async def rec_sleep(s):
            self.sleeps.append(s)

        self.api = TelegramApi(self.server, limiter=ChatRateLimiter(min_gap=0, sleep=no_sleep), sleep=rec_sleep)
        self.adapter = TelegramAdapter(self.api, forum_chat_id=FORUM if forum else None,
                                       allowed_user_ids=[str(USER)], sync_delay=0, sleep=no_sleep)
        self.incoming = []
        self.interrupted = []

        async def handle_incoming(msg):
            self.incoming.append(msg)

        async def interrupt(conv):
            self.interrupted.append(conv)
            return True

        async def transcribe(data, name):
            return "spoken words"

        self.poller = TelegramPoller(self.api, self.adapter, allowed_user_ids=[str(USER)],
                                     forum_chat_id=FORUM if forum else None,
                                     handle_incoming=handle_incoming, interrupt=interrupt, transcribe=transcribe,
                                     sleep=rec_sleep, clock=self.server.clock,
                                     files_dir=Path(tempfile.mkdtemp(prefix="tg-files-")))

    async def pump(self):
        await self.poller.poll_once()
        await self.poller.drain()
