"""
대시보드 계열 Discord View 컴포넌트들

스크림 대시보드(TeamInputView)와 신청 취소/강제취소 흐름의 뷰를 담당합니다.
로스터 계열 뷰(GroupRosterView 등)는 commands/ui/roster_views.py에 있습니다.
"""
from typing import TYPE_CHECKING, Dict, Optional
import discord
from discord import ButtonStyle, Color, SelectOption
from discord.ui import ActionRow, Button, Container, LayoutView, Select, Separator, TextDisplay

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from utils.layout_helpers import (
    check_cooldown,
    error_view, success_view, info_view,
    timeout_view, permission_error_view,
    send_response, FOOTER_TEXT,
    send_error_message,
)
from commands.team_pipeline import schedule_mmr_refresh
from models.team_data_manager import ASSIGNMENT_CLOSED_REGISTER_MSG
from models.user_team_cache import UserTeamCache
from utils.helpers import get_team_members, is_admin

from .modals import TeamEditModal, TeamModal

if TYPE_CHECKING:
    from models.team_data import TeamData

logger = get_logger('views')

# 취소/강제취소 마감 안내. 마감 시각은 settings 를 단일 출처로 쓴다
# (등록/수정 문구는 models.team_data_manager 의 ASSIGNMENT_CLOSED_*_MSG 를 그대로 사용)
ASSIGNMENT_CLOSED_CANCEL_MSG = (
    f"{settings.TEAM_REGISTRATION_DEADLINE_HOUR}시 조편성이 완료되어 팀 취소가 불가능합니다. "
    "관리자에게 문의해주세요."
)
ASSIGNMENT_CLOSED_FORCE_CANCEL_MSG = (
    f"{settings.TEAM_REGISTRATION_DEADLINE_HOUR}시 조편성이 완료되어 강제취소가 불가능합니다."
)


class TeamInputView(LayoutView):
    """
    팀 입력 및 관리 뷰

    스크림 참가 신청, 취소, 관리자 기능에 대한 버튼들을 제공합니다.
    사용자의 팀 등록 상태에 따라 적절한 모달을 표시합니다.
    """

    def __init__(self, *, scrim_day: int, scrim_month: int, scrim_weekday: str, is_rest_day: bool = False):
        super().__init__(timeout=None)

        # Container (스크림 안내): 공휴일/일요일은 '자율 스크림'으로 표시
        scrim_label = "자율 스크림" if is_rest_day else "스크림"
        title = f"🏆 {scrim_month}/{scrim_day} ({scrim_weekday}) {scrim_label}"
        deadline_line = f"`{settings.TEAM_REGISTRATION_DEADLINE_HOUR}:00` 팀 등록 마감, 조편성\n"
        start_line = f"`{settings.SCRIM_START_HOUR}:00` 스크림 시작 ({settings.TOTAL_ROUNDS}라운드)\n"
        open_line = f"`{settings.NEXT_SCRIM_OPEN_HOUR}:00` 다음날 스크림 오픈"
        if is_rest_day:
            schedule = (
                "📋 **일정**\n"
                + deadline_line
                + "`19:55` 지정된 팀 방설정 완료\n"
                + start_line
                + open_line
            )
        else:
            schedule = (
                "📋 **일정**\n"
                + deadline_line
                + start_line
                + open_line
            )

        children = [
            TextDisplay(content=f"## {title}"),
            TextDisplay(content=schedule),
        ]

        # 공지사항 (정적 설정 + 공휴일/일요일 시 사용자 설정 대전 가이드 링크)
        announcement_lines = []
        if settings.ANNOUNCEMENT_MESSAGE:
            announcement_lines.append(settings.ANNOUNCEMENT_MESSAGE)
        if is_rest_day:
            announcement_lines.append(settings.CUSTOM_GAME_GUIDE_LINK)
        if announcement_lines:
            children.append(TextDisplay(content="📢 **공지사항**\n" + "\n".join(announcement_lines)))

        children.append(Separator())
        children.append(TextDisplay(content=FOOTER_TEXT))

        self.add_item(Container(*children, accent_colour=Color.green()))

        # ActionRow (신청/취소 버튼)
        self.add_team_button = Button(
            label="신청/수정",
            style=ButtonStyle.primary,
            emoji="✏️"
        )
        self.add_team_button.callback = self.add_team_callback
        self.cancel_team_button = Button(
            label="취소",
            style=ButtonStyle.secondary,
            emoji="🚫"
        )
        self.cancel_team_button.callback = self.cancel_team_callback
        self.manage_button = Button(
            label="관리",
            style=ButtonStyle.secondary,
            emoji="🛠️"
        )
        self.manage_button.callback = self.manage_callback
        self.add_item(ActionRow(self.add_team_button, self.cancel_team_button, self.manage_button))

    async def add_team_callback(self, interaction: discord.Interaction) -> None:
        """팀 추가 버튼 콜백 (기존 팀이 있으면 수정 모달 표시)"""
        if await check_cooldown(interaction):
            return
        try:
            team_data_manager = BotManager.get_instance().get_team_data_manager()

            # 조편성 시작 이후인지 확인
            if team_data_manager.is_team_assignment_started:
                await send_error_message(interaction, ASSIGNMENT_CLOSED_REGISTER_MSG)
                return
            
            user_team = team_data_manager.find_user_team(
                str(interaction.user.id), interaction.user
            )
            
            if user_team:
                # 기존 팀이 있는 경우 - 팀 수정 모달 표시
                await self._show_team_edit_modal(interaction, user_team, team_data_manager)
            else:
                # 기존 팀이 없는 경우 - 캐시에서 이전 데이터 조회 후 새 팀 등록 모달 표시
                default_team_name = ""
                default_players = ""
                default_staff = ""

                try:
                    cache = UserTeamCache()
                    cached = cache.get(str(interaction.user.id))
                    if cached:
                        default_team_name = cached.get("team_name", "")
                        default_players = "\n".join(cached.get("players", []))
                        default_staff = "\n".join(cached.get("staff", []))
                except Exception as e:
                    logger.warning(f"[뷰] 캐시 조회 실패: {e}")

                modal = TeamModal(
                    interaction.user,
                    default_team_name=default_team_name,
                    default_players=default_players,
                    default_staff=default_staff,
                )
                if not interaction.response.is_done():
                    await interaction.response.send_modal(modal)
                else:
                    await interaction.followup.send("모달을 표시할 수 없습니다. 다시 시도해주세요.", ephemeral=True)

        except discord.NotFound:
            logger.warning("[뷰] 팀 추가 interaction 만료")
        except Exception as e:
            logger.error(f"[뷰] 팀 추가 콜백 처리 실패: {e}", exc_info=True)
            await send_error_message(interaction, "팀 추가 중 오류가 발생했습니다.")
    
    async def cancel_team_callback(self, interaction: discord.Interaction) -> None:
        """팀 취소 버튼 콜백 (신청자 ID 또는 닉네임 기반)"""
        if await check_cooldown(interaction, cooldown_seconds=1):
            return
        try:
            team_data_manager = BotManager.get_instance().get_team_data_manager()

            user_team = team_data_manager.find_user_team(
                str(interaction.user.id), interaction.user
            )
            
            if not user_team:
                await send_error_message(interaction, "등록한 팀이 없습니다.")
                return

            team_data = team_data_manager.get_team_data(user_team)
            team_mmr = team_data_manager.get_team_mmr(user_team) or 0.0

            players = []
            staff = []
            if team_data:
                players, staff = get_team_members(team_data)

            # 확인 LayoutView 생성 (ConfirmView에 텍스트 + 버튼 포함)
            members_str = ', '.join(players) if players else '(없음)'
            staff_str = ', '.join(staff) if staff else '(없음)'
            cancel_text = f"**{user_team}** 팀의 등록을 취소하시겠습니까?"
            fields = [("선수", members_str)]
            if staff:
                fields.append(("스태프", staff_str))
            fields.append(("MMR", f"{team_mmr:.2f}"))

            confirm_view = ConfirmView(
                title="🚫 팀 등록 취소 확인",
                body=cancel_text,
                fields=fields,
                confirm_label="등록 취소하기",
                confirm_emoji="⚠️",
                accent_colour=Color.orange(),
                error_text="팀 취소 중 오류가 발생했습니다.",
                on_confirm=lambda i: self._process_team_cancellation(i, user_team),
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(view=confirm_view, ephemeral=True)
                confirm_view.message = await interaction.original_response()
            else:
                msg = await interaction.followup.send(view=confirm_view, ephemeral=True, wait=True)
                confirm_view.message = msg

        except discord.NotFound:
            logger.warning("[뷰] 팀 취소 interaction 만료")
        except Exception as e:
            logger.error(f"[뷰] 팀 취소 콜백 처리 실패: {e}", exc_info=True)
            await send_error_message(interaction, "팀 취소 중 오류가 발생했습니다.")
    
    
    async def _show_team_edit_modal(self, interaction: discord.Interaction, team_name: str, team_data_manager) -> None:
        """기존 팀 수정 모달을 표시합니다."""
        try:
            team_data = team_data_manager.get_team_data(team_name)
            if not team_data:
                await send_error_message(interaction, "팀 정보를 찾을 수 없습니다.")
                return

            team_mmr = team_data_manager.get_team_mmr(team_name) or 0.0

            # 팀 정보를 튜플 형태로 구성 (TeamEditModal 형식에 맞춤)
            team_info = (team_name, team_data, team_mmr)

            # 팀 수정 모달 표시 (TeamInputView를 직접 전달하여 일반 참가자 수정임을 명확히 함)
            if interaction.response.is_done():
                logger.warning("[뷰] 이미 응답된 interaction - 팀 수정 모달 표시 불가")
                return
            await interaction.response.send_modal(TeamEditModal(self, team_info, is_roster_change=False))

        except discord.NotFound:
            logger.warning("[뷰] 팀 수정 모달 interaction 만료")
        except Exception as e:
            logger.error(f"[뷰] 팀 수정 모달 표시 실패: {e}", exc_info=True)
            await send_error_message(interaction, "팀 수정 모달 표시 중 오류가 발생했습니다.")
    
    async def _process_team_cancellation(self, interaction: discord.Interaction, team_name: str) -> None:
        try:
            team_data_manager = BotManager.get_instance().get_team_data_manager()

            # 조편성 시작 이후인지 확인
            if team_data_manager.is_team_assignment_started:
                await send_error_message(interaction, ASSIGNMENT_CLOSED_CANCEL_MSG)
                return

            # 팀 정보 가져오기 (취소 전에). 미등록 팀은 remove_team 이 사유와 함께 거부한다
            team_info = team_data_manager.get_team_data(team_name)
            players = []
            staff = []
            if team_info:
                players, staff = get_team_members(team_info)

            success, failure_reason = await team_data_manager.remove_team(team_name)
            if not success:
                # 실패 사유가 있으면 그대로 표시, 없으면 기본 메시지
                error_message = failure_reason if failure_reason else (
                    "팀 취소가 실패했습니다.\n\n"
                    "💡 취소 시간 제한을 확인해주세요."
                )
                await send_response(interaction, error_view(error_message))
                return

            players_str = ', '.join(players) if players else '(없음)'
            staff_str = ', '.join(staff) if staff else '(없음)'
            team_data_manager.log_action(
                "취소", interaction.user, team_name,
                detail=f"선수: {players_str} / 스태프: {staff_str}",
            )
            logger.info(f"[팀취소] {team_name} | 선수: [{players_str}] | 스태프: [{staff_str}]")

            await send_response(interaction, success_view(f"**{team_name}** 팀이 성공적으로 취소되었습니다."))

            # 백그라운드에서 MMR 갱신 및 메시지 업데이트
            schedule_mmr_refresh(team_data_manager, interaction.channel)

        except discord.NotFound:
            logger.warning("[뷰] 팀 취소 interaction 만료")
        except Exception as e:
            logger.error(f"[뷰] 팀 취소 실패: {e}", exc_info=True)
            await send_error_message(interaction, "팀 취소 중 오류가 발생했습니다.")

    # ── 운영진 강제취소 ────────────────────────────────────────────────
    async def manage_callback(self, interaction: discord.Interaction) -> None:
        """관리 버튼 콜백: 운영진 전용 팀 강제취소 진입점."""
        if await check_cooldown(interaction):
            return
        try:
            if not is_admin(interaction.user):
                await send_response(interaction, permission_error_view())
                return

            team_data_manager = BotManager.get_instance().get_team_data_manager()
            if team_data_manager.is_team_assignment_started:
                await send_error_message(interaction, ASSIGNMENT_CLOSED_FORCE_CANCEL_MSG)
                return

            teams = team_data_manager.get_all_teams()
            if not teams:
                await send_response(interaction, info_view("신청한 팀이 없습니다."))
                return

            view = ForceCancelSelectView(self, teams)
            view.message = await send_response(interaction, view)

        except discord.NotFound:
            logger.warning("[뷰] 관리 interaction 만료")
        except Exception as e:
            logger.error(f"[뷰] 관리 콜백 처리 실패: {e}", exc_info=True)
            await send_error_message(interaction, "관리 화면을 여는 중 오류가 발생했습니다.")

    async def _execute_force_cancel(self, interaction: discord.Interaction, team_name: str) -> None:
        """운영진 강제취소 실행: 확정 시점에 권한과 조편성 상태를 재검증한다."""
        try:
            team_data_manager = BotManager.get_instance().get_team_data_manager()

            # 확정 시점 재검증 (드롭다운/확인 대기 중 상태 변화 방지)
            if not is_admin(interaction.user):
                await send_response(interaction, permission_error_view())
                return
            if team_data_manager.is_team_assignment_started:
                await send_error_message(interaction, ASSIGNMENT_CLOSED_FORCE_CANCEL_MSG)
                return

            team_info = team_data_manager.get_team_data(team_name)
            if team_info is None:
                await send_error_message(interaction, "이미 취소되었거나 존재하지 않는 팀입니다.")
                return

            players, staff = get_team_members(team_info)
            applicant_id = team_info.user_id

            success, failure_reason = await team_data_manager.remove_team(team_name)
            if not success:
                await send_response(interaction, error_view(failure_reason or "강제취소에 실패했습니다."))
                return

            players_str = ', '.join(players) if players else '(없음)'
            staff_str = ', '.join(staff) if staff else '(없음)'
            applicant = f"<@{applicant_id}>" if applicant_id else "(미상)"
            team_data_manager.log_action(
                "강제취소", interaction.user, team_name,
                detail=f"신청자: {applicant} / 선수: {players_str} / 스태프: {staff_str}",
            )
            logger.info(f"[강제취소] {team_name} | 운영진: {interaction.user} | 선수: [{players_str}]")

            await send_response(interaction, success_view(f"**{team_name}** 팀을 강제 취소했습니다."))

            # 백그라운드에서 MMR 갱신 및 대시보드 메시지 업데이트
            schedule_mmr_refresh(team_data_manager, interaction.channel)

        except discord.NotFound:
            logger.warning("[뷰] 강제취소 실행 interaction 만료")
        except Exception as e:
            logger.error(f"[뷰] 강제취소 실행 실패: {e}", exc_info=True)
            await send_response(interaction, error_view("강제취소 중 오류가 발생했습니다."))


class _TimeoutEditView(LayoutView):
    """타임아웃 시 메시지를 안내 뷰로 교체하는 공통 베이스."""

    def __init__(self, *, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.message: Optional[discord.Message] = None

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=timeout_view(), embed=None, content=None)
            except Exception:
                pass


class ConfirmView(_TimeoutEditView):
    """
    확인/돌아가기 공통 확인 뷰

    안내 텍스트와 선택 필드를 표시하고, 확인 시 on_confirm(interaction)에 위임합니다.
    """

    def __init__(
        self,
        *,
        title: str,
        body: str = "",
        fields: list[tuple[str, str]] | None = None,
        confirm_label: str,
        confirm_emoji: str,
        accent_colour: Color,
        error_text: str,
        on_confirm,
    ):
        super().__init__()
        self._on_confirm = on_confirm
        self._error_text = error_text

        # Container (안내 텍스트 + 필드)
        children: list = [TextDisplay(content=f"## {title}\n{body}" if body else f"## {title}")]
        if fields:
            for name, value in fields:
                children.append(TextDisplay(content=f"**{name}**\n{value}"))
        children.append(Separator())
        children.append(TextDisplay(content=FOOTER_TEXT))
        self.add_item(Container(*children, accent_colour=accent_colour))

        # ActionRow (확인/돌아가기 버튼)
        self.confirm_button = Button(label=confirm_label, style=ButtonStyle.danger, emoji=confirm_emoji)
        self.confirm_button.callback = self.confirm_callback
        self.back_button = Button(label="돌아가기", style=ButtonStyle.secondary, emoji="↩️")
        self.back_button.callback = self.back_callback
        self.add_item(ActionRow(self.confirm_button, self.back_button))

    async def confirm_callback(self, interaction: discord.Interaction) -> None:
        try:
            self.confirm_button.disabled = True
            self.back_button.disabled = True
            await interaction.response.edit_message(view=self)

            await self._on_confirm(interaction)
        except Exception as e:
            logger.error(f"[뷰] 확인 콜백 실패: {e}", exc_info=True)
            await interaction.followup.send(
                view=error_view(self._error_text),
                ephemeral=True
            )

    async def back_callback(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.edit_message(view=info_view("이전 화면으로 돌아갔습니다."), embed=None, content=None)
        except Exception as e:
            logger.error(f"[뷰] 돌아가기 콜백 실패: {e}", exc_info=True)


_SELECT_OPTION_LIMIT = 25  # Discord Select 옵션 최대 개수


class ForceCancelSelectView(_TimeoutEditView):
    """
    운영진 강제취소: 팀 선택 뷰

    신청된 전체 팀을 드롭다운으로 보여주고, 선택한 팀을 강제취소 확인 단계로 넘긴다.
    팀이 25개를 넘으면 Discord Select 제한을 우회하기 위해 드롭다운을 여러 개로 분할한다.
    """

    def __init__(self, parent_view: Optional['TeamInputView'], teams: Dict[str, 'TeamData']):
        super().__init__()
        self.parent_view = parent_view

        self.add_item(Container(
            TextDisplay(content="## 🛠️ 팀 강제취소\n취소할 팀을 선택해주세요."),
            Separator(),
            TextDisplay(content=FOOTER_TEXT),
            accent_colour=Color.orange(),
        ))

        sorted_names = sorted(teams.keys())
        self.selects: list[Select] = []
        for start in range(0, len(sorted_names), _SELECT_OPTION_LIMIT):
            chunk = sorted_names[start:start + _SELECT_OPTION_LIMIT]
            options = [
                SelectOption(
                    label=f"{name} (MMR: {getattr(teams[name], 'mmr', 0.0):.2f})"[:100],
                    value=name,
                    description=(', '.join(getattr(teams[name], 'players', [])[:3]) or '정보 없음')[:100],
                )
                for name in chunk
            ]
            select = Select(placeholder="취소할 팀을 선택해주세요", options=options)
            select.callback = self.team_select_callback
            self.selects.append(select)
            self.add_item(ActionRow(select))

    async def team_select_callback(self, interaction: discord.Interaction) -> None:
        """드롭다운에서 팀 선택 → 강제취소 확인 단계로 진입."""
        try:
            # 발화한 드롭다운의 선택값을 raw payload에서 직접 읽는다(다중 Select 분할 시 모호성 제거).
            values = (interaction.data or {}).get("values") or []
            selected = values[0] if values else None
            if not selected:
                await send_response(interaction, error_view("선택된 팀을 확인할 수 없습니다."))
                return

            parent_view = self.parent_view

            async def _confirm_force_cancel(inter: discord.Interaction, team_name: str = selected) -> None:
                if parent_view is not None:
                    await parent_view._execute_force_cancel(inter, team_name)

            confirm_view = ConfirmView(
                title="🔨 강제취소 확인",
                body=f"**{selected}** 팀을 강제취소하시겠습니까?",
                confirm_label="강제 취소하기",
                confirm_emoji="🔨",
                accent_colour=Color.red(),
                error_text="강제취소 중 오류가 발생했습니다.",
                on_confirm=_confirm_force_cancel,
            )
            self.stop()  # 확인 단계로 전환: 이 드롭다운 뷰의 타임아웃 타이머 종료
            await interaction.response.edit_message(view=confirm_view)
            confirm_view.message = await interaction.original_response()
        except discord.InteractionResponded:
            pass
        except discord.NotFound:
            logger.warning("[뷰] 강제취소 팀 선택 interaction 만료")
        except Exception as e:
            logger.error(f"[뷰] 강제취소 팀 선택 실패: {e}", exc_info=True)
            await send_response(interaction, error_view("팀 선택 중 오류가 발생했습니다."))

