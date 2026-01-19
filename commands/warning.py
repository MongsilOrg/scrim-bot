"""
경고 관리 명령어
"""
import discord
from discord import Color, Embed

from config.logging_config import get_logger
from config.settings import settings
from ui.modals import WarningReasonModal

logger = get_logger('warning')


def _is_admin(user: discord.Member) -> bool:
    """사용자가 관리자인지 확인합니다"""
    return any(role.id in settings.ADMIN_ROLE_IDS for role in user.roles)


async def 주의부여(interaction: discord.Interaction, user: discord.Member) -> None:
    """주의 부여 컨텍스트 메뉴 핸들러"""
    try:
        admin_name = interaction.user.display_name or interaction.user.name
        target_name = user.display_name or user.name

        logger.info(f"[명령어] 주의 부여 시작 - 관리자: {admin_name}, 대상: {target_name} (ID: {user.id})")

        # 관리자 권한 확인
        if not _is_admin(interaction.user):
            logger.warning(f"[명령어] 주의 부여 권한 없음 - 사용자: {admin_name} (ID: {interaction.user.id})")
            error_embed = Embed(
                title="❌ 권한 없음",
                description="관리자 권한이 없습니다.",
                color=Color.red()
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return

        # 모달 표시
        modal = WarningReasonModal(user, '주의')
        await interaction.response.send_modal(modal)
        logger.debug(f"[명령어] 주의 부여 모달 표시 완료 - 대상: {target_name}")

    except Exception as e:
        logger.error(f"[명령어] 주의 부여 명령어 처리 실패: {e}", exc_info=True)
        error_embed = Embed(
            title="❌ 오류",
            description="명령어 처리 중 오류가 발생했습니다.",
            color=Color.red()
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
        else:
            await interaction.followup.send(embed=error_embed, ephemeral=True)


async def 경고부여(interaction: discord.Interaction, user: discord.Member) -> None:
    """경고 부여 컨텍스트 메뉴 핸들러"""
    try:
        admin_name = interaction.user.display_name or interaction.user.name
        target_name = user.display_name or user.name

        logger.info(f"[명령어] 경고 부여 시작 - 관리자: {admin_name}, 대상: {target_name} (ID: {user.id})")

        # 관리자 권한 확인
        if not _is_admin(interaction.user):
            logger.warning(f"[명령어] 경고 부여 권한 없음 - 사용자: {admin_name} (ID: {interaction.user.id})")
            error_embed = Embed(
                title="❌ 권한 없음",
                description="관리자 권한이 없습니다.",
                color=Color.red()
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
            return

        # 모달 표시
        modal = WarningReasonModal(user, '경고')
        await interaction.response.send_modal(modal)
        logger.debug(f"[명령어] 경고 부여 모달 표시 완료 - 대상: {target_name}")

    except Exception as e:
        logger.error(f"[명령어] 경고 부여 명령어 처리 실패: {e}", exc_info=True)
        error_embed = Embed(
            title="❌ 오류",
            description="명령어 처리 중 오류가 발생했습니다.",
            color=Color.red()
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
        else:
            await interaction.followup.send(embed=error_embed, ephemeral=True)

