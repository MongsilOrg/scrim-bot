"""
경고 관리 명령어
"""
import discord

from utils.layout_helpers import error_view, permission_error_view, send_response
from config.logging_config import get_logger
from commands.ui.warning_modals import WarningReasonModal
from utils.helpers import is_admin

logger = get_logger('warning')


async def 제재부여(interaction: discord.Interaction, user: discord.Member) -> None:
    """제재 부여 컨텍스트 메뉴 핸들러 (주의/경고 통합)"""
    try:
        admin_name = interaction.user.display_name or interaction.user.name
        target_name = user.display_name or user.name

        logger.debug(f"[명령어] 제재 부여 시작 - 관리자: {admin_name}, 대상: {target_name} (ID: {user.id})")

        if not is_admin(interaction.user):
            logger.warning(f"[명령어] 제재 부여 권한 없음 - 사용자: {admin_name} (ID: {interaction.user.id})")
            await send_response(interaction, permission_error_view())
            return

        # 통합 모달 표시
        modal = WarningReasonModal(user)
        await interaction.response.send_modal(modal)
        logger.debug(f"[명령어] 제재 부여 모달 표시 완료 - 대상: {target_name}")

    except Exception as e:
        logger.error(f"[명령어] 제재 부여 명령어 처리 실패: {e}", exc_info=True)
        await send_response(interaction, error_view("명령어 처리 중 오류가 발생했습니다."))
