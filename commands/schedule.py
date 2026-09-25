import asyncio
from datetime import timedelta

from bot.client import ScrimBot
from bot.manager import BotManager
from commands.ui.schedule_views import refresh_dashboard
from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_current_kst_time

logger = get_logger('schedule')

_weekly_reset_task: asyncio.Task | None = None


def _should_auto_reset(schedule_mgr) -> bool:
    if not schedule_mgr.week_start:
        return False
    now = get_current_kst_time()
    deadline = schedule_mgr.week_start + timedelta(days=5, hours=settings.NEXT_SCRIM_OPEN_HOUR)
    return now >= deadline


async def _weekly_reset_loop(client: ScrimBot) -> None:
    await client.wait_until_ready()
    while not client.is_closed():
        now = get_current_kst_time()
        days_until_saturday = (5 - now.weekday()) % 7
        if days_until_saturday == 0 and now.hour >= settings.NEXT_SCRIM_OPEN_HOUR:
            days_until_saturday = 7

        next_saturday_22 = now.replace(
            hour=settings.NEXT_SCRIM_OPEN_HOUR, minute=0, second=0, microsecond=0
        ) + timedelta(days=days_until_saturday)

        wait_seconds = (next_saturday_22 - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        try:
            schedule_mgr = BotManager.get_instance().get_schedule_manager()
            schedule_mgr.initialize_week()

            guild = client.guilds[0] if client.guilds else None
            if guild:
                channel = guild.get_channel(settings.SCHEDULE_CHANNEL_ID)
                if channel:
                    await refresh_dashboard(guild, channel=channel, schedule_mgr=schedule_mgr)
            logger.info(f"[일정] 자동 주차 전환 완료 - 주차: {schedule_mgr.week_label}")
        except Exception as e:
            logger.error(f"[일정] 자동 주차 전환 실패: {e}", exc_info=True)

        await asyncio.sleep(60)


async def setup_schedule_dashboard(client: ScrimBot) -> None:
    global _weekly_reset_task

    guild = client.guilds[0] if client.guilds else None
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
