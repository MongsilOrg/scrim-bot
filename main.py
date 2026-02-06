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
from commands.warning import 주의부여, 경고부여
from config.logging_config import ScrimbotLogger
from config.settings import settings


async def main():
    """메인 함수"""
    try:
        # 로깅 시스템 초기화
        ScrimbotLogger.setup_logging(settings.LOG_LEVEL, settings.LOG_FILE)
        logger = ScrimbotLogger.get_logger("main")

        # 설정 검증
        if not settings.validate():
            logger.error("[시작] 필수 설정값 누락")
            return

        # Discord 클라이언트 생성
        client = ScrimBot()

        # BotManager에 클라이언트 설정
        BotManager.get_instance().set_client(client)

        # 이벤트 핸들러 등록
        @client.event
        async def on_ready():
            """봇 준비 완료 이벤트"""
            logger.info(f"[시작] 봇 준비 완료 - {client.user} 온라인")

            # BotManager에 클라이언트 재설정 (완전히 준비된 상태)
            bot_manager = BotManager.get_instance()
            bot_manager.set_client(client)

            # WarningManager 초기화 및 정리 태스크 시작
            warning_manager = bot_manager.get_warning_manager()
            if warning_manager.worksheet:
                warning_manager.start_cleanup_task()
                logger.info("[시작] 경고 관리 시스템 초기화 완료")

            # 명령어 동기화
            try:
                synced = await client.tree.sync(
                    guild=discord.Object(id=settings.GUILD_ID)
                )
                logger.info(f"[시작] 명령어 동기화 완료 - {len(synced)}개 명령어 등록됨")
            except Exception as e:
                logger.error(f"[시작] 명령어 동기화 실패: {e}", exc_info=True)

            # 시드 데이터 로드는 TeamDataManager에서 처리

        @client.event
        async def on_message(message):
            """메시지 이벤트 처리"""
            # 명령어 처리
            await client.process_commands(message)

            # 추가 메시지 처리
            await handle_message(message)

        # 명령어 등록
        @client.tree.command(
            name="방코드",
            description="방 코드를 공지합니다",
            guild=discord.Object(id=settings.GUILD_ID),
        )
        async def room_code_command(
            interaction: discord.Interaction, room_code: str
        ):
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

        # 컨텍스트 메뉴 명령어 등록
        @client.tree.context_menu(
            name="주의 부여", guild=discord.Object(id=settings.GUILD_ID)
        )
        async def caution_context_menu(
            interaction: discord.Interaction, user: discord.Member
        ):
            await 주의부여(interaction, user)

        @client.tree.context_menu(
            name="경고 부여", guild=discord.Object(id=settings.GUILD_ID)
        )
        async def warning_context_menu(
            interaction: discord.Interaction, user: discord.Member
        ):
            await 경고부여(interaction, user)

        # 앱 명령어 전역 에러 핸들러 (컨텍스트 메뉴 등)
        @client.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
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
