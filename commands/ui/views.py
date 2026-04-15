"""
Discord View 컴포넌트들
"""
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple
import discord
from discord import ButtonStyle, Color, SelectOption
from discord.ui import ActionRow, Button, Container, LayoutView, MediaGallery, Select, Separator, TextDisplay

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from commands.ui.layout_helpers import (
    error_view, success_view, warning_view, info_view,
    processing_view, timeout_view, permission_error_view,
    custom_view, send_response, edit_to_layout, FOOTER_TEXT,
    update_temp_message, send_error_message,
)
from models.team_data_manager import TeamDataManager
from models.team_processor import TeamProcessor
from services.bser_api import BSERAPIClient
from utils.helpers import get_current_kst_time, is_admin
from utils.validators import validate_team_name

# 버튼 cooldown 관리 (사용자별 마지막 클릭 시간)
_button_cooldowns: Dict[int, float] = {}
BUTTON_COOLDOWN_SECONDS = 1


_COOLDOWN_CLEANUP_THRESHOLD = 100  # 이 크기 초과 시 만료 항목 정리


async def _check_cooldown(interaction: discord.Interaction, cooldown_seconds: float = BUTTON_COOLDOWN_SECONDS) -> bool:
    """버튼 cooldown을 확인합니다. True면 cooldown 중이므로 무시해야 합니다."""
    user_id = interaction.user.id
    now = time.monotonic()
    last_click = _button_cooldowns.get(user_id, 0)
    if now - last_click < cooldown_seconds:
        remaining = cooldown_seconds - (now - last_click)
        await send_response(interaction, info_view("요청 처리 중입니다. 잠시 기다려주세요.", title="⏳ 대기"))
        return True
    _button_cooldowns[user_id] = now
    # 만료된 쿨다운 항목 주기적 정리
    if len(_button_cooldowns) > _COOLDOWN_CLEANUP_THRESHOLD:
        expired = [uid for uid, t in _button_cooldowns.items() if now - t > cooldown_seconds]
        for uid in expired:
            del _button_cooldowns[uid]
    return False

if TYPE_CHECKING:
    from .modals import TeamModal
    from models.team_data import TeamData

logger = get_logger('views')


class TeamInputView(LayoutView):
    """
    팀 입력 및 관리 뷰

    스크림 참가 신청, 취소, 관리자 기능에 대한 버튼들을 제공합니다.
    사용자의 팀 등록 상태에 따라 적절한 모달을 표시합니다.
    """

    def __init__(self, *, scrim_day: int, scrim_month: int, scrim_weekday: str):
        super().__init__(timeout=None)

        # Container (스크림 안내)
        title = f"🏆 {scrim_month}/{scrim_day} ({scrim_weekday}) 스크림"
        schedule = (
            "📋 **일정**\n"
            "`17:00` 팀 등록 마감 · 조편성\n"
            "`20:00` 스크림 시작 (4라운드)\n"
            "`22:00` 다음날 스크림 오픈"
        )

        children = [
            TextDisplay(content=f"## {title}"),
            TextDisplay(content=schedule),
        ]

        # 공지사항 (정적 설정)
        if settings.ANNOUNCEMENT_MESSAGE:
            children.append(TextDisplay(content=f"📢 **공지사항**\n{settings.ANNOUNCEMENT_MESSAGE}"))

        children.append(TextDisplay(content="\n아래 버튼을 눌러 팀을 등록해주세요."))
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
        self.add_item(ActionRow(self.add_team_button, self.cancel_team_button))
    
    async def add_team_callback(self, interaction: discord.Interaction) -> None:
        """팀 추가 버튼 콜백 (기존 팀이 있으면 수정 모달 표시)"""
        if await _check_cooldown(interaction):
            return
        try:
            # 전역 team_data_manager 인스턴스 사용 (순환 참조 방지)
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            
            # 조편성 시작 이후인지 확인
            if team_data_manager.is_team_assignment_started:
                await send_error_message(interaction, "17시 조편성이 완료되어 팀 등록이 불가능합니다. 다음 스크림에 신청해주세요.")
                return
            
            user_team = team_data_manager.find_user_team(
                str(interaction.user.id), interaction.user.display_name
            )
            
            if user_team:
                # 기존 팀이 있는 경우 - 팀 수정 모달 표시
                await self._show_team_edit_modal(interaction, user_team, team_data_manager)
            else:
                # 기존 팀이 없는 경우 - 캐시에서 이전 데이터 조회 후 새 팀 등록 모달 표시
                from .modals import TeamModal
                from models.user_team_cache import UserTeamCache

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
                    self, interaction.user,
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
        if await _check_cooldown(interaction, cooldown_seconds=1):
            return
        try:
            # 전역 team_data_manager 인스턴스 사용 (순환 참조 방지)
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            
            user_team = team_data_manager.find_user_team(
                str(interaction.user.id), interaction.user.display_name
            )
            
            if not user_team:
                await send_error_message(interaction, "등록한 팀이 없습니다.")
                return

            # 팀 정보 가져오기
            team_data = team_data_manager.get_team_data(user_team)
            team_mmr = team_data_manager.get_team_mmr(user_team) or 0.0

            players = []
            staff = []
            if team_data:
                from utils.helpers import get_team_members
                players, staff = get_team_members(team_data)

            # 확인 LayoutView 생성 (CancelConfirmView에 텍스트 + 버튼 포함)
            members_str = ', '.join(players) if players else '(없음)'
            staff_str = ', '.join(staff) if staff else '(없음)'
            cancel_text = f"**{user_team}** 팀의 등록을 취소하시겠습니까?"
            fields = [("선수", members_str)]
            if staff:
                fields.append(("스태프", staff_str))
            fields.append(("MMR", f"{team_mmr:.2f}"))

            confirm_view = CancelConfirmView(self, user_team, cancel_text=cancel_text, fields=fields)
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
            # 팀 데이터 가져오기
            team_data = team_data_manager.get_team_data(team_name)
            if not team_data:
                await send_error_message(interaction, "팀 정보를 찾을 수 없습니다.")
                return

            # MMR 정보 가져오기
            team_mmr = team_data_manager.get_team_mmr(team_name) or 0.0

            # 팀 정보를 튜플 형태로 구성 (TeamEditModal 형식에 맞춤)
            team_info = (team_name, team_data, team_mmr)

            # 팀 수정 모달 표시 (TeamInputView를 직접 전달하여 일반 참가자 수정임을 명확히 함)
            from .modals import TeamEditModal
            if not interaction.response.is_done():
                await interaction.response.send_modal(TeamEditModal(self, team_info))
            else:
                await interaction.followup.send_modal(TeamEditModal(self, team_info))

        except discord.NotFound:
            logger.warning("[뷰] 팀 수정 모달 interaction 만료")
        except Exception as e:
            logger.error(f"[뷰] 팀 수정 모달 표시 실패: {e}", exc_info=True)
            await send_error_message(interaction, "팀 수정 모달 표시 중 오류가 발생했습니다.")
    
    async def _process_team_registration(self, interaction: discord.Interaction, team_name: str, team_data: dict, temp_message: discord.Message = None) -> None:
        """팀 등록 처리"""
        try:
            current_time = get_current_kst_time()
            team_data_manager = BotManager.get_instance().get_team_data_manager()

            # ── 1단계: 로컬 검증 (즉시, 네트워크 불필요) ──
            errors = []

            if team_data_manager.is_team_assignment_started:
                errors.append("17시 조편성이 완료되어 팀 등록이 불가능합니다. 다음 스크림에 신청해주세요.")

            if not errors:
                is_allowed, err = await team_data_manager.check_team_registration_allowed(current_time)
                if not is_allowed:
                    errors.append(err)

            from utils.helpers import get_all_members
            team_members = get_all_members(team_data) if isinstance(team_data, dict) or hasattr(team_data, 'players') else team_data

            if not errors:
                is_bot_valid, bot_err = team_data_manager.check_duplicate_with_bot_teams(team_name, team_members)
                if not is_bot_valid:
                    errors.append(bot_err)

            if not errors:
                from utils.validators import validate_members_in_guild
                client = BotManager.get_instance().get_client()
                guild = client.get_guild(settings.GUILD_ID) if client else None
                if guild:
                    is_guild_valid, not_found = validate_members_in_guild(guild, team_members)
                    if not is_guild_valid:
                        errors.append(f"❌ 디스코드 서버에서 확인되지 않는 닉네임: **{', '.join(not_found)}**")

            # 로컬 검증 실패 시 즉시 반환 (네트워크 호출 생략)
            if errors:
                msg = '\n'.join(errors)
                if temp_message:
                    await update_temp_message(temp_message, msg, discord.Color.red())
                else:
                    await send_error_message(interaction, msg)
                return

            # ── 2단계: API 검증 (네트워크, 병렬) ──
            team_processor = BotManager.get_instance().get_team_processor()
            has_test_account = any(team_processor._is_test_account(m) for m in team_members)

            is_maintenance = False
            if not has_test_account:
                from utils.validators import validate_members_api
                is_valid, api_invalid_members, is_maintenance = await validate_members_api(
                    team_members, team_data_manager
                )
                if not is_valid and not is_maintenance:
                    msg = f"❌ 게임 내에서 확인되지 않는 닉네임: **{', '.join(api_invalid_members)}**\n💡 게임 내 닉네임을 정확히 입력해주세요."
                    if temp_message:
                        await update_temp_message(temp_message, msg, discord.Color.red())
                    else:
                        await send_error_message(interaction, msg)
                    return
            
            # MMR 계산 (team_data dict에 mmr 필드가 설정됨)
            team_mmr = 0.0
            try:
                team_processor = BotManager.get_instance().get_team_processor()
                _, _, team_mmr = await team_processor.fetch_team_mmr(team_name, team_data)
            except Exception as e:
                logger.error(f"[뷰] 팀 MMR 계산 실패 - 팀명: {team_name}: {e}", exc_info=True)

            # 팀 데이터 저장 (MMR이 team_data에 이미 포함됨, user_id는 add_team에서 자동 설정)
            success, failure_reason = await team_data_manager.add_team(team_name, team_data, interaction.user)
            if not success:
                # 실패 사유가 있으면 그대로 표시, 없으면 기본 메시지
                error_message = failure_reason if failure_reason else (
                    "❌ 팀 등록에 실패했습니다.\n\n"
                    "💡 신청 시간 제한을 확인해주세요."
                )
                if temp_message:
                    await update_temp_message(temp_message, error_message, discord.Color.red())
                else:
                    await send_error_message(interaction, error_message)
                return
            
            # 팀원과 스태프 목록 추출
            from utils.helpers import get_team_members
            players, staff = get_team_members(team_data)

            # 캐시 저장
            try:
                from models.user_team_cache import UserTeamCache
                cache = UserTeamCache()
                cache.set(str(interaction.user.id), {
                    "team_name": team_name,
                    "players": players,
                    "staff": staff,
                })
            except Exception as e:
                logger.warning(f"[뷰] 캐시 저장 실패: {e}")

            players_str = ', '.join(players) if players else '(없음)'
            staff_str = ', '.join(staff) if staff else '(없음)'
            team_data_manager.log_action(
                "신청", interaction.user, team_name,
                detail=f"선수: {players_str} · 스태프: {staff_str}",
            )
            logger.info(f"[팀신청] {team_name} | MMR: {team_mmr:.2f} | 선수: [{players_str}] | 스태프: [{staff_str}]")

            if is_maintenance:
                team_data_manager.unverified_teams.add(team_name)
                team_data_manager._save_backup()
                success_msg = (
                    f"**{team_name}** 팀이 등록되었습니다.\n\n"
                    f"🎮 선수: {players_str}\n"
                    f"🛠️ 스태프: {staff_str}\n\n"
                    f"🔧 서버 점검으로 닉네임 확인을 건너뛰었습니다.\n"
                    f"점검 종료 후 자동으로 확인되며, 결과는 DM으로 안내드립니다.\n"
                    f"💡 닉네임 오타가 없는지 다시 한번 확인해주세요."
                )
            else:
                success_msg = (
                    f"**{team_name}** 팀이 성공적으로 등록되었습니다!\n\n"
                    f"🎮 선수: {players_str}\n"
                    f"🛠️ 스태프: {staff_str}\n"
                    f"📊 팀 평균 MMR: **{team_mmr:.2f}**"
                )

            if temp_message:
                # 임시 메시지를 성공 메시지로 업데이트
                await update_temp_message(temp_message, success_msg, discord.Color.green())
            else:
                # LayoutView로 성공 메시지 전송
                await send_response(interaction, success_view(success_msg))
            
            # 백그라운드에서 MMR 갱신 및 메시지 업데이트
            import asyncio
            task = asyncio.create_task(self._update_mmr_background(team_data_manager, interaction.channel))
            team_data_manager._pending_tasks.add(task)
            task.add_done_callback(team_data_manager._pending_tasks.discard)

        except discord.NotFound:
            logger.warning("[뷰] 팀 등록 interaction 만료")
        except discord.HTTPException as e:
            logger.error(f"[뷰] 팀 등록 Discord API 오류: {e.status} {e.text}", exc_info=True)
            await send_error_message(interaction, "❌ 팀 등록 중 Discord 오류가 발생했습니다.\n\n💡 잠시 후 다시 시도해주세요.")
        except Exception as e:
            logger.error(f"[뷰] 팀 등록 실패: {e}", exc_info=True)
            await send_error_message(
                interaction,
                "❌ 팀 등록 중 오류가 발생했습니다.\n\n💡 다시 시도해도 문제가 지속되면 관리자에게 문의해주세요."
            )

    async def _update_mmr_background(self, team_data_manager, channel) -> None:
        """백그라운드에서 MMR 갱신 및 메시지 업데이트"""
        try:
            # ✅ scrim.py 방식: 직접 가져오기, 복잡한 체크 제거
            client = BotManager.get_instance().get_client()
            
            # MMR 갱신
            try:
                await team_data_manager._update_all_team_mmr()
            except Exception as e:
                logger.error(f"[뷰] 팀 MMR 갱신 실패: {e}", exc_info=True)
            
            # MMR 메시지 업데이트 (실패 시 다음 갱신 루프에서 재시도)
            try:
                await team_data_manager.update_mmr_message(channel)
            except Exception as e:
                logger.error(f"[뷰] MMR 메시지 업데이트 실패: {e}", exc_info=True)
                    
        except Exception as e:
            logger.error(f"[뷰] 백그라운드 MMR 갱신 실패: {e}", exc_info=True)
    
    async def _process_team_cancellation(self, interaction: discord.Interaction, team_name: str) -> None:
        """팀 취소 처리"""
        try:
            # 현재 시간 확인
            current_time = get_current_kst_time()
            
            # 전역 team_data_manager 인스턴스 사용 (순환 참조 방지)
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            
            # 조편성 시작 이후인지 확인
            if team_data_manager.is_team_assignment_started:
                await send_error_message(interaction, "17시 조편성이 완료되어 팀 취소가 불가능합니다. 관리자에게 문의하세요.")
                return
            
            # 팀 취소 가능 여부 확인
            is_allowed, error_message = await team_data_manager.check_team_cancellation_allowed(current_time)
            
            if not is_allowed:
                await send_error_message(interaction, error_message)
                return
            
            # 팀 존재 확인
            if team_name not in team_data_manager.get_all_teams():
                await send_error_message(interaction, "등록되지 않은 팀명입니다.")
                return

            # 일반 취소 처리
            await self._execute_team_cancellation(interaction, team_name, team_data_manager)

        except discord.NotFound:
            logger.warning("[뷰] 팀 취소 interaction 만료")
        except Exception as e:
            logger.error(f"[뷰] 팀 취소 실패: {e}", exc_info=True)
            await send_error_message(interaction, "팀 취소 중 오류가 발생했습니다.")
    
    async def _execute_team_cancellation(self, interaction: discord.Interaction, team_name: str, team_data_manager: TeamDataManager) -> None:
        """실제 팀 취소 실행"""
        try:
            # 팀 정보 가져오기 (취소 전에)
            team_info = team_data_manager.get_team_data(team_name)
            players = []
            staff = []
            if team_info:
                if isinstance(team_info, dict):
                    players = team_info.get('players', [])
                    staff = team_info.get('staff', [])
                else:
                    players = getattr(team_info, 'players', [])
                    staff = getattr(team_info, 'staff', [])
            
            # 팀 취소 처리 (조편성 체크는 이미 _process_team_cancellation에서 수행됨)
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
                detail=f"선수: {players_str} · 스태프: {staff_str}",
            )
            logger.info(f"[팀취소] {team_name} | 선수: [{players_str}] | 스태프: [{staff_str}]")

            # 성공 메시지 전송
            await send_response(interaction, success_view(f"**{team_name}** 팀이 성공적으로 취소되었습니다."))

            # 백그라운드에서 MMR 갱신 및 메시지 업데이트
            import asyncio
            task = asyncio.create_task(self._update_mmr_background(team_data_manager, interaction.channel))
            team_data_manager._pending_tasks.add(task)
            task.add_done_callback(team_data_manager._pending_tasks.discard)

        except discord.NotFound:
            logger.warning("[뷰] 팀 취소 실행 interaction 만료")
        except Exception as e:
            logger.error(f"[뷰] 팀 취소 실행 실패: {e}", exc_info=True)
            await send_response(interaction, error_view("팀 취소 중 오류가 발생했습니다."))
    


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
            children.append(MediaGallery(discord.MediaGalleryItem(media="attachment://group_mmr_table.png")))
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

    def update_group_teams(self, new_group_teams: List[Tuple[str, 'TeamData', float]]) -> None:
        """팀 정보를 업데이트합니다."""
        self.group_teams = new_group_teams

    async def roster_change_callback(self, interaction: discord.Interaction) -> None:
        """로스터 변경 버튼 콜백"""
        try:
            if not is_admin(interaction.user):
                await send_response(interaction, permission_error_view())
                return

            from .views import TeamSelectionView
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
            TextDisplay(content="## 팀 선택\n변경할 팀을 선택하세요."),
            Separator(),
            TextDisplay(content=FOOTER_TEXT),
            accent_colour=Color.blue(),
        ))

        if self.is_empty:
            # 빈 팀 리스트일 때 placeholder 옵션 추가
            options = [SelectOption(label="등록된 팀이 없습니다", value="_empty", description="팀이 등록되면 선택 가능합니다")]
        else:
            from utils.helpers import get_team_members as _get_tm
            options = [
                SelectOption(
                    label=f"{i+1}. {team_name} (MMR: {mmr:.2f})",
                    value=team_name,
                    description=f"팀원: {', '.join(_get_tm(team_data)[0][:3]) or '정보 없음'}"
                )
                for i, (team_name, team_data, mmr) in enumerate(parent_view.group_teams)
            ]

        # ActionRow (팀 선택 드랍다운)
        self.team_select = Select(
            placeholder="변경할 팀을 선택하세요",
            options=options,
            disabled=self.is_empty
        )
        self.team_select.callback = self.team_select_callback
        self.add_item(ActionRow(self.team_select))

    async def team_select_callback(self, interaction: discord.Interaction) -> None:
        """팀 선택 콜백"""
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
            from .modals import TeamEditModal
            if not interaction.response.is_done():
                await interaction.response.send_modal(TeamEditModal(self.parent_view, selected_team_data))
            else:
                await interaction.followup.send_modal(TeamEditModal(self.parent_view, selected_team_data))

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


class CancelConfirmView(LayoutView):
    """
    팀 취소 확인 뷰

    취소 버튼 클릭 후 팀 정보를 표시하고 최종 확인을 받습니다.
    """

    def __init__(
        self,
        parent_view: 'TeamInputView',
        team_name: str,
        *,
        cancel_text: str = "",
        fields: list[tuple[str, str]] | None = None,
    ):
        super().__init__(timeout=60)
        self.parent_view = parent_view
        self.team_name = team_name
        self.message: Optional[discord.Message] = None

        # Container (안내 텍스트 + 필드)
        children: list = [TextDisplay(content=f"## 🚫 팀 등록 취소 확인\n{cancel_text}")]
        if fields:
            for name, value in fields:
                children.append(TextDisplay(content=f"**{name}**\n{value}"))
        children.append(Separator())
        children.append(TextDisplay(content=FOOTER_TEXT))
        self.add_item(Container(*children, accent_colour=Color.orange()))

        # ActionRow (확인/돌아가기 버튼)
        self.confirm_button = Button(label="등록 취소하기", style=ButtonStyle.danger, emoji="⚠️")
        self.confirm_button.callback = self.confirm_callback
        self.back_button = Button(label="돌아가기", style=ButtonStyle.secondary, emoji="↩️")
        self.back_button.callback = self.back_callback
        self.add_item(ActionRow(self.confirm_button, self.back_button))

    async def confirm_callback(self, interaction: discord.Interaction) -> None:
        """취소 확인 버튼 콜백"""
        try:
            # 버튼 비활성화
            self.confirm_button.disabled = True
            self.back_button.disabled = True
            await interaction.response.edit_message(view=self)

            # 기존 취소 로직 실행
            await self.parent_view._process_team_cancellation(interaction, self.team_name)
        except Exception as e:
            logger.error(f"[뷰] 팀 취소 확인 콜백 실패: {e}", exc_info=True)
            await interaction.followup.send(
                view=error_view("팀 취소 중 오류가 발생했습니다."),
                ephemeral=True
            )

    async def back_callback(self, interaction: discord.Interaction) -> None:
        """돌아가기 버튼 콜백"""
        try:
            await interaction.response.edit_message(view=info_view("이전 화면으로 돌아갔습니다."), embed=None, content=None)
        except Exception as e:
            logger.error(f"[뷰] 팀 취소 돌아가기 콜백 실패: {e}", exc_info=True)

    async def on_timeout(self) -> None:
        """타임아웃 시 버튼 비활성화 및 안내 메시지"""
        if self.message:
            try:
                await self.message.edit(view=timeout_view(), embed=None, content=None)
            except Exception:
                pass