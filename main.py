"""
스크림봇 메인 진입점

클라이언트를 생성하고 이벤트/앱 명령어를 등록한 뒤 봇을 실행합니다.
"""
import asyncio
import os
import sys

import sentry_sdk

def _sentry_before_send(event, hint):
    """일시적 네트워크 에러는 Sentry로 보내지 않는다."""
    exc_info = hint.get("exc_info")
    if exc_info:
        name = getattr(exc_info[0], "__name__", "")
        msg = str(exc_info[1])
        if name in ("TimeoutError", "ConnectTimeoutError", "ReadTimeout", "ConnectionError", "ClientConnectorError", "ClientOSError", "ServerDisconnectedError", "WSServerHandshakeError", "ConnectionClosed", "ConnectionResetError"):
            return None
        for _t in ("Connection timeout", "Cannot connect to host", "Temporary failure in name resolution", "네트워크 오류", "연결 중 오류"):
            if _t in msg:
                return None
    return event


sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN", ""),
    traces_sample_rate=0.1,
    environment="production", before_send=_sentry_before_send,
)

import discord

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.client import ScrimBot
from bot.events import bootstrap_on_ready, on_app_command_error, on_message as handle_message
from bot.manager import BotManager
from commands.room_code import 방코드
from commands.warning import 제재부여
from config.logging_config import ScrimbotLogger
from config.settings import settings


def _register_client_events(client: ScrimBot) -> None:
    """클라이언트 이벤트 핸들러를 등록합니다."""
    @client.event
    async def on_ready():
        await bootstrap_on_ready(client)

    @client.event
    async def on_message(message):
        await handle_message(message)

    client.tree.error(on_app_command_error)


def _register_app_commands(client: ScrimBot) -> None:
    """앱 명령어/컨텍스트 메뉴를 등록합니다."""
    @client.tree.command(
        name="방코드",
        description="방 코드를 공지합니다",
        guild=discord.Object(id=settings.GUILD_ID),
    )
    async def room_code_command(interaction: discord.Interaction, room_code: str):
        await 방코드(interaction, room_code)

    @client.tree.context_menu(
        name="제재 부여", guild=discord.Object(id=settings.GUILD_ID)
    )
    async def sanction_context_menu(
        interaction: discord.Interaction, user: discord.Member
    ):
        await 제재부여(interaction, user)


async def main():
    try:
        ScrimbotLogger.setup_logging(settings.LOG_LEVEL, settings.LOG_FILE)
        logger = ScrimbotLogger.get_logger("main")

        is_valid, config_errors = settings.validate()
        if not is_valid:
            logger.error("[시작] 봇 필수 설정값 누락:")
            for err in config_errors:
                logger.error(f"  - {err}")
            return

        client = ScrimBot()
        BotManager.get_instance().set_client(client)

        _register_client_events(client)
        _register_app_commands(client)

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
