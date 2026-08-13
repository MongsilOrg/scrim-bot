"""봇 이벤트 핸들러"""

import asyncio
from typing import TYPE_CHECKING, List

import discord
from discord import app_commands

from bot.manager import BotManager
from commands.schedule import setup_schedule_dashboard
from commands.scrim import setup_scrim_dashboard
from utils.layout_helpers import error_view, image_response_view, custom_view, send_response
from config.logging_config import get_logger
from config.settings import settings
from services.image_generator import ImageGenerator
from services.score_aggregation import (
    CSVRow,
    aggregate_team_scores,
    collect_today_csv_data,
    is_csv_filename,
)
from utils.helpers import get_current_kst_time, get_group_letter, get_start_of_day_utc

if TYPE_CHECKING:
    from bot.client import ScrimBot
    from discord.ui import LayoutView

logger = get_logger('events')

# 점수표 첨부 이미지 파일명 (attachment:// 참조와 일치해야 함)
SCORE_IMAGE_FILENAME = 'score_table.png'

# on_ready는 재-IDENTIFY 시 재발화하므로 부트스트랩은 1회만 실행한다
_bootstrap_done = False


async def bootstrap_on_ready(client: "ScrimBot") -> None:
    """봇 준비 완료 시 초기 상태를 복구하고 명령어를 동기화합니다."""
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
            if not team_data_manager.is_team_assignment_started:
                current_time = get_current_kst_time()
                if (team_data_manager.is_scrim_date_today()
                        and current_time.hour >= settings.TEAM_REGISTRATION_DEADLINE_HOUR):
                    # 스크림 당일 마감 시각 이후 재시작: 조편성이 미완료면 즉시 실행
                    logger.info(f"[시작] {settings.TEAM_REGISTRATION_DEADLINE_HOUR}시 이후 재시작 - 조편성 미완료, 즉시 실행")
                    asyncio.create_task(team_data_manager.start_team_assignment())
                else:
                    # 태스크 재시작 (당일 마감 시각 전 또는 전날 밤)
                    team_data_manager.auto_assignment_task = asyncio.create_task(
                        team_data_manager.check_and_auto_assign()
                    )
                    team_data_manager.mmr_update_task = asyncio.create_task(
                        team_data_manager.mmr_update_loop()
                    )
                    logger.info("[시작] 조편성/MMR 태스크 재시작")
            else:
                # 조편성 후 복구: GroupRosterView 재등록
                await team_data_manager.restore_group_roster_views(client)
                logger.info("[시작] 조편성 후 복구 완료")
        else:
            logger.warning("[시작] 백업 복구 실패")
    else:
        team_data_manager.clear_backup()

    warning_manager = bot_manager.get_warning_manager()
    if warning_manager.worksheet:
        warning_manager.start_cleanup_task()
        logger.info("[시작] 경고 관리 시스템 초기화 완료")

    try:
        await setup_scrim_dashboard(client)
    except Exception as e:
        logger.error(f"[시작] 스크림 대시보드 연동 실패: {e}", exc_info=True)

    try:
        await setup_schedule_dashboard(client)
    except Exception as e:
        logger.error(f"[시작] 일정 대시보드 연동 실패: {e}", exc_info=True)

    try:
        synced = await client.tree.sync(guild=discord.Object(id=settings.GUILD_ID))
        logger.info(f"[시작] 명령어 동기화 완료 - {len(synced)}개 명령어 등록됨")
    except Exception as e:
        logger.error(f"[시작] 명령어 동기화 실패: {e}", exc_info=True)


async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    """앱 명령어 전역 에러 핸들러"""
    logger.error(f"[명령어] 앱 명령어 오류: {error}", exc_info=True)
    try:
        await send_response(interaction, error_view("명령어 처리 중 오류가 발생했습니다."))
    except Exception:
        pass


async def on_message(message: discord.Message) -> None:
    """메시지 이벤트 핸들러 (CSV 업로드 시 점수 합산 이미지 생성)"""
    if message.author.bot:
        return
    if not any(is_csv_filename(att.filename) for att in message.attachments):
        return

    try:
        await _process_csv_attachments(message)
    except Exception as e:
        logger.error(f"[이벤트] CSV 처리 실패: {e}", exc_info=True)


async def _process_csv_attachments(message: discord.Message) -> None:
    """오늘 업로드된 모든 CSV를 스캔해 점수를 합산하고 이미지를 전송합니다."""
    channel = message.channel
    now_kst = get_current_kst_time()
    start_utc = get_start_of_day_utc(now_kst)

    csv_data_list = await collect_today_csv_data(channel, start_utc)
    if not csv_data_list:
        return

    csv_data_list.sort(key=lambda x: x[0])
    current_round_count = len(csv_data_list)

    group_letter = get_group_letter(channel.id)
    group_info = f"{group_letter}조" if group_letter else "알 수 없음"
    date_str = now_kst.strftime('%m월 %d일')

    team_data = aggregate_team_scores(csv_data_list)
    if not team_data:
        logger.warning("[이벤트] 처리할 팀 데이터 없음")
        return

    img_buf = await ImageGenerator.generate_score_table_image_async(team_data)
    if not img_buf:
        logger.error("[이벤트] 점수표 이미지 생성 실패")
        return

    score_view = _build_score_view(current_round_count, group_info, date_str)
    score_file = discord.File(img_buf, filename=SCORE_IMAGE_FILENAME)
    await channel.send(view=score_view, file=score_file)

    if current_round_count == settings.TOTAL_ROUNDS:
        gameid_view = _build_gameid_view(csv_data_list, group_info, date_str)
        await channel.send(view=gameid_view)
        # 백업 채널에는 별도 LayoutView 인스턴스 생성 (View는 상태를 가지므로 재사용 불가)
        backup_gameid_view = _build_gameid_view(csv_data_list, group_info, date_str)
        await _send_gameid_to_backup_channel(backup_gameid_view)


def _build_score_view(current_round_count: int, group_info: str, date_str: str) -> 'LayoutView':
    title = f"📊 스크림 결과 - {current_round_count}R - {group_info} {date_str}"
    return image_response_view(title, "", f"attachment://{SCORE_IMAGE_FILENAME}", discord.Color.blue())


def _build_gameid_view(csv_data_list: List[CSVRow], group_info: str, date_str: str) -> 'LayoutView':
    lines = [f"**{round_num}R**: `{game_id}`" for round_num, (game_id, _, _) in enumerate(csv_data_list, 1)]
    return custom_view(
        f"🎮 GameId 정보 - {group_info} {date_str}",
        "\n".join(lines),
        discord.Color.blue(),
    )


async def _send_gameid_to_backup_channel(gameid_view) -> None:
    try:
        client = BotManager.get_instance().get_client()
        if not client:
            return
        backup_channel = client.get_channel(settings.BACKUP_ANALYSIS_CHANNEL_ID)
        if backup_channel:
            await backup_channel.send(view=gameid_view)
    except Exception as e:
        logger.error(f"[이벤트] 백업 채널 전송 실패: {e}", exc_info=True)
