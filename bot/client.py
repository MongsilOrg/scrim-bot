"""
Discord 클라이언트 설정
"""
import discord
from discord.ext import commands

from config.logging_config import get_logger
from config.settings import settings
from commands.ui.layout_helpers import error_view

logger = get_logger('client')


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
    
    async def setup_hook(self):
        """봇 설정 후크"""
        try:
            # 명령어 동기화 (로그는 main.py에서 처리)
            guild = discord.Object(id=settings.GUILD_ID)
            await self.tree.sync(guild=guild)
            
        except Exception as e:
            logger.error(f'[봇클라이언트] 명령어 동기화 실패: {e}', exc_info=True)
    
    async def on_ready(self):
        """봇 준비 완료 이벤트 (로그는 main.py에서 처리)"""
        try:
            pass
        except Exception as e:
            logger.error(f'[봇클라이언트] 봇 준비 실패: {e}', exc_info=True)
    
    async def on_command_error(self, ctx, error):
        """명령어 오류 처리"""
        logger.error(f'[봇클라이언트] 명령어 오류: {error}', exc_info=True)
        
        if isinstance(error, commands.CommandNotFound):
            return
        
        await ctx.send(view=error_view("명령어 처리 중 오류가 발생했습니다."), ephemeral=True)
