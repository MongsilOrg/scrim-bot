import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from commands import room_code


def _member(*role_names):
    return SimpleNamespace(roles=[SimpleNamespace(name=n, id=0) for n in role_names])


class SubWeatherPermissionTest(unittest.IsolatedAsyncioTestCase):
    async def _denial(self, member, *, admin=False, rest_day=False):
        with patch.object(room_code, "is_admin", return_value=admin), \
                patch.object(room_code, "get_rest_day_info", new=AsyncMock(return_value={"is_rest_day": rest_day})):
            return await room_code._sub_weather_denial(member, "A")

    async def test_admin_always_allowed(self):
        self.assertIsNone(await self._denial(_member(), admin=True))

    async def test_weekday_group_member_denied(self):
        denied = await self._denial(_member("A조"))
        self.assertEqual(denied, "서브 날씨는 관리자만 고를 수 있습니다.")

    async def test_rest_day_group_member_allowed(self):
        self.assertIsNone(await self._denial(_member("A조"), rest_day=True))

    async def test_rest_day_other_group_denied(self):
        denied = await self._denial(_member("B조"), rest_day=True)
        self.assertIn("A조 참가자", denied)

    async def test_rest_day_lookup_timeout_counts_as_weekday(self):
        async def slow(*_args, **_kwargs):
            await asyncio.sleep(1)
            return {"is_rest_day": True}

        with patch.object(room_code, "REST_DAY_CHECK_TIMEOUT", 0.01), \
                patch.object(room_code, "is_admin", return_value=False), \
                patch.object(room_code, "get_rest_day_info", new=slow):
            denied = await room_code._sub_weather_denial(_member("A조"), "A")
        self.assertEqual(denied, "서브 날씨는 관리자만 고를 수 있습니다.")


def _view_text(view):
    texts = []

    def walk(item):
        content = getattr(item, "content", None)
        if isinstance(content, str):
            texts.append(content)
        for child in getattr(item, "children", []) or []:
            walk(child)

    for item in view.children:
        walk(item)
    return "\n".join(texts)


def _interaction(channel):
    interaction = MagicMock()
    interaction.channel = channel
    interaction.user = _member("A조")
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


class RoundOverflowTest(unittest.IsolatedAsyncioTestCase):
    async def test_fifth_round_is_refused_without_side_effects(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 1
        interaction = _interaction(channel)
        sent = AsyncMock()
        with patch.object(room_code, "get_group_letter", return_value="A"), \
                patch.object(room_code, "_can_post_room_code", return_value=True), \
                patch.object(room_code, "get_round_number", new=AsyncMock(return_value=room_code.settings.TOTAL_ROUNDS + 1)), \
                patch.object(room_code, "send_response", new=sent), \
                patch.object(room_code.BotManager, "get_instance") as get_instance:
            await room_code.방코드(interaction, "123456")

        get_instance.assert_not_called()
        interaction.followup.send.assert_not_called()
        self.assertIn(room_code.ROUND_OVERFLOW_TEXT, _view_text(sent.await_args.args[1]))


if __name__ == "__main__":
    unittest.main()
