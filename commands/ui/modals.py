from typing import TYPE_CHECKING, Optional, Tuple, Union

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


MEMBER_INPUT_DESCRIPTION = "서버 별명과 게임 닉네임이 둘 다 이 이름과 같아야 합니다."
MEMBER_INPUT_PLACEHOLDER = "한 줄에 한 명씩 입력해주세요"


def _parse_member_lines(text: str) -> list:
    return [line.strip() for line in text.strip().split('\n') if line.strip()]


def _member_input(*, required: bool, default: str) -> TextInput:
    return TextInput(
        placeholder=MEMBER_INPUT_PLACEHOLDER,
        max_length=200,
        required=required,
        style=discord.TextStyle.paragraph,
        default=default or None,
    )


class TeamModal(Modal):
    def __init__(self, user: discord.Member, default_team_name: str = "", default_players: str = "", default_staff: str = ""):
        super().__init__(title="팀 신청")
        self.user = user

        self.team_name_input = TextInput(
            label="팀명 3~12글자",
            placeholder="한글과 영어만 가능, 예: Team ER",
            min_length=3,
            max_length=12,
            required=True,
            default=default_team_name or None,
        )
        self.add_item(self.team_name_input)

        self.players_input = _member_input(required=True, default=default_players)
        self.add_item(Label(text="선수 3~4명", description=MEMBER_INPUT_DESCRIPTION, component=self.players_input))

        self.staff_input = _member_input(required=False, default=default_staff)
        self.add_item(Label(text="스태프 0~3명", description=MEMBER_INPUT_DESCRIPTION, component=self.staff_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            temp_message = await interaction.followup.send(view=processing_view("팀 정보를 확인하고 있습니다."), ephemeral=True, wait=True)

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
            await send_error_message(interaction, "팀 신청 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")


class TeamEditModal(Modal):
    
    def __init__(
        self,
        view: Union['GroupRosterView', 'TeamInputView'],
        team_data: Tuple[str, TeamData, float],
        *,
        is_roster_change: bool,
        draft: Optional[dict] = None,
    ):
        """draft는 같은 팀의 실패한 수정 입력, 있으면 기본값으로 채움."""
        super().__init__(title="팀 정보 수정")
        self.view = view
        self.is_roster_change = is_roster_change
        self.original_team_name, self.original_team_data, self.original_mmr = team_data

        original_players = self.original_team_data.players
        source = draft or {
            "team_name": self.original_team_name,
            "players": original_players,
            "staff": self.original_team_data.staff,
        }

        self.team_name_input = TextInput(
            label="팀명 3~12글자",
            placeholder="한글과 영어만 가능, 예: Team ER",
            min_length=3,
            max_length=12,
            required=True,
            default=source["team_name"]
        )
        self.add_item(self.team_name_input)

        self.players_input = _member_input(required=True, default='\n'.join(source["players"]))
        self.add_item(Label(text="선수 3~4명", description=MEMBER_INPUT_DESCRIPTION, component=self.players_input))

        self.staff_input = _member_input(required=False, default='\n'.join(source["staff"]))
        self.add_item(Label(text="스태프 0~3명", description=MEMBER_INPUT_DESCRIPTION, component=self.staff_input))

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
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            temp_message = await interaction.followup.send(view=processing_view("팀 정보를 확인하고 있습니다."), ephemeral=True, wait=True)

            is_roster_change = self.is_roster_change

            new_team_data = TeamData(
                name=self.team_name_input.value.strip(),
                players=_parse_member_lines(self.players_input.value),
                staff=_parse_member_lines(self.staff_input.value),
            )

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
            await send_error_message(interaction, "팀 수정 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
