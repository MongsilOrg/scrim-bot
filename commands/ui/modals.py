"""
Discord Modal 컴포넌트들
"""
from typing import TYPE_CHECKING, Dict, List, Tuple, Union

import discord
from discord.components import CheckboxGroupOption, RadioGroupOption
from discord.ui import CheckboxGroup, Label, Modal, RadioGroup, Select, TextDisplay, TextInput

from bot.manager import BotManager
from commands.ui.layout_helpers import (
    error_view, success_view, warning_view, info_view,
    processing_view, timeout_view, custom_view,
    send_response, FOOTER_TEXT,
    update_temp_message, send_error_message,
)
from config.logging_config import get_logger
from utils.validators import (
    check_duplicate_members,
    normalize_nickname_for_comparison,
    validate_discord_user_in_team,
    validate_members_in_guild,
    validate_team_data,
    validate_team_name,
)

if TYPE_CHECKING:
    from .views import TeamInputView, GroupRosterView

logger = get_logger('modals')


class TeamModal(Modal):
    """
    팀 정보 입력 모달
    
    새로운 팀 등록을 위한 폼을 제공합니다.
    팀명, 선수 4명, 스태프 1명의 정보를 입력받습니다.
    """
    
    def __init__(self, view: 'TeamInputView', user: discord.Member, default_team_name: str = "", default_players: str = "", default_staff: str = ""):
        super().__init__(title="팀 정보 입력")
        self.view = view
        self.user = user

        # 팀명 입력
        self.team_name_input = TextInput(
            label="팀명 (3~12글자, 한글/영어)",
            placeholder="예: Team ER",
            min_length=3,
            max_length=12,
            required=True,
            default=default_team_name or None,
        )
        self.add_item(self.team_name_input)

        # 플레이어 입력 (엔터키로 구분)
        self.players_input = TextInput(
            label="플레이어 (3~4명)",
            placeholder="한 줄에 하나씩 입력",
            max_length=200,
            required=True,
            style=discord.TextStyle.paragraph,
            default=default_players or None,
        )
        self.add_item(self.players_input)

        # 스태프 입력 (엔터키로 구분)
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
        """모달 제출 처리"""
        try:
            # 즉시 응답하여 모달을 닫음
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            # 임시 메시지 전송
            temp_message = await interaction.followup.send(view=processing_view("팀 정보를 확인하고 등록하고 있습니다."), ephemeral=True, wait=True)

            # 입력 데이터 수집
            team_name = self.team_name_input.value.strip()

            # 플레이어 목록 파싱 (엔터키로 구분)
            players_text = self.players_input.value.strip()
            players = [player.strip() for player in players_text.split('\n') if player.strip()]

            # 스태프 목록 파싱 (엔터키로 구분)
            staff_text = self.staff_input.value.strip()
            staff = [staff_member.strip() for staff_member in staff_text.split('\n') if staff_member.strip()]

            # 팀 데이터 구성
            team_data = {
                'players': players,
                'staff': staff
            }
            
            # 유효성 검사
            is_name_valid, name_error = validate_team_name(team_name)
            if not is_name_valid:
                await update_temp_message(temp_message, name_error, discord.Color.red())
                return

            # 팀원 중복 검사
            is_duplicate_valid, duplicate_error = check_duplicate_members(players, staff)
            if not is_duplicate_valid:
                await update_temp_message(temp_message, duplicate_error, discord.Color.red())
                return
            
            is_valid, error_message = validate_team_data(team_data)
            if not is_valid:
                await update_temp_message(temp_message, error_message, discord.Color.red())
                return
            
            # 팀원 중 테스트 계정이 있는지 확인
            team_processor = BotManager.get_instance().get_team_processor()
            
            # 테스트 계정이 포함된 경우 디스코드 닉네임 확인 생략
            all_members = players + staff
            has_test_account = any(team_processor._is_test_account(member) for member in all_members)
            
            # 디스코드 사용자가 팀에 포함되어 있는지 확인 (테스트 계정이 없는 경우에만)
            if not has_test_account:
                submitter_name = self.user.display_name
                if not validate_discord_user_in_team(team_data, submitter_name):
                    error_msg = (f"❌ 본인의 디스코드 닉네임이 팀원 목록에 포함되어 있지 않습니다.\n\n"
                               f"📌 **참가팀의 팀원만 신청할 수 있습니다.**\n\n"
                               f"**현재 디스코드 닉네임**: {submitter_name}\n"
                               f"**입력된 팀원**: {', '.join(players + staff) if players and staff and isinstance(players, (list, tuple)) and isinstance(staff, (list, tuple)) else '정보 없음'}\n\n"
                               f"💡 플레이어 또는 스태프 목록에 본인의 디스코드 닉네임을 포함해주세요.")
                    await update_temp_message(temp_message, error_msg, discord.Color.red())
                    return
            
            # 팀 등록 처리
            await self.view._process_team_registration(interaction, team_name, team_data, temp_message)
            
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
    
    def __init__(self, view: Union['GroupRosterView', 'TeamInputView'], team_data: Tuple[str, 'TeamData', float]):
        super().__init__(title="팀 정보 수정")
        self.view = view
        self.original_team_name, self.original_team_data, self.original_mmr = team_data

        # 팀명 입력
        self.team_name_input = TextInput(
            label="팀명 (3~12글자, 한글/영어)",
            placeholder="예: Team ER",
            min_length=3,
            max_length=12,
            required=True,
            default=self.original_team_name
        )
        self.add_item(self.team_name_input)

        # 플레이어 입력 (엔터키로 구분)
        if isinstance(self.original_team_data, dict):
            original_players = self.original_team_data.get('players', [])
        else:
            # TeamData 객체인 경우
            original_players = getattr(self.original_team_data, 'players', [])
        players_text = '\n'.join(original_players) if original_players and isinstance(original_players, (list, tuple)) else ''

        self.players_input = TextInput(
            label="플레이어 (3~4명)",
            placeholder="한 줄에 하나씩 입력",
            max_length=200,
            required=True,
            style=discord.TextStyle.paragraph,
            default=players_text
        )
        self.add_item(self.players_input)

        # 스태프 입력 (엔터키로 구분)
        if isinstance(self.original_team_data, dict):
            original_staff = self.original_team_data.get('staff', [])
        else:
            # TeamData 객체인 경우
            original_staff = getattr(self.original_team_data, 'staff', [])
        staff_text = '\n'.join(original_staff) if original_staff and isinstance(original_staff, (list, tuple)) else ''

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
        from commands.ui.views import GroupRosterView
        self.warning_checkbox = None
        self.warning_reason_input = None
        if isinstance(view, GroupRosterView):
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
        """모달 제출 처리"""
        try:
            # 즉시 응답하여 모달을 닫음
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            
            # 임시 메시지 전송
            temp_message = await interaction.followup.send(view=processing_view("변경된 팀 정보를 확인하고 업데이트하고 있습니다."), ephemeral=True, wait=True)
            
            from commands.ui.views import GroupRosterView
            is_roster_change = isinstance(self.view, GroupRosterView)
            
            # 입력 데이터 수집
            new_team_name = self.team_name_input.value.strip()
            
            # 플레이어 목록 파싱 (엔터키로 구분)
            players_text = self.players_input.value.strip()
            new_players = [player.strip() for player in players_text.split('\n') if player.strip()]
            
            # 스태프 목록 파싱 (엔터키로 구분)
            staff_text = self.staff_input.value.strip()
            new_staff = [staff_member.strip() for staff_member in staff_text.split('\n') if staff_member.strip()]
            
            # 팀 데이터 구성
            new_team_data = {
                'players': new_players,
                'staff': new_staff
            }
            
            # 관리자 로스터 변경은 모든 검증을 건너뜀
            if not is_roster_change:
                # 유효성 검사
                is_name_valid, name_error = validate_team_name(new_team_name)
                if not is_name_valid:
                    await update_temp_message(temp_message, name_error, discord.Color.red())
                    return
                
                # 팀원 중복 검사
                is_duplicate_valid, duplicate_error = check_duplicate_members(new_players, new_staff)
                if not is_duplicate_valid:
                    await update_temp_message(temp_message, duplicate_error, discord.Color.red())
                    return
                
                is_valid, error_message = validate_team_data(new_team_data)
                if not is_valid:
                    await update_temp_message(temp_message, error_message, discord.Color.red())
                    return
            
            # 팀 정보 수정 처리
            await self._process_team_edit(interaction, new_team_name, new_team_data, temp_message)
            
        except discord.NotFound:
            logger.warning("[모달] 팀 수정 interaction 만료")
        except Exception as e:
            logger.error(f"[모달] 팀 정보 수정 모달 제출 처리 실패: {e}", exc_info=True)
            await send_error_message(interaction, "팀 정보 수정 중 오류가 발생했습니다.")
    
    async def _process_team_edit(self, interaction: discord.Interaction, new_team_name: str, new_team_data: dict, temp_message: discord.Message) -> None:
        """팀 정보 수정 처리"""
        try:
            from models.team_processor import TeamProcessor
            from services.bser_api import BSERAPIClient
            from config.settings import settings
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            is_maintenance = False

            # 조편성 시작 여부 확인
            # GroupRosterView인지 확인하여 로스터 변경인지 판단
            from commands.ui.views import GroupRosterView
            is_roster_change = isinstance(self.view, GroupRosterView)
            
            if team_data_manager.is_team_assignment_started:
                # 조편성 이후에는 개별 팀 수정 불가 (관리자 로스터 변경만 가능)
                if not is_roster_change:
                    await update_temp_message(temp_message, "17시 조편성이 완료되어 팀 수정이 불가능합니다.", discord.Color.red())
                    return
            
            # 조별 공지 로스터 변경(admin) 시 모든 검증을 건너뜀
            if not is_roster_change:
                # 팀 수정 가능 여부 확인 (시간 제한 및 예비팀 상태)
                from utils.helpers import get_current_kst_time
                current_time = get_current_kst_time()
                is_allowed, error_message = await team_data_manager.check_team_edit_allowed(current_time)
                
                if not is_allowed:
                    await update_temp_message(temp_message, error_message, discord.Color.red())
                    return
                
                # 팀명이 변경된 경우 중복 검사
                if new_team_name != self.original_team_name:
                    # TeamData 객체에서 안전하게 플레이어와 스태프 추출
                    players = new_team_data.get('players', []) if isinstance(new_team_data, dict) else getattr(new_team_data, 'players', [])
                    staff = new_team_data.get('staff', []) if isinstance(new_team_data, dict) else getattr(new_team_data, 'staff', [])
                    team_members = (players if isinstance(players, (list, tuple)) else []) + (staff if isinstance(staff, (list, tuple)) else [])
                    
                    # 조 내 팀 수정인지 개별 팀 수정인지 확인
                    from commands.ui.views import GroupRosterView
                    is_roster_change = isinstance(self.view, GroupRosterView)
                    if is_roster_change:  # 조 내 팀 수정
                        # 같은 조 내의 다른 팀들과 중복 검사
                        is_group_valid, group_error = self._check_duplicate_within_group(new_team_name, team_members)
                        if not is_group_valid:
                            await update_temp_message(temp_message, group_error, discord.Color.red())
                            return
                    else:  # 개별 팀 수정
                        # 모든 팀과 중복 검사 (기존 팀 제외)
                        is_bot_valid, bot_error = team_data_manager.check_duplicate_with_bot_teams(new_team_name, team_members, exclude_team=self.original_team_name)
                        if not is_bot_valid:
                            await update_temp_message(temp_message, bot_error, discord.Color.red())
                            return
                
                # TeamData 객체에서 안전하게 플레이어와 스태프 추출
                from utils.helpers import get_team_members, get_all_members
                players, staff = get_team_members(new_team_data)
                all_members = players + staff
                
                # 팀원 중 테스트 계정이 있는지 확인
                team_processor = BotManager.get_instance().get_team_processor()
                
                has_test_account = any(team_processor._is_test_account(member) for member in all_members)

                is_maintenance = False
                # 닉네임 검증 (테스트 계정이 없는 경우에만)
                if not has_test_account:
                    # 디스코드 서버 멤버 검증 (로컬, API 호출 전에 먼저 수행)
                    client = BotManager.get_instance().get_client()
                    guild = client.get_guild(settings.GUILD_ID) if client else None

                    if guild:
                        is_guild_valid, not_found_members = validate_members_in_guild(guild, all_members)
                        if not is_guild_valid:
                            not_found_str = ', '.join(not_found_members)
                            error_msg = (
                                f"❌ 다음 닉네임이 디스코드 서버에서 확인되지 않습니다.\n\n"
                                f"**{not_found_str}**\n\n"
                                f"💡 디스코드 서버 닉네임과 동일하게 입력해주세요."
                            )
                            await update_temp_message(temp_message, error_msg, discord.Color.red())
                            return

                # API 닉네임 검증 (게임 내 닉네임 확인)
                if not has_test_account:
                    api_invalid_members = []
                    is_maintenance = False
                    api_error = False
                    try:
                        async with BSERAPIClient() as client_instance:
                            for member in all_members:
                                if not await client_instance.get_user_uid(member):
                                    api_invalid_members.append(member)

                            # 과반수 이상 조회 실패 시 서버 점검 확인
                            if api_invalid_members and len(api_invalid_members) >= len(all_members) / 2:
                                if team_data_manager._is_maintenance:
                                    is_maintenance = True
                                else:
                                    try:
                                        is_maintenance = await client_instance.check_server_maintenance()
                                    except Exception as e:
                                        logger.warning(f"[모달] 서버 점검 확인 실패: {e}")
                                        is_maintenance = True
                    except Exception as e:
                        logger.error(f"[모달] API 닉네임 검증 실패: {e}", exc_info=True)
                        api_error = True
                        if team_data_manager._is_maintenance:
                            is_maintenance = True
                        else:
                            try:
                                async with BSERAPIClient() as check_client:
                                    is_maintenance = await check_client.check_server_maintenance()
                            except Exception:
                                is_maintenance = True

                    if is_maintenance:
                        # 점검 중: API 검증 스킵, 수정은 진행
                        pass
                    elif api_error:
                        # API 예외로 검증 자체가 실패한 경우
                        await update_temp_message(temp_message, "⚠️ 닉네임 확인 중 문제가 발생했습니다.\n\n💡 잠시 후 다시 시도해주세요.", discord.Color.red())
                        return
                    elif api_invalid_members:
                        await update_temp_message(
                            temp_message,
                            f"❌ 다음 닉네임들을 찾을 수 없습니다.\n**{', '.join(api_invalid_members) if api_invalid_members and isinstance(api_invalid_members, (list, tuple)) else str(api_invalid_members)}**\n\n💡 게임 내 닉네임을 정확히 입력했는지 확인해주세요.",
                            discord.Color.red()
                        )
                        return
            
            # MMR 계산 (실패 시 기존 MMR 유지)
            original_mmr = self.original_team_data.mmr if hasattr(self.original_team_data, 'mmr') else (self.original_team_data.get('mmr', 0.0) if isinstance(self.original_team_data, dict) else 0.0)
            new_team_mmr = original_mmr
            try:
                team_processor = BotManager.get_instance().get_team_processor()
                _, _, fetched_mmr = await team_processor.fetch_team_mmr(new_team_name, new_team_data)
                if fetched_mmr > 0:
                    new_team_mmr = fetched_mmr
            except Exception as e:
                logger.error(f"[모달] 팀 MMR 계산 실패 - 팀명: {new_team_name}: {e}", exc_info=True)
                # MMR 계산 실패해도 팀 수정은 진행 (기존 MMR 유지)

            # 팀 데이터 업데이트 (인덱스/캐시 일관성 유지)
            from models.team_data import TeamData
            team_data_obj = TeamData(
                name=new_team_name,
                players=new_team_data['players'],
                staff=new_team_data['staff'],
                user_id=str(interaction.user.id),
                created_at=interaction.created_at
            )
            await team_data_manager.replace_team(self.original_team_name, team_data_obj, new_team_mmr)

            # 점검 중 수정 시 unverified 처리
            if is_maintenance:
                # 닉네임이 변경된 경우에만 unverified 추가
                if isinstance(self.original_team_data, dict):
                    _orig_players = self.original_team_data.get('players', [])
                else:
                    _orig_players = getattr(self.original_team_data, 'players', [])
                old_players_norm = set(normalize_nickname_for_comparison(p) for p in _orig_players)
                new_players_norm = set(normalize_nickname_for_comparison(p) for p in new_team_data['players'])
                if old_players_norm != new_players_norm:
                    team_data_manager.unverified_teams.add(new_team_name)
                # 팀명 변경 시 기존 팀명 제거
                if new_team_name != self.original_team_name:
                    team_data_manager.unverified_teams.discard(self.original_team_name)
                team_data_manager._save_backup()
            else:
                # 정상 수정으로 검증 통과 → unverified 제거
                team_data_manager.unverified_teams.discard(new_team_name)
                if new_team_name != self.original_team_name:
                    team_data_manager.unverified_teams.discard(self.original_team_name)

            # 변경사항 diff 계산
            if isinstance(self.original_team_data, dict):
                old_players = set(self.original_team_data.get('players', []))
                old_staff = set(self.original_team_data.get('staff', []))
            else:
                old_players = set(getattr(self.original_team_data, 'players', []))
                old_staff = set(getattr(self.original_team_data, 'staff', []))
            new_players = set(new_team_data['players'])
            new_staff = set(new_team_data['staff'])

            added = (new_players | new_staff) - (old_players | old_staff)
            removed = (old_players | old_staff) - (new_players | new_staff)

            has_change = (self.original_team_name != new_team_name) or added or removed
            if has_change:
                players_str = ', '.join(new_team_data['players'])
                staff_str = ', '.join(new_team_data['staff']) if new_team_data['staff'] else '(없음)'
                parts = []
                if self.original_team_name != new_team_name:
                    parts.append(f"{self.original_team_name} → {new_team_name}")
                if removed:
                    parts.append(f"{', '.join(sorted(removed))} → {', '.join(sorted(added))}" if added else f"-{', '.join(sorted(removed))}")
                elif added:
                    parts.append(f"+{', '.join(sorted(added))}")
                detail = ' · '.join(parts) + f" · 선수: {players_str} · 스태프: {staff_str}"
                team_data_manager.log_action("수정", interaction.user, new_team_name, detail=detail)
            
            # 변경된 팀 데이터 업데이트 (조 내 팀 수정 시에만)
            from commands.ui.views import GroupRosterView
            is_roster_change = isinstance(self.view, GroupRosterView)
            if is_roster_change:
                await self._update_changed_team(team_data_manager, new_team_name, new_team_mmr)
            
            # 해당 조의 MMR 메시지 업데이트 (전체 MMR 메시지는 업데이트하지 않음)
            # 로스터 변경 시에는 조별 공지만 업데이트
            
            # 성공 메시지로 임시 메시지 업데이트
            if is_maintenance:
                await update_temp_message(
                    temp_message,
                    f"**{self.original_team_name}** → **{new_team_name}**\n\n"
                    f"🔧 서버 점검으로 닉네임 확인을 건너뛰었습니다.\n"
                    f"점검 종료 후 자동으로 확인되며, 결과는 DM으로 안내드립니다.\n"
                    f"💡 닉네임 오타가 없는지 다시 한번 확인해주세요.",
                    discord.Color.green()
                )
            else:
                await update_temp_message(
                    temp_message,
                    f"**{self.original_team_name}** → **{new_team_name}**\n팀 평균 MMR: **{new_team_mmr:.2f}**",
                    discord.Color.green()
                )
            # 변경 전후 정보 로깅
            if isinstance(self.original_team_data, dict):
                original_players = self.original_team_data.get('players', [])
                original_staff = self.original_team_data.get('staff', [])
            else:
                # TeamData 객체인 경우
                original_players = getattr(self.original_team_data, 'players', [])
                original_staff = getattr(self.original_team_data, 'staff', [])
            new_players = new_team_data['players']
            new_staff = new_team_data['staff']

            original_players_str = ', '.join(original_players) if original_players else '(없음)'
            original_staff_str = ', '.join(original_staff) if original_staff else '(없음)'
            new_players_str = ', '.join(new_players) if new_players else '(없음)'
            new_staff_str = ', '.join(new_staff) if new_staff else '(없음)'

            logger.info(
                f"[팀수정] {self.original_team_name} → {new_team_name} | MMR: {new_team_mmr:.2f} | "
                f"선수: [{original_players_str}] → [{new_players_str}] | "
                f"스태프: [{original_staff_str}] → [{new_staff_str}]"
            )
            
            # 로스터 변경 시 주의 부여 처리
            if is_roster_change and self.warning_checkbox and self.warning_checkbox.values:
                await self._apply_roster_warnings(interaction, original_players, temp_message)

            # 조별 공지 업데이트 (조 내 팀 수정 시에만)
            if is_roster_change:
                await self._update_group_announcement(interaction)
            else:
                # 개별 팀 수정 시에는 MMR 메시지만 업데이트
                await self._update_mmr_message_for_individual_team(team_data_manager)

        except Exception as e:
            logger.error(f"[모달] 팀 정보 수정 실패: {e}", exc_info=True)
            await send_error_message(interaction, "팀 정보 수정 중 오류가 발생했습니다.")
    
    async def _apply_roster_warnings(self, interaction: discord.Interaction, original_players: list, temp_message: discord.Message) -> None:
        """로스터 변경 시 빠지는 팀 선수에게 주의를 부여합니다."""
        try:
            reason = self.warning_reason_input.value.strip() if self.warning_reason_input and self.warning_reason_input.value else "대타"
            if not reason:
                reason = "대타"

            admin_name = interaction.user.display_name or interaction.user.name
            warning_manager = BotManager.get_instance().get_warning_manager()

            # 길드 멤버 매핑 (닉네임 → Member)
            from config.settings import settings
            client = BotManager.get_instance().get_client()
            guild = client.get_guild(settings.GUILD_ID) if client else None

            member_map = {}
            if guild:
                for m in guild.members:
                    member_map[normalize_nickname_for_comparison(m.display_name)] = m
                    if m.global_name:
                        member_map[normalize_nickname_for_comparison(m.global_name)] = m
                    member_map[normalize_nickname_for_comparison(m.name)] = m

            success_count = 0
            fail_names = []

            for player in original_players:
                discord_member = member_map.get(normalize_nickname_for_comparison(player))
                target_id = str(discord_member.id) if discord_member else ""
                target_name = discord_member.display_name if discord_member else player

                success, message, auto_warning, converted_cautions = await warning_manager.add_warning(
                    target=target_name,
                    target_id=target_id,
                    warning_type="주의",
                    reason=reason,
                    admin_display_name=admin_name,
                )

                if success:
                    success_count += 1
                    # DM 발송
                    if discord_member:
                        try:
                            await self._send_roster_warning_dm(discord_member, reason, auto_warning, converted_cautions)
                        except Exception as e:
                            logger.warning(f"[로스터주의] DM 발송 실패 - 대상: {target_name}, 오류: {e}")
                else:
                    fail_names.append(target_name)
                    logger.error(f"[로스터주의] 주의 부여 실패 - 대상: {target_name}, 메시지: {message}")

            # 결과 메시지 추가
            result_parts = [f"주의 {success_count}명 부여 완료"]
            if fail_names:
                result_parts.append(f"실패: {', '.join(fail_names)}")
            result_text = " | ".join(result_parts)

            try:
                current_content = temp_message.content if hasattr(temp_message, 'content') else ""
                await update_temp_message(
                    temp_message,
                    f"**{self.original_team_name}** → 로스터 변경 완료\n⚡ {result_text}",
                    discord.Color.green()
                )
            except Exception:
                pass

            logger.info(f"[로스터주의] {self.original_team_name} - {result_text} (사유: {reason})")

        except Exception as e:
            logger.error(f"[로스터주의] 주의 부여 처리 실패: {e}", exc_info=True)

    async def _send_roster_warning_dm(
        self,
        target_user: discord.Member,
        reason: str,
        auto_warning: dict = None,
        converted_cautions: list = None,
    ) -> None:
        """로스터 변경으로 인한 주의 DM을 발송합니다."""
        try:
            if auto_warning and converted_cautions:
                restricted_until = auto_warning.get('restricted_until', 'N/A')
                caution_lines = []
                for i, caution in enumerate(converted_cautions, 1):
                    caution_date = caution.get('날짜', 'N/A')
                    caution_reason = caution.get('사유', 'N/A')
                    caution_lines.append(f"`{i}회` {caution_date}\n└ {caution_reason}")
                fields = [
                    ("📋 누적 주의 내역", "\n\n".join(caution_lines) if caution_lines else "내역 없음"),
                    ("🚫 참여 제한", f"**{restricted_until}**까지 스크림 참여가 제한됩니다."),
                ]
                dm_view = custom_view("🚨 경고 알림", "주의 2회 누적으로 인해 **경고**가 부여되었습니다.", discord.Color.red(), fields=fields)
            else:
                fields = [
                    ("📝 사유", reason),
                    ("💡 안내", "주의 2회 누적 시 경고로 전환되며,\n스크림 참여가 제한됩니다."),
                ]
                dm_view = custom_view("⚡ 주의 알림", "**주의**가 부여되었습니다.", discord.Color.from_str("#FEE75C"), fields=fields)

            await target_user.send(view=dm_view)

        except discord.Forbidden:
            logger.warning(f"[로스터주의] DM 발송 실패 (DM 차단) - 대상: {target_user.display_name}")
        except Exception as e:
            logger.error(f"[로스터주의] DM 발송 실패 - 대상: {target_user.display_name}, 오류: {e}", exc_info=True)

    def _check_duplicate_within_group(self, new_team_name: str, new_team_members: List[str]) -> Tuple[bool, str]:
        """같은 조 내에서만 중복 검사"""
        try:
            from utils.validators import normalize_nickname_for_comparison, normalize_team_name

            normalized_new_team = normalize_team_name(new_team_name)
            normalized_new_members = [normalize_nickname_for_comparison(member) for member in new_team_members]

            # 같은 조 내의 다른 팀들과 중복 검사
            for team_name, team_data, mmr in self.view.group_teams:
                if team_name == self.original_team_name:
                    continue  # 현재 수정 중인 팀은 제외
                
                # 팀명 중복 검사
                if normalize_team_name(team_name) == normalized_new_team:
                    return False, f"'{new_team_name}' 팀명이 이미 같은 조에 존재합니다."
                
                # 팀원 중복 검사
                from utils.helpers import get_team_members
                if hasattr(team_data, 'players') or isinstance(team_data, dict):
                    existing_players, existing_staff = get_team_members(team_data)
                else:
                    existing_players = list(team_data)
                    existing_staff = []
                
                existing_members = existing_players + existing_staff
                normalized_existing = [normalize_nickname_for_comparison(member) for member in existing_members]
                
                # 새 팀원이 기존 팀에 속해있는지 확인
                for new_member_norm, new_member_raw in zip(normalized_new_members, new_team_members):
                    if new_member_norm in normalized_existing:
                        return False, f"'{new_member_raw}' 닉네임이 이미 같은 조의 다른 팀에 등록되어 있습니다."
            
            return True, ""
            
        except Exception as e:
            logger.error(f"[모달] 조 내 중복 검사 실패: {e}", exc_info=True)
            return False, "중복 검사 중 오류가 발생했습니다."
    
    async def _update_mmr_message_for_individual_team(self, team_data_manager) -> None:
        """개별 팀 수정 시 MMR 메시지 업데이트"""
        try:
            # MMR 메시지가 있는 경우 해당 채널로 업데이트
            if team_data_manager.mmr_message and team_data_manager.mmr_message.channel:
                await team_data_manager.update_mmr_message(team_data_manager.mmr_message.channel)
            else:
                logger.warning("[모달] MMR 메시지 또는 채널 정보가 없어 업데이트 건너뜀")
        except Exception as e:
            logger.error(f"[모달] 개별 팀 수정 후 MMR 메시지 업데이트 실패: {e}", exc_info=True)
    
    async def _update_changed_team(self, team_data_manager, new_team_name: str, new_team_mmr: float) -> None:
        """변경된 팀의 데이터만 업데이트 (팀 번호/순서 유지)"""
        try:
            # 변경된 팀의 인덱스 찾기
            changed_index = None
            for i, (team_name, team_data, mmr) in enumerate(self.view.group_teams):
                if team_name == self.original_team_name:
                    changed_index = i
                    break

            if changed_index is None:
                logger.warning(f"[모달] 변경된 팀을 찾을 수 없음: {self.original_team_name}")
                return

            # 해당 팀만 새 데이터로 교체 (순서 유지)
            updated_team_data = team_data_manager.teams[new_team_name]
            group_teams = list(self.view.group_teams)
            group_teams[changed_index] = (new_team_name, updated_team_data, new_team_mmr)

            # view의 group_teams 업데이트 (로스터 변경 메뉴에 반영)
            self.view.update_group_teams(group_teams)

            # team_data_manager.groups도 동기화
            group_index = ord(self.view.group_letter) - ord('A')
            if team_data_manager.groups and 0 <= group_index < len(team_data_manager.groups):
                team_data_manager.groups[group_index] = group_teams
                team_data_manager._save_backup()

            # 조별 역할 업데이트
            await self._update_group_roles(group_teams)

            # 변경된 팀의 음성채널 이름만 변경
            await self._update_voice_channel_for_team(changed_index, new_team_name)

        except Exception as e:
            logger.error(f"[모달] 팀 데이터 업데이트 실패: {e}", exc_info=True)
    
    async def _update_group_roles(self, group_teams: List[Tuple[str, 'TeamData', float]]) -> None:
        """조별 역할을 업데이트합니다."""
        try:
            from config.settings import settings

            client = BotManager.get_instance().get_client()
            team_processor = BotManager.get_instance().get_team_processor()
            guild = client.get_guild(settings.GUILD_ID)

            if not guild:
                logger.warning("[모달] 서버 정보를 찾을 수 없음")
                return

            group_letter = self.view.group_letter

            await team_processor.update_group_roles(guild, group_letter, group_teams)
            
        except Exception as e:
            logger.error(f"[모달] 조별 역할 업데이트 실패: {e}", exc_info=True)
    
    async def _update_voice_channel_for_team(self, team_index: int, new_team_name: str) -> None:
        """변경된 팀의 음성채널 이름만 변경"""
        try:
            from config.settings import settings

            client = BotManager.get_instance().get_client()
            guild = client.get_guild(settings.GUILD_ID)

            if not guild:
                logger.warning("[모달] 서버 정보를 찾을 수 없음")
                return

            group_letter = self.view.group_letter
            category_name = settings.GROUP_CATEGORY_PATTERN.format(letter=group_letter)

            if not category_name:
                logger.warning(f"[모달] 카테고리 패턴이 설정되지 않음 - 조: {group_letter}조")
                return

            category = discord.utils.get(guild.categories, name=category_name)
            if not category:
                logger.warning(f"[모달] 카테고리를 찾을 수 없음 - 카테고리: {category_name}")
                return

            voice_channels = [ch for ch in category.voice_channels if isinstance(ch, discord.VoiceChannel)]
            voice_channels.sort(key=lambda x: x.position)

            if team_index < len(voice_channels):
                voice_channel = voice_channels[team_index]
                new_name = f"{team_index + 1}. {new_team_name}"
                try:
                    await voice_channel.edit(name=new_name)
                except discord.HTTPException as e:
                    logger.error(f"[모달] 음성채널 이름 변경 실패: {e}", exc_info=True)
                except discord.Forbidden:
                    logger.warning("[모달] 음성채널 이름 변경 권한 없음")

        except Exception as e:
            logger.error(f"[모달] 음성채널 이름 변경 실패: {e}", exc_info=True)
    
    async def _update_group_announcement(self, interaction: discord.Interaction) -> None:
        """조별 공지 업데이트"""
        try:
            from config.settings import settings
            
            client = BotManager.get_instance().get_client()
            guild = client.get_guild(settings.GUILD_ID)
            
            if not guild:
                logger.warning("[모달] 서버 정보를 찾을 수 없음")
                return
            
            group_letter = self.view.group_letter
            channel_id = settings.GROUP_CHANNEL_IDS.get(group_letter)
            
            if not channel_id:
                logger.warning(f"[모달] 조별 채널 ID가 설정되지 않음 - 조: {group_letter}조")
                return
            
            channel = guild.get_channel(channel_id)
            if not channel:
                logger.warning(f"[모달] 조별 채널을 찾을 수 없음 - 조: {group_letter}조")
                return
            
            # 현재 조의 팀들 수집 (업데이트된 정보로)
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            
            # view의 group_teams를 사용 (이미 업데이트됨)
            updated_group_teams = self.view.group_teams.copy()
            
            # 기존 조별 공지 메시지 찾기 및 수정
            await self._update_existing_group_announcement(channel, group_letter, updated_group_teams)
            
        except Exception as e:
            logger.error(f"[모달] 조별 공지 업데이트 실패: {e}", exc_info=True)
    
    async def _update_existing_group_announcement(self, channel: discord.TextChannel, group_letter: str, updated_group_teams: List[Tuple[str, 'TeamData', float]]) -> None:
        """기존 조별 공지 메시지를 찾아서 수정합니다."""
        try:
            # 저장된 message_id로 직접 fetch
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            message_id = team_data_manager.group_message_ids.get(group_letter)
            target_message = None
            if message_id:
                try:
                    target_message = await channel.fetch_message(message_id)
                except discord.NotFound:
                    logger.warning(f"[모달] 저장된 공지 메시지를 찾을 수 없음 (id={message_id})")

            if not target_message:
                logger.warning(f"[모달] 조별 공지 메시지를 찾을 수 없음 - 조: {group_letter}조")
                return
            
            # 조별 팀 데이터 수집 (삽입 순서 = 팀 번호 순서)
            group_teams = {}
            for team_name, team_data, team_mmr in updated_group_teams:
                group_teams[team_name] = team_data

            # 새로운 MMR 이미지 생성 (팀 번호 순서 유지)
            from services.image_generator import ImageGenerator
            img_io = ImageGenerator.generate_mmr_image(group_teams, sort_by_mmr=False)
            
            # 조별 공지 메시지 생성
            team_processor = BotManager.get_instance().get_team_processor()
            message_content = team_processor._create_group_announcement_message(group_letter, updated_group_teams)

            # 조별 역할 멘션 추가
            role_mention = await team_processor._get_group_role_mention(channel.guild, group_letter)
            if role_mention:
                message_content = role_mention + "\n" + message_content

            # 새로운 로스터 뷰 생성 (공지 텍스트 + 이미지 + 버튼 포함)
            from commands.ui.views import GroupRosterView
            roster_view = GroupRosterView(
                group_letter, updated_group_teams,
                message_text=message_content, has_image=bool(img_io),
            )

            # 기존 메시지 수정
            if img_io:
                await target_message.edit(
                    view=roster_view,
                    content=None,
                    embed=None,
                    attachments=[discord.File(img_io, filename='group_mmr_table.png')],
                )
            else:
                await target_message.edit(
                    view=roster_view,
                    content=None,
                    embed=None,
                )
            
            # 백업에 갱신된 텍스트 저장
            team_data_manager.group_message_texts[group_letter] = message_content
            team_data_manager._save_backup()

            logger.debug(f"[모달] 조별 공지 업데이트 완료 - 조: {group_letter}조")

        except Exception as e:
            logger.error(f"[모달] 기존 조별 공지 메시지 수정 실패: {e}", exc_info=True)


