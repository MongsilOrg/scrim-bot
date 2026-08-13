"""
스크림 대시보드

지정 채널에 스크림 대시보드 메시지를 유지합니다.
봇 시작 시 기존 메시지를 찾아 뷰를 재등록하고, 없으면 새로 생성합니다.
만료 판정/다음날 전환/일일 리셋은 models/scrim_orchestrator.py가 담당합니다.
"""
import asyncio
from datetime import date

import discord

from bot.client import ScrimBot
from bot.manager import BotManager
from commands.ui.views import TeamInputView
from config.logging_config import get_logger
from config.settings import settings
from models.scrim_orchestrator import (
    daily_reset_loop,
    is_scrim_expired,
    transition_to_next_scrim,
)
from services.holidays_api import get_rest_day_info
from utils.helpers import get_next_scrim_date
from utils.layout_helpers import upsert_persistent_message

logger = get_logger('scrim')

SCRIM_CHANNEL_ID = settings.SCRIM_CHANNEL_ID

_daily_reset_task: asyncio.Task | None = None


async def _refresh_scrim_dashboard(channel: discord.TextChannel) -> None:
    """스크림 대시보드 메시지를 현재 상태로 갱신합니다."""
    team_data_manager = BotManager.get_instance().get_team_data_manager()
    date_info = get_next_scrim_date()

    # 스크림 날짜가 없으면 자동 설정
    scrim_day = team_data_manager.scrim_day or date_info['day']
    scrim_month = team_data_manager.scrim_month or date_info['month']

    # 스크림 날짜가 공휴일/일요일인지 확인 (자율 스크림 표시)
    try:
        scrim_is_rest_day = (await get_rest_day_info(date(date_info['year'], scrim_month, scrim_day)))["is_rest_day"]
    except ValueError:
        scrim_is_rest_day = False

    view = TeamInputView(
        scrim_day=scrim_day,
        scrim_month=scrim_month,
        scrim_weekday=date_info['weekday_name'],
        is_rest_day=scrim_is_rest_day,
    )

    new_id = await upsert_persistent_message(channel, team_data_manager.dashboard_message_id, view)
    if new_id != team_data_manager.dashboard_message_id:
        team_data_manager.dashboard_message_id = new_id
        team_data_manager.save_backup()


async def setup_scrim_dashboard(client: ScrimBot) -> None:
    """봇 시작 시 스크림 대시보드를 연동합니다."""
    global _daily_reset_task

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

    # 만료된 스크림이면 다음날로 자동 전환
    if team_data_manager.scrim_day is not None and is_scrim_expired(team_data_manager):
        await transition_to_next_scrim(client, channel, _refresh_scrim_dashboard)
    elif team_data_manager.scrim_day is None:
        # 첫 실행이면 오늘/내일 스크림으로 설정
        await transition_to_next_scrim(client, channel, _refresh_scrim_dashboard)
    else:
        # 활성 스크림: 대시보드 갱신
        await _refresh_scrim_dashboard(channel)

        # 조편성 전이고 팀이 있으면 MMR 메시지 재생성
        if not team_data_manager.is_team_assignment_started and team_data_manager.teams:
            try:
                await team_data_manager.update_mmr_message(channel)
                logger.info("[스크림] MMR 메시지 재생성 완료")
            except Exception as e:
                logger.error(f"[스크림] MMR 메시지 재생성 실패: {e}", exc_info=True)

    # 22시 자동 전환 태스크 시작
    if _daily_reset_task is not None:
        _daily_reset_task.cancel()
    _daily_reset_task = asyncio.create_task(daily_reset_loop(client, _refresh_scrim_dashboard))

    logger.info("[스크림] 대시보드 연동 완료")
