import unittest
from unittest.mock import MagicMock

import discord

import main
from bot.client import ScrimBot
from commands import schedule
from config.settings import settings


class RoomCodeCommandTest(unittest.TestCase):
    def test_option_has_korean_name_and_description(self):
        client = ScrimBot()
        main._register_app_commands(client)
        command = client.tree.get_command("방코드", guild=discord.Object(id=settings.GUILD_ID))
        options = command.to_dict(client.tree)["options"]
        self.assertEqual(options[0]["name"], "코드")
        self.assertEqual(options[0]["description"], "6자리 방 코드")


class ScheduleGuildLookupTest(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_uses_configured_guild(self):
        other = MagicMock()
        target = MagicMock()
        client = MagicMock()
        client.guilds = [other, target]
        client.get_guild = MagicMock(return_value=target)
        target.get_channel.return_value = None

        await schedule.setup_schedule_dashboard(client)

        client.get_guild.assert_called_once_with(settings.GUILD_ID)
        target.get_channel.assert_called_once_with(settings.SCHEDULE_CHANNEL_ID)
        other.get_channel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
