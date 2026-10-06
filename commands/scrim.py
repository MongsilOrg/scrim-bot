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
from models.team_data_manager import set_dashboard_refresh_hook
from services.holidays_api import get_rest_day_info
from utils.helpers import get_next_scrim_date
from utils.layout_helpers import upsert_persistent_message

logger = get_logger('scrim')

SCRIM_CHANNEL_ID = settings.SCRIM_CHANNEL_ID

_daily_reset_task: asyncio.Task | None = None

# 신청이 몰릴 때 이 간격 안의 변경을 편집 한 번으로 묶음
DASHBOARD_REFRESH_DELAY_SECONDS = 5
_dashboard_refresh_task: asyncio.Task | None = None
_dashboard_lock = asyncio.Lock()


def request_dashboard_refresh() -> None:
    global _dashboard_refresh_task
    if _dashboard_refresh_task is not None and not _dashboard_refresh_task.done():
        return
    _dashboard_refresh_task = asyncio.create_task(_debounced_dashboard_refresh())


async def _debounced_dashboard_refresh() -> None:
    global _dashboard_refresh_task
    await asyncio.sleep(DASHBOARD_REFRESH_DELAY_SECONDS)
    # 편집 중에 들어온 요청은 다음 편집으로 넘김
    _dashboard_refresh_task = None

    bot_manager = BotManager.get_instance()
    team_data_manager = bot_manager.get_team_data_manager()
    # 22시 전환 중에는 메시지 ID가 잠시 비어 새 대시보드가 생길 수 있음
    if not team_data_manager.dashboard_message_id or is_scrim_expired(team_data_manager):
        return
    client = bot_manager.get_client()
    channel = client.get_channel(SCRIM_CHANNEL_ID) if client else None
    if channel is None:
        logger.warning(f"[스크림] 대시보드 갱신 건너뜀 - 채널 없음: {SCRIM_CHANNEL_ID}")
        return
    try:
        await _refresh_scrim_dashboard(channel)
    except Exception as e:
        logger.error(f"[스크림] 대시보드 갱신 실패: {e}", exc_info=True)


async def _refresh_scrim_dashboard(channel: discord.TextChannel) -> None:
    async with _dashboard_lock:
        await _edit_scrim_dashboard(channel)


async def _edit_scrim_dashboard(channel: discord.TextChannel) -> None:
    team_data_manager = BotManager.get_instance().get_team_data_manager()
    date_info = get_next_scrim_date()

    scrim_day = team_data_manager.scrim_day or date_info['day']
    scrim_month = team_data_manager.scrim_month or date_info['month']

    try:
        scrim_is_rest_day = (await get_rest_day_info(date(date_info['year'], scrim_month, scrim_day)))["is_rest_day"]
    except ValueError:
        scrim_is_rest_day = False

    view = TeamInputView(
        scrim_day=scrim_day,
        scrim_month=scrim_month,
        scrim_weekday=date_info['weekday_name'],
        is_rest_day=scrim_is_rest_day,
        phase=team_data_manager.registration_phase(),
        team_count=len(team_data_manager.teams),
    )

    new_id = await upsert_persistent_message(channel, team_data_manager.dashboard_message_id, view)
    if new_id != team_data_manager.dashboard_message_id:
        team_data_manager.dashboard_message_id = new_id
        team_data_manager.save_backup()


async def _sync_scrim_dashboard(client: ScrimBot) -> None:
    guild = client.get_guild(settings.GUILD_ID)
    if not guild:
        logger.error(f"[스크림] 서버를 찾을 수 없음 - 서버 ID: {settings.GUILD_ID}")
        return

    channel = guild.get_channel(SCRIM_CHANNEL_ID)
    if not channel:
        logger.error(f"[스크림] 대시보드 채널을 찾을 수 없음 - 채널 ID: {SCRIM_CHANNEL_ID}")
        return

    team_data_manager = BotManager.get_instance().get_team_data_manager()
    team_data_manager.scrim_channel_id = SCRIM_CHANNEL_ID

    if is_scrim_expired(team_data_manager):
        await transition_to_next_scrim(client, channel, _refresh_scrim_dashboard)
        return

    await _refresh_scrim_dashboard(channel)

    if not team_data_manager.is_team_assignment_started and team_data_manager.teams:
        try:
            await team_data_manager.update_mmr_message(channel)
            logger.info("[스크림] MMR 메시지 재생성 완료")
        except Exception as e:
            logger.error(f"[스크림] MMR 메시지 재생성 실패: {e}", exc_info=True)


async def setup_scrim_dashboard(client: ScrimBot) -> None:
    global _daily_reset_task

    set_dashboard_refresh_hook(request_dashboard_refresh)
    try:
        await _sync_scrim_dashboard(client)
    finally:
        # 대시보드 연동이 실패해도 22시 전환 유지
        if _daily_reset_task is not None:
            _daily_reset_task.cancel()
        _daily_reset_task = asyncio.create_task(daily_reset_loop(client, _refresh_scrim_dashboard))

    logger.info("[스크림] 대시보드 연동 완료")
