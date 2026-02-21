"""
스크림봇 메인 진입점

Discord 봇의 메인 실행 파일로, 봇 클라이언트 관리, 명령어 등록,
이벤트 처리 등의 핵심 기능을 담당합니다.
BotManager 클래스를 통해 봇의 생명주기를 관리하고,
클라이언트 재생성 및 MMR 업데이트 대기열을 처리합니다.
"""
import asyncio
import os
import sys

import discord
from discord import app_commands

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.client import ScrimBot
from bot.events import on_message as handle_message
from bot.manager import BotManager
from commands.room_code import 방코드
from commands.scrim import 스크림
from commands.scrim_csv_assign import 조편성_csv
from commands.warning import 제재부여
from config.logging_config import ScrimbotLogger
from config.settings import settings


async def _bootstrap_on_ready(client: ScrimBot, logger) -> None:
    """봇 준비 완료 시 초기 상태를 복구하고 명령어를 동기화합니다."""
    logger.info(f"[시작] 봇 준비 완료 - {client.user} 온라인")

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
                from utils.helpers import get_current_kst_time
                current_time = get_current_kst_time()
                is_scrim_day = (
                    current_time.day == team_data_manager.scrim_day
                    and current_time.month == team_data_manager.scrim_month
                )
                if is_scrim_day and current_time.hour >= 17:
                    logger.info("[시작] 17시 이후 재시작 - 자동 조편성 태스크 건너뜀")
                else:
                    team_data_manager.auto_assignment_task = asyncio.create_task(
                        team_data_manager.check_and_auto_assign()
                    )
                    logger.info("[시작] 자동 조편성 태스크 재시작")
                team_data_manager.mmr_update_task = asyncio.create_task(
                    team_data_manager.mmr_update_loop()
                )
                logger.info("[시작] MMR 갱신 태스크 재시작")
        else:
            logger.warning("[시작] 백업 복구 실패")
    else:
        team_data_manager.clear_backup()

    warning_manager = bot_manager.get_warning_manager()
    if warning_manager.worksheet:
        warning_manager.start_cleanup_task()
        logger.info("[시작] 경고 관리 시스템 초기화 완료")

    try:
        synced = await client.tree.sync(guild=discord.Object(id=settings.GUILD_ID))
        logger.info(f"[시작] 명령어 동기화 완료 - {len(synced)}개 명령어 등록됨")
    except Exception as e:
        logger.error(f"[시작] 명령어 동기화 실패: {e}", exc_info=True)


async def _process_incoming_message(client: ScrimBot, message: discord.Message) -> None:
    """메시지 파이프라인: 명령어 처리 후 CSV 후처리를 실행합니다."""
    await client.process_commands(message)
    await handle_message(message)


async def _on_app_command_error(
    logger,
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    """앱 명령어 전역 에러 핸들러"""
    logger.error(f"[시작] 앱 명령어 오류: {error}", exc_info=True)
    try:
        error_embed = discord.Embed(
            title="❌ 오류",
            description="명령어 처리 중 오류가 발생했습니다.",
            color=discord.Color.red()
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
        else:
            await interaction.followup.send(embed=error_embed, ephemeral=True)
    except Exception:
        pass


def _register_client_events(client: ScrimBot, logger) -> None:
    """클라이언트 이벤트 핸들러를 등록합니다."""
    @client.event
    async def on_ready():
        await _bootstrap_on_ready(client, logger)

    @client.event
    async def on_message(message):
        await _process_incoming_message(client, message)

    @client.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        await _on_app_command_error(logger, interaction, error)


def _register_app_commands(client: ScrimBot) -> None:
    """앱 명령어/컨텍스트 메뉴를 등록합니다."""
    @client.tree.command(
        name="방코드",
        description="방 코드를 공지합니다",
        guild=discord.Object(id=settings.GUILD_ID),
    )
    async def room_code_command(interaction: discord.Interaction, room_code: str):
        await 방코드(interaction, room_code)

    @client.tree.command(
        name="스크림",
        description="스크림을 시작합니다",
        guild=discord.Object(id=settings.GUILD_ID),
    )
    async def scrim_command(interaction: discord.Interaction):
        await 스크림(interaction)

    @client.tree.context_menu(
        name="조편성", guild=discord.Object(id=settings.GUILD_ID)
    )
    async def assign_csv_context(
        interaction: discord.Interaction, message: discord.Message
    ):
        await 조편성_csv(interaction, message)

    @client.tree.context_menu(
        name="제재 부여", guild=discord.Object(id=settings.GUILD_ID)
    )
    async def sanction_context_menu(
        interaction: discord.Interaction, user: discord.Member
    ):
        await 제재부여(interaction, user)


async def main():
    """메인 함수"""
    try:
        # 로깅 시스템 초기화
        ScrimbotLogger.setup_logging(settings.LOG_LEVEL, settings.LOG_FILE)
        logger = ScrimbotLogger.get_logger("main")

        # 설정 검증
        is_valid, config_errors = settings.validate()
        if not is_valid:
            logger.error("[시작] 봇 필수 설정값 누락:")
            for err in config_errors:
                logger.error(f"  - {err}")
            return

        # Discord 클라이언트 생성
        client = ScrimBot()

        # BotManager에 클라이언트 설정
        BotManager.get_instance().set_client(client)

        _register_client_events(client, logger)
        _register_app_commands(client)

        # 봇 실행
        await client.start(settings.DISCORD_TOKEN)

    except Exception as e:
        logger.error(f"[시작] 봇 실행 실패: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("봇이 종료되었습니다.")
    except Exception as e:
        print(f"봇 실행 중 오류 발생: {e}")
