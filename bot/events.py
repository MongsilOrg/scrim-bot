from typing import TYPE_CHECKING

import discord
from discord import app_commands

from bot.manager import BotManager
from commands.schedule import setup_schedule_dashboard
from commands.ticket_cleanup import setup_ticket_cleanup
from commands.scrim import setup_scrim_dashboard
from utils.layout_helpers import error_view, send_response
from config.logging_config import get_logger
from config.settings import settings
from services import labs_api
from utils.helpers import get_current_kst_time, get_group_letter, is_assignment_window, is_csv_filename

if TYPE_CHECKING:
    from bot.client import ScrimBot

logger = get_logger('events')

# on_ready는 재-IDENTIFY 때 재발화
_bootstrap_done = False


async def bootstrap_on_ready(client: "ScrimBot") -> None:
    global _bootstrap_done
    logger.info(f"[시작] 봇 준비 완료 - {client.user} 온라인")

    if _bootstrap_done:
        logger.info("[시작] 재연결로 인한 on_ready 재발화 - 부트스트랩 건너뜀")
        return
    _bootstrap_done = True

    bot_manager = BotManager.get_instance()
    bot_manager.set_client(client)

    team_data_manager = bot_manager.get_team_data_manager()
    if team_data_manager.should_restore_backup():
        if team_data_manager.load_backup():
            logger.info(
                f"[시작] 백업 복구 완료 - {len(team_data_manager.teams)}개 팀, "
                f"스크림 날짜: {team_data_manager.scrim_month}/{team_data_manager.scrim_day}"
            )
            await _resume_after_restore(client, team_data_manager)
        else:
            logger.error("[시작] 백업 복구 실패")

    # 시트 연결 재시도는 정리 루프와 사용 시점 몫
    bot_manager.get_warning_manager().start_cleanup_task()

    try:
        await setup_scrim_dashboard(client)
    except Exception as e:
        logger.error(f"[시작] 스크림 대시보드 연동 실패: {e}", exc_info=True)

    try:
        await setup_schedule_dashboard(client)
    except Exception as e:
        logger.error(f"[시작] 일정 대시보드 연동 실패: {e}", exc_info=True)

    setup_ticket_cleanup(client)
    labs_api.resume_backups(client)

    try:
        synced = await client.tree.sync(guild=discord.Object(id=settings.GUILD_ID))
        logger.info(f"[시작] 명령어 동기화 완료 - {len(synced)}개 명령어 등록됨")
    except Exception as e:
        logger.error(f"[시작] 명령어 동기화 실패: {e}", exc_info=True)


async def _resume_after_restore(client: "ScrimBot", team_data_manager) -> None:
    current_time = get_current_kst_time()
    started = team_data_manager.is_team_assignment_started

    if started and team_data_manager.groups:
        await team_data_manager.restore_group_roster_views(client)

    last = team_data_manager.last_auto_assignment
    done_today = bool(last and last.date() == current_time.date())
    if (team_data_manager.is_scrim_date_today(current_time)
            and is_assignment_window(current_time) and not done_today):
        logger.info("[시작] 조편성 시간 중 재시작 - 남은 조편성 바로 실행")
        team_data_manager.spawn_task(team_data_manager.start_team_assignment())
    elif not started:
        team_data_manager.start_background_tasks()
        logger.info("[시작] 조편성 대기와 MMR 갱신 태스크 시작")
    else:
        logger.info("[시작] 조편성 후 복구 완료")


async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    logger.error(f"[명령어] 앱 명령어 오류: {error}", exc_info=True)
    try:
        await send_response(interaction, error_view("명령어 처리 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요."))
    except Exception:
        pass


async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    if not any(is_csv_filename(att.filename) for att in message.attachments):
        return
    group_letter = get_group_letter(message.channel.id)
    if not group_letter:
        return
    if labs_api.linked_today(group_letter):
        client = BotManager.get_instance().get_client()
        if client:
            labs_api.schedule_backup(client, group_letter.upper())
