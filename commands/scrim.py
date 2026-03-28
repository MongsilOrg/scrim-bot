"""
스크림 대시보드

지정 채널에 스크림 대시보드 메시지를 유지합니다.
봇 시작 시 기존 메시지를 찾아 뷰를 재등록하고, 없으면 새로 생성합니다.
"""
import asyncio

import discord

from bot.client import ScrimBot
from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_current_kst_time, get_next_scrim_date, is_admin

logger = get_logger('scrim')

SCRIM_CHANNEL_ID = settings.SCRIM_CHANNEL_ID


async def _refresh_scrim_dashboard(channel: discord.TextChannel) -> None:
    """스크림 대시보드 메시지를 현재 상태로 갱신합니다."""
    from commands.ui.views import TeamInputView, ScrimIdleView

    team_data_manager = BotManager.get_instance().get_team_data_manager()

    # 활성 스크림이 있는지 판단
    has_scrim = team_data_manager.scrim_day is not None

    if has_scrim:
        date_info = get_next_scrim_date()
        view = TeamInputView(
            scrim_day=team_data_manager.scrim_day,
            scrim_month=team_data_manager.scrim_month,
            scrim_weekday=date_info['weekday_name'],
        )
    else:
        view = ScrimIdleView()

    # 기존 메시지 갱신 또는 새로 생성
    if team_data_manager.dashboard_message_id:
        try:
            msg = await channel.fetch_message(team_data_manager.dashboard_message_id)
            await msg.edit(view=view, content=None, embed=None)
            return
        except (discord.NotFound, discord.HTTPException):
            pass

    msg = await channel.send(view=view)
    team_data_manager.dashboard_message_id = msg.id
    team_data_manager._save_backup()


async def setup_scrim_dashboard(client: ScrimBot) -> None:
    """봇 시작 시 스크림 대시보드를 연동합니다."""
    guild = client.guilds[0] if client.guilds else None
    if not guild:
        logger.warning("[스크림] 서버를 찾을 수 없습니다.")
        return

    channel = guild.get_channel(SCRIM_CHANNEL_ID)
    if not channel:
        logger.warning("[스크림] 대시보드 채널을 찾을 수 없습니다.")
        return

    team_data_manager = BotManager.get_instance().get_team_data_manager()
    team_data_manager.scrim_channel_id = SCRIM_CHANNEL_ID

    await _refresh_scrim_dashboard(channel)
    logger.info("[스크림] 대시보드 연동 완료")
