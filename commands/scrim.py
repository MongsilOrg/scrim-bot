"""
스크림 대시보드

지정 채널에 스크림 대시보드 메시지를 유지합니다.
봇 시작 시 기존 메시지를 찾아 뷰를 재등록하고, 없으면 새로 생성합니다.
매일 22시에 다음날 스크림으로 자동 전환합니다.
"""
import asyncio
from datetime import timedelta

import discord

from bot.client import ScrimBot
from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_current_kst_time, get_next_scrim_date

logger = get_logger('scrim')

SCRIM_CHANNEL_ID = settings.SCRIM_CHANNEL_ID

_daily_reset_task: asyncio.Task | None = None


def _is_scrim_expired(team_data_manager) -> bool:
    """스크림이 만료되었는지 확인합니다 (스크림 당일 22시 기준)."""
    from datetime import date

    if not team_data_manager.scrim_day or not team_data_manager.scrim_month:
        return True

    try:
        now = get_current_kst_time()
        today = now.date()
        scrim_date = date(now.year, team_data_manager.scrim_month, team_data_manager.scrim_day)

        if (scrim_date - today).days > 180:
            scrim_date = date(now.year - 1, team_data_manager.scrim_month, team_data_manager.scrim_day)

        if scrim_date < today:
            return True
        if scrim_date == today and now.hour >= 22:
            return True
        return False
    except ValueError:
        return True


async def _transition_to_next_scrim(client: ScrimBot, channel: discord.TextChannel) -> None:
    """다음날 스크림으로 전환합니다."""
    bot_manager = BotManager.get_instance()
    old_tdm = bot_manager.get_team_data_manager()
    old_msg_id = old_tdm.dashboard_message_id

    # 기존 MMR 메시지 삭제 (새 날에는 새 MMR 메시지 생성)
    if old_tdm.mmr_message:
        try:
            await old_tdm.mmr_message.delete()
        except Exception:
            pass
    elif old_tdm.mmr_message_id:
        try:
            old_mmr_msg = await channel.fetch_message(old_tdm.mmr_message_id)
            await old_mmr_msg.delete()
        except Exception:
            pass

    # 리셋 + 새 스크림 설정 (dashboard_message_id 보존)
    team_data_manager = await bot_manager.reset_team_data_manager(client)
    if old_msg_id:
        team_data_manager.dashboard_message_id = old_msg_id
    team_data_manager.scrim_channel_id = SCRIM_CHANNEL_ID

    date_info = get_next_scrim_date()
    await team_data_manager.initialize_new_scrim(
        scrim_day=date_info['day'],
        scrim_month=date_info['month'],
        scrim_channel_id=SCRIM_CHANNEL_ID,
    )

    # 대시보드 갱신 (메시지 ID가 확정된 후 백업)
    await _refresh_scrim_dashboard(channel)
    team_data_manager._save_backup()

    # 태스크 시작
    team_data_manager.auto_assignment_task = asyncio.create_task(
        team_data_manager.check_and_auto_assign()
    )
    team_data_manager.mmr_update_task = asyncio.create_task(
        team_data_manager.mmr_update_loop()
    )

    # MMR 메시지 생성 (팀이 있을 때만)
    if team_data_manager.teams:
        try:
            await team_data_manager.update_mmr_message(channel)
        except Exception as e:
            logger.error(f"[스크림] MMR 메시지 생성 실패: {e}", exc_info=True)

    logger.info(f"[스크림] 다음 스크림 전환 완료 — {date_info['month']}/{date_info['day']} ({date_info['weekday_name']})")


async def _daily_reset_loop(client: ScrimBot) -> None:
    """매일 22시에 다음날 스크림으로 자동 전환하는 백그라운드 태스크."""
    await client.wait_until_ready()
    while not client.is_closed():
        now = get_current_kst_time()

        # 오늘 22시까지 대기 (이미 지났으면 내일 22시)
        target = now.replace(hour=22, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        try:
            guild = client.guilds[0] if client.guilds else None
            if not guild:
                continue
            channel = guild.get_channel(SCRIM_CHANNEL_ID)
            if not channel:
                continue

            await _transition_to_next_scrim(client, channel)
        except Exception as e:
            logger.error(f"[스크림] 자동 전환 실패: {e}", exc_info=True)

        # 중복 실행 방지
        await asyncio.sleep(60)


async def _refresh_scrim_dashboard(channel: discord.TextChannel) -> None:
    """스크림 대시보드 메시지를 현재 상태로 갱신합니다."""
    from commands.ui.views import TeamInputView

    team_data_manager = BotManager.get_instance().get_team_data_manager()
    date_info = get_next_scrim_date()

    # 스크림 날짜가 없으면 자동 설정
    scrim_day = team_data_manager.scrim_day or date_info['day']
    scrim_month = team_data_manager.scrim_month or date_info['month']

    view = TeamInputView(
        scrim_day=scrim_day,
        scrim_month=scrim_month,
        scrim_weekday=date_info['weekday_name'],
    )

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
    if team_data_manager.scrim_day is not None and _is_scrim_expired(team_data_manager):
        await _transition_to_next_scrim(client, channel)
    elif team_data_manager.scrim_day is None:
        # 첫 실행이면 오늘/내일 스크림으로 설정
        await _transition_to_next_scrim(client, channel)
    else:
        # 활성 스크림 — 대시보드 갱신
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
    _daily_reset_task = asyncio.create_task(_daily_reset_loop(client))

    logger.info("[스크림] 대시보드 연동 완료")
