"""
일정 명령어

/일정 단일 명령어로 주간 일정 등록, 불참 사유 입력, 현황 표시, 자동 편성을 통합 관리합니다.
"""
import discord

from bot.manager import BotManager
from config.logging_config import get_logger
from commands.ui.layout_helpers import error_view, permission_error_view, send_response
from commands.ui.views import ScheduleView, _refresh_schedule_status
from utils.helpers import is_admin

logger = get_logger('schedule')


async def 일정(interaction: discord.Interaction) -> None:
    """일정 명령어 처리

    주간 일정이 없으면 자동으로 초기화하고,
    현황 + 액션 버튼을 포함한 ScheduleView를 표시합니다.
    """
    try:
        if not is_admin(interaction.user):
            await send_response(interaction, permission_error_view())
            return

        schedule_mgr = BotManager.get_instance().get_schedule_manager()

        # 주간 일정이 없으면 자동 초기화
        if not schedule_mgr.week_label:
            schedule_mgr.initialize_week()

        # 관리자 목록 수집
        guild = interaction.guild
        if not guild:
            await send_response(interaction, error_view("서버 정보를 가져올 수 없습니다."))
            return

        from models.schedule_manager import EXCLUDED_USER_IDS
        all_admins: list[tuple[str, str]] = []
        for member in guild.members:
            if is_admin(member) and not member.bot and member.id not in EXCLUDED_USER_IDS:
                all_admins.append((str(member.id), member.display_name))

        status_text = schedule_mgr.get_status_text(all_admins)
        has_assignments = bool(schedule_mgr.assignments)

        view = ScheduleView(status_text, has_assignments=has_assignments)

        # 응답 전송 (채널에 공개 메시지로)
        if not interaction.response.is_done():
            await interaction.response.send_message(view=view)
            msg = await interaction.original_response()
        else:
            msg = await interaction.followup.send(view=view, wait=True)

        # 상태 메시지 참조 저장
        schedule_mgr.status_message_id = msg.id
        schedule_mgr.status_channel_id = interaction.channel_id
        schedule_mgr._save_backup()

    except Exception as e:
        logger.error(f"[명령어] 일정 명령어 처리 실패: {e}", exc_info=True)
        await send_response(interaction, error_view("일정 명령어 처리 중 오류가 발생했습니다."))
