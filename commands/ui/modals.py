"""
Discord Modal 컴포넌트들
"""
from typing import TYPE_CHECKING, Tuple, Union

import discord
from discord.components import CheckboxGroupOption
from discord.ui import CheckboxGroup, Label, Modal, TextInput

from config.logging_config import get_logger
from models.team_data import TeamData
from commands.team_pipeline import process_team_edit, process_team_registration
from utils.layout_helpers import processing_view, send_error_message

if TYPE_CHECKING:
    from .roster_views import GroupRosterView
    from .views import TeamInputView

logger = get_logger('modals')


def _parse_member_lines(text: str) -> list:
    """줄 단위 입력을 공백 제거한 멤버 리스트로 파싱합니다."""
    return [line.strip() for line in text.strip().split('\n') if line.strip()]


class TeamModal(Modal):
    """
    팀 정보 입력 모달

    새로운 팀 등록을 위한 폼을 제공합니다.
    팀명, 선수 3~4명, 스태프 최대 3명의 정보를 입력받습니다.
    """

    def __init__(self, user: discord.Member, default_team_name: str = "", default_players: str = "", default_staff: str = ""):
        super().__init__(title="팀 신청")
        self.user = user

        self.team_name_input = TextInput(
            label="팀명 (3~12글자, 한글/영어)",
            placeholder="예: Team ER",
            min_length=3,
            max_length=12,
            required=True,
            default=default_team_name or None,
        )
        self.add_item(self.team_name_input)

        self.players_input = TextInput(
            label="플레이어 (3~4명)",
            placeholder="한 줄에 하나씩 입력",
            max_length=200,
            required=True,
            style=discord.TextStyle.paragraph,
            default=default_players or None,
        )
        self.add_item(self.players_input)

        self.staff_input = TextInput(
            label="스태프 (선택사항)",
            placeholder="한 줄에 하나씩 입력",
            max_length=200,
            required=False,
            style=discord.TextStyle.paragraph,
            default=default_staff or None,
        )
        self.add_item(self.staff_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모달 제출 처리: 입력 수집 후 등록 파이프라인에 위임"""
        try:
            # 즉시 응답하여 모달을 닫음
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            # 임시 메시지 전송
            temp_message = await interaction.followup.send(view=processing_view("팀 정보를 확인하고 등록하고 있습니다."), ephemeral=True, wait=True)

            team_data = TeamData(
                name=self.team_name_input.value.strip(),
                players=_parse_member_lines(self.players_input.value),
                staff=_parse_member_lines(self.staff_input.value),
            )

            await process_team_registration(interaction, team_data, temp_message, submitter=self.user)

        except discord.NotFound:
            logger.warning("[모달] 팀 등록 interaction 만료")
        except Exception as e:
            logger.error(f"[모달] 팀 모달 제출 처리 실패: {e}", exc_info=True)
            await send_error_message(interaction, "팀 등록 중 오류가 발생했습니다.")


class TeamEditModal(Modal):
    """
    팀 정보 수정 모달
    
    기존 팀의 정보를 수정할 수 있는 폼을 제공합니다.
    팀명, 선수, 스태프 정보를 변경할 수 있으며, MMR 재계산 및 중복 검사를 수행합니다.
    """
    
    def __init__(
        self,
        view: Union['GroupRosterView', 'TeamInputView'],
        team_data: Tuple[str, TeamData, float],
        *,
        is_roster_change: bool,
    ):
        super().__init__(title="팀 정보 수정")
        self.view = view
        self.is_roster_change = is_roster_change
        self.original_team_name, self.original_team_data, self.original_mmr = team_data

        self.team_name_input = TextInput(
            label="팀명 (3~12글자, 한글/영어)",
            placeholder="예: Team ER",
            min_length=3,
            max_length=12,
            required=True,
            default=self.original_team_name
        )
        self.add_item(self.team_name_input)

        original_players = self.original_team_data.players
        players_text = '\n'.join(original_players)

        self.players_input = TextInput(
            label="플레이어 (3~4명)",
            placeholder="한 줄에 하나씩 입력",
            max_length=200,
            required=True,
            style=discord.TextStyle.paragraph,
            default=players_text
        )
        self.add_item(self.players_input)

        staff_text = '\n'.join(self.original_team_data.staff)

        self.staff_input = TextInput(
            label="스태프 (선택사항)",
            placeholder="한 줄에 하나씩 입력",
            max_length=200,
            required=False,
            style=discord.TextStyle.paragraph,
            default=staff_text
        )
        self.add_item(self.staff_input)

        # 로스터 변경 시 주의 부여 옵션 추가
        self.warning_checkbox = None
        self.warning_reason_input = None
        if is_roster_change:
            members = ', '.join(original_players)
            self.warning_checkbox = CheckboxGroup(
                options=[
                    CheckboxGroupOption(
                        label="주의 1회 부여",
                        value="yes",
                        description=f"{self.original_team_name} 선수 {len(original_players)}명: {members}",
                    ),
                ],
                required=False,
            )
            self.add_item(Label(text="주의 부여", component=self.warning_checkbox))
            self.warning_reason_input = TextInput(
                placeholder="주의 부여 시 사유",
                max_length=100,
                required=False,
                default="대타",
            )
            self.add_item(Label(text="사유", component=self.warning_reason_input))
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모달 제출 처리: 입력 수집 후 수정 파이프라인에 위임"""
        try:
            # 즉시 응답하여 모달을 닫음
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            # 임시 메시지 전송
            temp_message = await interaction.followup.send(view=processing_view("변경된 팀 정보를 확인하고 업데이트하고 있습니다."), ephemeral=True, wait=True)

            is_roster_change = self.is_roster_change

            new_team_data = TeamData(
                name=self.team_name_input.value.strip(),
                players=_parse_member_lines(self.players_input.value),
                staff=_parse_member_lines(self.staff_input.value),
            )

            # 주의 부여 입력 수집 (로스터 변경 전용)
            apply_warning = bool(is_roster_change and self.warning_checkbox and self.warning_checkbox.values)
            warning_reason = ""
            if self.warning_reason_input and self.warning_reason_input.value:
                warning_reason = self.warning_reason_input.value.strip()
            if not warning_reason:
                warning_reason = "대타"

            await process_team_edit(
                interaction,
                group_letter=getattr(self.view, 'group_letter', None),
                original_team_name=self.original_team_name,
                original_team_data=self.original_team_data,
                new_team_data=new_team_data,
                temp_message=temp_message,
                is_roster_change=is_roster_change,
                apply_warning=apply_warning,
                warning_reason=warning_reason,
            )

        except discord.NotFound:
            logger.warning("[모달] 팀 수정 interaction 만료")
        except Exception as e:
            logger.error(f"[모달] 팀 정보 수정 모달 제출 처리 실패: {e}", exc_info=True)
            await send_error_message(interaction, "팀 정보 수정 중 오류가 발생했습니다.")
