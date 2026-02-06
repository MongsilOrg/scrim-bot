"""CSV 컨텍스트 메뉴 기반 조편성 실행"""
import io
import csv
from typing import List

import discord

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_current_kst_time

logger = get_logger("scrim_csv_assign")


async def 조편성_csv(interaction: discord.Interaction, message: discord.Message) -> None:
    """메시지의 CSV 첨부를 읽어 조편성을 실행합니다."""
    try:
        # 관리자 권한 확인
        if not any(role.id in settings.ADMIN_ROLE_IDS for role in interaction.user.roles):
            await _send_embed(interaction, "권한 오류", "관리자만 사용할 수 있습니다.", discord.Color.red())
            return

        # CSV 첨부 찾기 (첫 번째 CSV 사용)
        csv_attachment = None
        for att in message.attachments:
            if att.filename.lower().endswith(".csv"):
                csv_attachment = att
                break

        if not csv_attachment:
            await _send_embed(interaction, "CSV 없음", "선택한 메시지에서 CSV 첨부를 찾을 수 없습니다.", discord.Color.red())
            return

        # CSV 읽기
        content = await csv_attachment.read()
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))

        required_cols = ["team_name", "players", "staff"]
        if any(col not in reader.fieldnames for col in required_cols):
            await _send_embed(
                interaction,
                "CSV 형식 오류",
                f"필요한 컬럼이 없습니다: {', '.join(required_cols)}",
                discord.Color.red(),
            )
            return

        teams_payload = {}
        for row in reader:
            team_name = (row.get("team_name") or "").strip()
            players = [p.strip() for p in (row.get("players") or "").split(",") if p.strip()]
            staff = [s.strip() for s in (row.get("staff") or "").split(",") if s.strip()]

            if not team_name or not players:
                continue

            teams_payload[team_name] = {
                "players": players,
                "staff": staff,
            }

        if not teams_payload:
            await _send_embed(interaction, "팀 없음", "CSV에서 유효한 팀을 찾지 못했습니다.", discord.Color.red())
            return

        # 긴 작업 전에 defer로 응답 지연 (3초 타임아웃 방지)
        await interaction.response.defer(ephemeral=True)

        # 팀 데이터 매니저 리셋 후 팀 로드 (비동기 함수이므로 await 필요)
        bot_manager = BotManager.get_instance()
        team_data_manager = await bot_manager.reset_team_data_manager(interaction.client)

        # 스크림 날짜 설정 (오늘 날짜) - 비동기 함수이므로 await 필요
        now = get_current_kst_time()
        await team_data_manager.initialize_new_scrim(
            scrim_day=now.day,
            scrim_month=now.month,
            scrim_channel_id=interaction.channel_id
        )

        # 팀 추가
        from models.team_data import TeamData
        for team_name, data in teams_payload.items():
            team_obj = TeamData(name=team_name, players=data["players"], staff=data["staff"])
            # CSV 조편성은 관리자용이므로 시간 제한을 무시하도록 오버라이드
            await team_data_manager.add_team(
                team_name,
                team_obj,
                interaction.user,
                allow_admin_override=True
            )

        # MMR 갱신 및 메시지 업데이트 (조편성 실행 전에 수행)
        try:
            await team_data_manager._update_all_team_mmr()
            await team_data_manager.update_mmr_message(interaction.channel)
        except Exception as e:
            logger.error(f"[명령어] MMR 갱신 실패: {e}", exc_info=True)

        # 조편성 실행
        total_teams, _, spare_teams = team_data_manager.get_team_counts()
        await team_data_manager._start_team_assignment(total_teams, spare_teams)

        # defer 후에는 followup으로 응답
        await interaction.followup.send(
            embed=discord.Embed(
                title="조편성 시작",
                description=f"CSV로 불러온 팀 {len(teams_payload)}개로 조편성을 시작했습니다.",
                color=discord.Color.green()
            ),
            ephemeral=True
        )

    except Exception as e:
        logger.error(f"[명령어] CSV 조편성 실패: {e}", exc_info=True)
        # defer 후에는 followup으로 응답
        if interaction.response.is_done():
            await interaction.followup.send(
                embed=discord.Embed(
                    title="오류",
                    description="조편성 중 오류가 발생했습니다.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
        else:
            await _send_embed(interaction, "오류", "조편성 중 오류가 발생했습니다.", discord.Color.red())


async def _send_embed(
    interaction: discord.Interaction,
    title: str,
    description: str,
    color: discord.Color,
    *,
    ephemeral: bool = True,
):
    embed = discord.Embed(title=title, description=description, color=color)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

