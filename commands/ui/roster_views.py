from typing import TYPE_CHECKING, List, Optional, Tuple

import discord
from discord import ButtonStyle, Color, SelectOption
from discord.ui import ActionRow, Button, Container, LayoutView, MediaGallery, Select, Separator, TextDisplay

from config.logging_config import get_logger
from config.settings import settings
from utils.layout_helpers import (
    error_view,
    permission_error_view,
    send_response, FOOTER_TEXT,
    send_error_message,
)
from utils.helpers import is_admin

from .modals import TeamEditModal

if TYPE_CHECKING:
    from models.team_data import TeamData

logger = get_logger('roster_views')


def _team_name_line(team_names: List[str]) -> str:
    def code(name: str) -> str:
        return f"`` {name} ``" if '`' in name else f"`{name}`"

    return "  ".join(code(n) for n in team_names)


class GroupRosterView(LayoutView):

    def __init__(
        self,
        group_letter: str,
        group_teams: List[Tuple[str, 'TeamData', float]],
        *,
        message_text: str = "",
        has_image: bool = True,
    ):
        super().__init__(timeout=None)
        self.group_letter = group_letter
        self.group_teams = group_teams
        self.message_text = message_text
        self.has_image = has_image

        children: list = [TextDisplay(content=message_text)]
        if has_image:
            # discord_service가 이 모듈을 최상단에서 import해서 순환
            from services.discord_service import GROUP_IMAGE_FILENAME
            children.append(MediaGallery(discord.MediaGalleryItem(media=f"attachment://{GROUP_IMAGE_FILENAME}")))
        if group_teams:
            children.append(TextDisplay(content=_team_name_line([name for name, _, _ in group_teams])))
        children.append(Separator())
        children.append(TextDisplay(content=FOOTER_TEXT))
        self.add_item(Container(*children, accent_colour=Color.blue()))

        self.roster_change_button = Button(
            label="로스터 변경",
            style=ButtonStyle.primary,
            emoji="✏️",
            custom_id=f"roster_change_{group_letter}"
        )
        self.roster_change_button.callback = self.roster_change_callback
        self.add_item(ActionRow(self.roster_change_button))

    async def roster_change_callback(self, interaction: discord.Interaction) -> None:
        try:
            if not is_admin(interaction.user):
                await send_response(interaction, permission_error_view())
                return

            team_selection_view = TeamSelectionView(self)
            await send_response(interaction, team_selection_view)

        except discord.InteractionResponded:
            pass
        except discord.NotFound:
            await self._recreate_view_on_message(interaction)
        except Exception as e:
            logger.error(f"[뷰] 로스터 변경 콜백 처리 실패: {e}", exc_info=True)
            await send_error_message(interaction, "로스터 변경 중 오류가 발생했습니다.")

    async def _recreate_view_on_message(self, interaction: discord.Interaction) -> None:
        try:
            if hasattr(interaction, 'message') and interaction.message:
                new_view = GroupRosterView(
                    self.group_letter, self.group_teams,
                    message_text=self.message_text, has_image=self.has_image,
                )
                await interaction.message.edit(view=new_view)
        except Exception as e:
            logger.error(f"[뷰] View 재생성 실패: {e}", exc_info=True)


def build_rest_day_guide_view(team_name: str, user_id: Optional[str] = None) -> LayoutView:
    mention = f"<@{user_id}>\n" if user_id else ""
    notice = (
        f"{mention}"
        "📢 **공휴일/주말 스크림 자율 진행 안내**\n"
        "공휴일 및 주말 스크림의 경우 레이팅컷에 따른 조 편성만 제공합니다.\n"
        "아래 링크를 확인한 뒤 참여해주세요.\n\n"
        f"`{team_name}` 팀은 사설방 개설 후 양식에 맞춰 업로드해주세요.\n"
        f"{settings.CUSTOM_GAME_GUIDE_LINK}\n\n"
        "공휴일/주말 스크림 간에 발생한 문제는 당일 중으로 문의주셔야 원활한 처리가 가능하니 참고 부탁드립니다."
    )
    view = LayoutView(timeout=None)
    view.add_item(Container(
        TextDisplay(content=notice),
        Separator(),
        TextDisplay(content=FOOTER_TEXT),
        accent_colour=Color.orange(),
    ))
    return view


class TeamSelectionView(LayoutView):

    def __init__(self, parent_view: 'GroupRosterView'):
        super().__init__(timeout=None)
        self.parent_view = parent_view
        self.is_empty = not parent_view.group_teams

        self.add_item(Container(
            TextDisplay(content="## 팀 선택\n변경할 팀을 선택해주세요."),
            Separator(),
            TextDisplay(content=FOOTER_TEXT),
            accent_colour=Color.blue(),
        ))

        if self.is_empty:
            options = [SelectOption(label="등록된 팀이 없습니다", value="_empty", description="팀이 등록되면 선택 가능합니다")]
        else:
            options = [
                SelectOption(
                    label=f"{i+1}. {team_name} (MMR: {mmr:.2f})",
                    value=team_name,
                    description=f"팀원: {', '.join(team_data.players[:3]) or '정보 없음'}"
                )
                for i, (team_name, team_data, mmr) in enumerate(parent_view.group_teams)
            ]

        self.team_select = Select(
            placeholder="변경할 팀을 선택해주세요",
            options=options,
            disabled=self.is_empty
        )
        self.team_select.callback = self.team_select_callback
        self.add_item(ActionRow(self.team_select))

    async def team_select_callback(self, interaction: discord.Interaction) -> None:
        try:
            selected_team = self.team_select.values[0]

            selected_team_data = None
            for team_name, team_data, mmr in self.parent_view.group_teams:
                if team_name == selected_team:
                    selected_team_data = (team_name, team_data, mmr)
                    break

            if not selected_team_data:
                await send_response(interaction, error_view("선택된 팀 정보를 찾을 수 없습니다."))
                return

            if interaction.response.is_done():
                logger.warning("[뷰] 이미 응답된 interaction - 팀 수정 모달 표시 불가")
                return
            await interaction.response.send_modal(
                TeamEditModal(self.parent_view, selected_team_data, is_roster_change=True)
            )

        except discord.InteractionResponded:
            pass
        except discord.NotFound:
            pass
        except Exception as e:
            logger.error(f"[뷰] 팀 선택 콜백 처리 실패: {e}", exc_info=True)
            try:
                await send_response(interaction, error_view("팀 선택 중 오류가 발생했습니다."))
            except Exception as e2:
                logger.error(f"[뷰] 에러 메시지 전송 실패: {e2}", exc_info=True)
