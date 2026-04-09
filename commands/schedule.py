"""
일정 대시보드

지정 채널에 일정 대시보드 메시지를 유지합니다.
봇 시작 시 기존 메시지를 찾아 연동하고, 없으면 새로 생성합니다.
매주 토요일 22시에 다음 주로 자동 전환합니다.
"""
import asyncio
from datetime import timedelta

import discord

from bot.client import ScrimBot
from bot.manager import BotManager
from commands.ui.schedule_views import ScheduleView
from config.logging_config import get_logger
from utils.helpers import get_current_kst_time, is_admin

logger = get_logger('schedule')

SCHEDULE_CHANNEL_ID = 1485653533637476512

_weekly_reset_task: asyncio.Task | None = None


def _should_auto_reset(schedule_mgr) -> bool:
    """현재 주차가 지났는지 (토요일 22시 이후) 확인합니다."""
    if not schedule_mgr.week_start:
        return False
    now = get_current_kst_time()
    deadline = schedule_mgr.week_start + timedelta(days=5, hours=22)
    return now >= deadline


async def _refresh_dashboard(guild: discord.Guild, channel, schedule_mgr) -> None:
    """대시보드 메시지를 현재 상태로 갱신합니다."""
    from models.schedule_manager import EXCLUDED_USER_IDS
    all_admins: list[tuple[str, str]] = []
    for member in guild.members:
        if is_admin(member) and not member.bot and member.id not in EXCLUDED_USER_IDS:
            all_admins.append((str(member.id), member.display_name))

    status_text = schedule_mgr.get_status_text(all_admins)
    has_assignments = bool(schedule_mgr.assignments)
    view = ScheduleView(status_text, has_assignments=has_assignments)

    if schedule_mgr.status_message_id:
        try:
            msg = await channel.fetch_message(schedule_mgr.status_message_id)
            await msg.edit(view=view, content=None, embed=None)
            return
        except (discord.NotFound, discord.HTTPException):
            pass

    msg = await channel.send(view=view)
    schedule_mgr.status_message_id = msg.id
    schedule_mgr.status_channel_id = SCHEDULE_CHANNEL_ID
    schedule_mgr._save_backup()


async def _weekly_reset_loop(client: ScrimBot) -> None:
    """매주 토요일 22시에 다음 주로 자동 전환하는 백그라운드 태스크."""
    await client.wait_until_ready()
    while not client.is_closed():
        now = get_current_kst_time()
        days_until_saturday = (5 - now.weekday()) % 7
        if days_until_saturday == 0 and now.hour >= 22:
            days_until_saturday = 7

        next_saturday_22 = now.replace(
            hour=22, minute=0, second=0, microsecond=0
        ) + timedelta(days=days_until_saturday)

        wait_seconds = (next_saturday_22 - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        try:
            schedule_mgr = BotManager.get_instance().get_schedule_manager()
            schedule_mgr.initialize_week()

            guild = client.guilds[0] if client.guilds else None
            if guild:
                channel = guild.get_channel(SCHEDULE_CHANNEL_ID)
                if channel:
                    await _refresh_dashboard(guild, channel, schedule_mgr)
        except Exception as e:
            logger.error(f"[일정] 자동 주차 전환 실패: {e}", exc_info=True)

        await asyncio.sleep(60)


async def setup_schedule_dashboard(client: ScrimBot) -> None:
    """봇 시작 시 일정 대시보드를 연동합니다."""
    global _weekly_reset_task

    guild = client.guilds[0] if client.guilds else None
    if not guild:
        logger.warning("[일정] 서버를 찾을 수 없습니다.")
        return

    channel = guild.get_channel(SCHEDULE_CHANNEL_ID)
    if not channel:
        logger.warning(f"[일정] 대시보드 채널을 찾을 수 없습니다.")
        return

    schedule_mgr = BotManager.get_instance().get_schedule_manager()

    if not schedule_mgr.week_label:
        schedule_mgr.initialize_week()
    elif _should_auto_reset(schedule_mgr):
        schedule_mgr.initialize_week()

    schedule_mgr.status_channel_id = SCHEDULE_CHANNEL_ID
    await _refresh_dashboard(guild, channel, schedule_mgr)

    if _weekly_reset_task is not None:
        _weekly_reset_task.cancel()
    _weekly_reset_task = asyncio.create_task(_weekly_reset_loop(client))
