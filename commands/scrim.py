"""
스크림 명령어
"""
import discord
from discord import Color, Embed

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from commands.ui.views import ScrimResetConfirmView, TeamInputView
from utils.helpers import get_current_kst_time, get_next_scrim_date, is_admin

logger = get_logger('scrim')


async def 스크림(interaction: discord.Interaction) -> None:
    """스크림 명령어 처리"""
    try:
        # 관리자 권한 확인
        if not is_admin(interaction.user):
            await _send_error_message(interaction, "관리자 권한이 없습니다.")
            return
        
        # 현재 시간 기준으로 다음 스크림 날짜 자동 계산
        current_time = get_current_kst_time()
        date_info = get_next_scrim_date(current_time)
        scrim_day = date_info["day"]
        scrim_month = date_info["month"]
        scrim_weekday = date_info["weekday_name"]

        bot_manager = BotManager.get_instance()

        # 진행 중인 스크림이 있으면 확인 요청 (22시 이후면 자동 초기화)
        existing_tdm = bot_manager.get_team_data_manager()
        if existing_tdm and existing_tdm.teams:
            if not _is_scrim_expired(existing_tdm, current_time):
                confirm_embed = Embed(
                    title="⚠️ 진행 중인 스크림이 있습니다",
                    description=(
                        f"현재 **{len(existing_tdm.teams)}개 팀**이 등록되어 있습니다.\n"
                        f"초기화하면 모든 팀 데이터가 삭제됩니다.\n\n"
                        f"초기화하시겠습니까?"
                    ),
                    color=Color.orange()
                )
                confirm_view = ScrimResetConfirmView()
                await interaction.response.send_message(embed=confirm_embed, view=confirm_view, ephemeral=True)
                confirm_view.message = await interaction.original_response()

                # 사용자 응답 대기
                await confirm_view.wait()
                if not confirm_view.confirmed:
                    return

        # 전역 team_data_manager 새로 생성하여 이전 스크림 데이터 완전 초기화
        # 이전 태스크가 완전히 종료될 때까지 대기합니다
        team_data_manager = await bot_manager.reset_team_data_manager(interaction.client)
        
        # 새 스크림 세팅 (이전 데이터가 완전히 초기화된 후 실행)
        await team_data_manager.initialize_new_scrim(
            scrim_day=scrim_day,
            scrim_month=scrim_month,
            scrim_channel_id=interaction.channel_id
        )
        
        
        # 자동 조편성 태스크 시작
        import asyncio
        team_data_manager.auto_assignment_task = asyncio.create_task(
            team_data_manager.check_and_auto_assign()
        )
        
        # MMR 업데이트 루프 시작
        team_data_manager.mmr_update_task = asyncio.create_task(
            team_data_manager.mmr_update_loop()
        )
        
        # 스크림 임베드 생성
        embed = _create_scrim_embed(scrim_day, scrim_month, scrim_weekday)

        # 팀 입력 뷰 생성
        view = TeamInputView(embed)

        # 응답 전송 (확인 뷰로 이미 응답한 경우 channel.send 사용)
        if interaction.response.is_done():
            await interaction.channel.send(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view)
        
        # MMR 메시지 즉시 전송 (팀 여부와 상관없이)
        try:
            # 조편성이 시작되지 않은 경우에만 MMR 갱신
            if not team_data_manager.is_team_assignment_started:
                # 팀이 있는 경우 MMR 갱신도 함께 진행
                if team_data_manager.teams:
                    await team_data_manager._update_all_team_mmr()
                
                await team_data_manager.update_mmr_message(interaction.channel)
        except Exception as e:
            logger.error(f"[명령어] MMR 갱신 실패: {e}", exc_info=True)
        
    except Exception as e:
        logger.error(f"[명령어] 스크림 명령어 처리 실패: {e}", exc_info=True)
        await _send_error_message(interaction, "스크림 명령어 처리 중 오류가 발생했습니다.")




def _is_scrim_expired(team_data_manager, current_time) -> bool:
    """이전 스크림이 만료되었는지 확인합니다 (스크림 당일 22시 기준)."""
    from datetime import date

    if not team_data_manager.scrim_day or not team_data_manager.scrim_month:
        return True

    try:
        today = current_time.date()
        scrim_date = date(current_time.year, team_data_manager.scrim_month, team_data_manager.scrim_day)

        # 스크림 날짜가 6개월 이상 미래이면 작년 스크림으로 판단
        if (scrim_date - today).days > 180:
            scrim_date = date(current_time.year - 1, team_data_manager.scrim_month, team_data_manager.scrim_day)

        if scrim_date < today:
            return True
        if scrim_date == today and current_time.hour >= 22:
            return True
        return False
    except ValueError:
        return True


def _create_scrim_embed(day: int, month: int, weekday: str) -> Embed:
    """스크림 임베드를 생성합니다"""
    embed = Embed(
        title=f"🏆 {month}/{day} ({weekday}) 스크림",
        description=(
            "⏰  `17:00` 조편성 · `20:00` 스크림 (4R)\n"
            "아래 버튼으로 팀을 등록해주세요."
        ),
        color=Color.green()
    )

    embed.set_footer(text=settings.EMBED_FOOTER_TEXT, icon_url=settings.THUMBNAIL_URL)

    return embed


async def _send_error_message(interaction: discord.Interaction, message: str) -> None:
    """에러 메시지를 전송합니다."""
    try:
        error_embed = Embed(
            title="❌ 오류",
            description=message,
            color=Color.red()
        )

        # 상호작용이 이미 응답되었는지 확인
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
        else:
            # 이미 응답된 경우 followup으로 전송
            await interaction.followup.send(embed=error_embed, ephemeral=True)
    except Exception as e:
        logger.error(f"[명령어] 에러 메시지 전송 실패: {e}", exc_info=True)
        # 최후의 수단으로 일반 메시지 전송 시도
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"오류: {message}", ephemeral=True)
            else:
                await interaction.followup.send(f"오류: {message}", ephemeral=True)
        except Exception as e2:
            logger.error(f"[명령어] 최후의 수단 메시지 전송 실패: {e2}", exc_info=True)
