import unittest
from datetime import datetime
from types import SimpleNamespace

import pytz

from commands import room_code
from utils.helpers import get_start_of_day_utc


def _notice_layout(text):
    """LayoutView(Components V2) 형태의 스크림 공지 메시지."""
    child = SimpleNamespace(content=text)
    component = SimpleNamespace(children=[child])
    return SimpleNamespace(components=[component], embeds=[])


def _notice_embed(title):
    """레거시 Embed 형태의 스크림 공지 메시지."""
    return SimpleNamespace(components=[], embeds=[SimpleNamespace(title=title)])


def _plain(title=None):
    """공지가 아닌 일반 메시지."""
    embeds = [SimpleNamespace(title=title)] if title else []
    return SimpleNamespace(components=[], embeds=embeds)


class _FakeChannel:
    """channel.history(after=..., limit=None)를 흉내내는 비동기 채널."""

    def __init__(self, messages):
        self._messages = messages

    def history(self, *args, **kwargs):
        messages = self._messages

        class _AsyncIter:
            def __aiter__(self_inner):
                self_inner._it = iter(messages)
                return self_inner

            async def __anext__(self_inner):
                try:
                    return next(self_inner._it)
                except StopIteration:
                    raise StopAsyncIteration

        return _AsyncIter()


class RoomCodeLogicTest(unittest.IsolatedAsyncioTestCase):
    def test_is_scrim_notice_message_layout(self):
        self.assertTrue(
            room_code._is_scrim_notice_message(_notice_layout("📢 스크림 공지 - 1라운드"))
        )

    def test_is_scrim_notice_message_embed(self):
        self.assertTrue(
            room_code._is_scrim_notice_message(_notice_embed("📢 스크림 공지 - 2라운드"))
        )

    def test_is_scrim_notice_message_false(self):
        self.assertFalse(room_code._is_scrim_notice_message(_plain("그냥 잡담")))
        self.assertFalse(room_code._is_scrim_notice_message(_plain()))

    async def test_get_round_number_counts_notices_plus_one(self):
        """공지 2개(잡담 섞임) → 다음 라운드는 3."""
        channel = _FakeChannel([
            _notice_layout("📢 스크림 공지 - 1라운드"),
            _plain("잡담"),
            _notice_embed("📢 스크림 공지 - 2라운드"),
            _plain(),
        ])
        self.assertEqual(await room_code.get_round_number(channel), 3)

    async def test_get_round_number_no_notice_returns_one(self):
        """당일 공지가 없으면 1라운드."""
        channel = _FakeChannel([_plain("잡담"), _plain()])
        self.assertEqual(await room_code.get_round_number(channel), 1)

    def test_get_start_of_day_utc(self):
        """KST 자정은 전날 15:00 UTC."""
        kst = pytz.timezone('Asia/Seoul')
        now = kst.localize(datetime(2026, 6, 12, 21, 30, 0))
        start = get_start_of_day_utc(now)
        self.assertEqual(start.hour, 15)
        self.assertEqual(start.utcoffset().total_seconds(), 0)


if __name__ == "__main__":
    unittest.main()
