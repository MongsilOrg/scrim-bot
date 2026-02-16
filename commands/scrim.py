"""
스크림 명령어
"""
import discord
from discord import Color, Embed

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from ui.views import TeamInputView
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
        
        # 전역 team_data_manager 새로 생성하여 이전 스크림 데이터 완전 초기화
        # 이전 태스크가 완전히 종료될 때까지 대기합니다
        bot_manager = BotManager.get_instance()
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
        embed = _create_scrim_embed(scrim_day, scrim_month, scrim_weekday, date_info)
        
        # 팀 입력 뷰 생성
        view = TeamInputView(embed)
        
        # 응답 전송
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




def _create_scrim_embed(day: int, month: int, weekday: str, date_info: dict = None) -> Embed:
    """스크림 임베드를 생성합니다"""
    # 날짜 정보를 title에 포함
    title = f"🏆 스크림 참가 신청 - {month}/{day} ({weekday})"

    embed = Embed(
        title=title,
        color=Color.green()
    )

    # 핵심 정보만 간결하게
    embed.add_field(
        name="📅 스크림 정보",
        value="• 조 편성: `17:00`\n"
              "• 스크림: `20:00 ~ 4R`",
        inline=True
    )

    embed.set_footer(text="ER Scrim", icon_url=settings.THUMBNAIL_URL)

    return embed


async def _send_error_message(interaction: discord.Interaction, message: str) -> None:
    """에러 메시지를 전송합니다."""
    try:
        error_embed = Embed(
            title="오류",
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
