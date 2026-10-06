import unittest

import discord

import main
from bot.client import ScrimBot
from config.settings import settings


class RoomCodeCommandTest(unittest.TestCase):
    def test_option_has_korean_name_and_description(self):
        client = ScrimBot()
        main._register_app_commands(client)
        command = client.tree.get_command("방코드", guild=discord.Object(id=settings.GUILD_ID))
        options = command.to_dict(client.tree)["options"]
        self.assertEqual(options[0]["name"], "코드")
        self.assertEqual(options[0]["description"], "6자리 방 코드")


if __name__ == "__main__":
    unittest.main()
