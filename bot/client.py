"""
Discord 클라이언트 설정
"""
import discord
from discord.ext import commands


class ScrimBot(commands.Bot):
    """스크림봇 클라이언트"""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True

        super().__init__(
            command_prefix='!',
            intents=intents,
            help_command=None
        )
