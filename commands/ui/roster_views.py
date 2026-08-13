"""
로스터 계열 Discord View 컴포넌트들

조편성 후 조별 공지에 붙는 로스터 관리 뷰(GroupRosterView, TeamSelectionView)와
공휴일/일요일 자율 진행 안내 뷰를 담당합니다.
대시보드 계열 뷰(TeamInputView 등)는 commands/ui/views.py에 있습니다.
"""
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


class GroupRosterView(LayoutView):
    """
    조별 로스터 관리 뷰

    조별 공지에서 팀 로스터 변경 기능을 제공합니다.
    관리자만 접근 가능하며, 드롭다운을 통해 팀을 선택하고 수정할 수 있습니다.
    """

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

        # Container (공지 텍스트 + 이미지 + 푸터)
        children: list = [TextDisplay(content=message_text)]
        if has_image:
            # 파일명 단일 출처. discord_service 가 이 모듈을 최상단 import 하므로
            # 순환을 피해 함수 스코프에서 가져온다
            from services.discord_service import GROUP_IMAGE_FILENAME
            children.append(MediaGallery(discord.MediaGalleryItem(media=f"attachment://{GROUP_IMAGE_FILENAME}")))
        children.append(Separator())
        children.append(TextDisplay(content=FOOTER_TEXT))
        self.add_item(Container(*children, accent_colour=Color.blue()))

        # ActionRow (로스터 변경 버튼)
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
        """View가 만료된 경우 메시지를 새로운 View로 업데이트"""
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
        "📢 **공휴일/일요일 스크림 자율 진행 안내**\n"
        "공휴일 및 일요일 스크림의 경우 레이팅컷에 따른 조 편성만 제공합니다.\n"
        "아래 링크를 확인한 뒤 참여해주세요.\n\n"
        f"`{team_name}` 팀은 사설방 개설 후 양식에 맞춰 업로드해주세요.\n"
        f"{settings.CUSTOM_GAME_GUIDE_LINK}"
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
    """
    팀 선택 뷰

    조별 로스터 변경 시 변경할 팀을 선택하는 드롭다운을 제공합니다.
    선택된 팀의 정보를 TeamEditModal로 전달합니다.
    """

    def __init__(self, parent_view: 'GroupRosterView'):
        super().__init__(timeout=None)  # 영구적으로 작동
        self.parent_view = parent_view
        self.is_empty = not parent_view.group_teams

        # Container (안내 텍스트)
        self.add_item(Container(
            TextDisplay(content="## 팀 선택\n변경할 팀을 선택해주세요."),
            Separator(),
            TextDisplay(content=FOOTER_TEXT),
            accent_colour=Color.blue(),
        ))

        if self.is_empty:
            # 빈 팀 리스트일 때 placeholder 옵션 추가
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

        # ActionRow (팀 선택 드랍다운)
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

            # 선택된 팀의 정보 찾기
            selected_team_data = None
            for team_name, team_data, mmr in self.parent_view.group_teams:
                if team_name == selected_team:
                    selected_team_data = (team_name, team_data, mmr)
                    break

            if not selected_team_data:
                await send_response(interaction, error_view("선택된 팀 정보를 찾을 수 없습니다."))
                return

            # 팀 정보 수정 모달 표시
            if interaction.response.is_done():
                logger.warning("[뷰] 이미 응답된 interaction - 팀 수정 모달 표시 불가")
                return
            await interaction.response.send_modal(
                TeamEditModal(self.parent_view, selected_team_data, is_roster_change=True)
            )

        except discord.InteractionResponded:
            pass  # 이미 응답된 상호작용 무시 (팀 선택)
        except discord.NotFound:
            pass  # 상호작용을 찾을 수 없음 - 팀 선택 View 만료 가능성
        except Exception as e:
            logger.error(f"[뷰] 팀 선택 콜백 처리 실패: {e}", exc_info=True)
            try:
                await send_response(interaction, error_view("팀 선택 중 오류가 발생했습니다."))
            except Exception as e2:
                logger.error(f"[뷰] 에러 메시지 전송 실패: {e2}", exc_info=True)
