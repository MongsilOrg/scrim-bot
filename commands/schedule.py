import asyncio
from datetime import timedelta

from bot.client import ScrimBot
from bot.manager import BotManager
import discord

from commands.ui.schedule_views import refresh_dashboard, schedule_members
from config.logging_config import get_logger
from config.settings import settings
from models.schedule_manager import OPEN_WEEKDAY, REMINDER_HOUR, REMINDER_WEEKDAY
from utils.helpers import get_current_kst_time

logger = get_logger('schedule')

_weekly_reset_task: asyncio.Task | None = None
_reminder_task: asyncio.Task | None = None


def _should_auto_reset(schedule_mgr) -> bool:
    if not schedule_mgr.week_start:
        return False
    now = get_current_kst_time()
    deadline = schedule_mgr.week_start + timedelta(days=OPEN_WEEKDAY, hours=settings.NEXT_SCRIM_OPEN_HOUR)
    return now >= deadline


async def _weekly_reset_loop(client: ScrimBot) -> None:
    await client.wait_until_ready()
    while not client.is_closed():
        now = get_current_kst_time()
        days_until_open = (OPEN_WEEKDAY - now.weekday()) % 7
        if days_until_open == 0 and now.hour >= settings.NEXT_SCRIM_OPEN_HOUR:
            days_until_open = 7

        next_open = now.replace(
            hour=settings.NEXT_SCRIM_OPEN_HOUR, minute=0, second=0, microsecond=0
        ) + timedelta(days=days_until_open)

        wait_seconds = (next_open - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        try:
            schedule_mgr = BotManager.get_instance().get_schedule_manager()
            schedule_mgr.initialize_week()

            guild = client.get_guild(settings.GUILD_ID)
            if guild:
                channel = guild.get_channel(settings.SCHEDULE_CHANNEL_ID)
                if channel:
                    await refresh_dashboard(guild, channel=channel, schedule_mgr=schedule_mgr)
            logger.info(f"[일정] 자동 주차 전환 완료 - 주차: {schedule_mgr.week_label}")
        except Exception as e:
            logger.error(f"[일정] 자동 주차 전환 실패: {e}", exc_info=True)

        await asyncio.sleep(60)


def _next_reminder(now):
    days = (REMINDER_WEEKDAY - now.weekday()) % 7
    target = now.replace(hour=REMINDER_HOUR, minute=0, second=0, microsecond=0) + timedelta(days=days)
    return target if target > now else target + timedelta(days=7)


async def send_schedule_reminder(client: ScrimBot) -> int:
    """다음 주 일정에 응답하지 않은 관리자를 일정 채널에서 멘션. 보낸 인원 수"""
    schedule_mgr = BotManager.get_instance().get_schedule_manager()
    # 편성을 마쳤거나 다음 주 일정이 아직 열리지 않았으면 보내지 않음
    if schedule_mgr.assignments or not schedule_mgr.week_start or schedule_mgr.week_start <= get_current_kst_time():
        return 0
    guild = client.get_guild(settings.GUILD_ID)
    channel = guild.get_channel(settings.SCHEDULE_CHANNEL_ID) if guild else None
    if not channel:
        logger.error(f"[일정] 알림 채널을 찾을 수 없음 - 채널 ID: {settings.SCHEDULE_CHANNEL_ID}")
        return 0
    responded = schedule_mgr.get_responded_user_ids()
    missing = [m for m in schedule_members(guild) if str(m.id) not in responded]
    if not missing:
        return 0
    await channel.send(
        f"{' '.join(m.mention for m in missing)}\n"
        f"{schedule_mgr.week_label} 일정에 아직 응답하지 않았습니다. 위 대시보드에서 참가 요일이나 불참을 골라주세요.",
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    logger.info(f"[일정] 미응답 알림 - {len(missing)}명: {', '.join(m.display_name for m in missing)}")
    return len(missing)


async def _reminder_loop(client: ScrimBot) -> None:
    await client.wait_until_ready()
    while not client.is_closed():
        now = get_current_kst_time()
        await asyncio.sleep((_next_reminder(now) - now).total_seconds())
        try:
            await send_schedule_reminder(client)
        except Exception as e:
            logger.error(f"[일정] 미응답 알림 실패: {e}", exc_info=True)
        await asyncio.sleep(60)


async def setup_schedule_dashboard(client: ScrimBot) -> None:
    global _weekly_reset_task, _reminder_task

    guild = client.get_guild(settings.GUILD_ID)
    if not guild:
        logger.warning("[일정] 서버를 찾을 수 없습니다.")
        return

    channel = guild.get_channel(settings.SCHEDULE_CHANNEL_ID)
    if not channel:
        logger.warning("[일정] 대시보드 채널을 찾을 수 없습니다.")
        return

    schedule_mgr = BotManager.get_instance().get_schedule_manager()

    if not schedule_mgr.week_label:
        schedule_mgr.initialize_week()
    elif _should_auto_reset(schedule_mgr):
        schedule_mgr.initialize_week()

    schedule_mgr.status_channel_id = settings.SCHEDULE_CHANNEL_ID
    await refresh_dashboard(guild, channel=channel, schedule_mgr=schedule_mgr)

    if _weekly_reset_task is not None:
        _weekly_reset_task.cancel()
    _weekly_reset_task = asyncio.create_task(_weekly_reset_loop(client))
    if _reminder_task is not None:
        _reminder_task.cancel()
    _reminder_task = asyncio.create_task(_reminder_loop(client))
