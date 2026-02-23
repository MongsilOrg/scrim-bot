"""
Discord Modal 컴포넌트들
"""
from typing import TYPE_CHECKING, Dict, List, Tuple, Union

import discord
from discord import SelectOption
from discord.ui import Label, Modal, Select, TextDisplay, TextInput

from bot.manager import BotManager
from config.logging_config import get_logger
from utils.validators import (
    check_duplicate_members,
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
    
    def __init__(self, view: 'TeamInputView', user: discord.Member):
        super().__init__(title="팀 정보 입력")
        self.view = view
        self.user = user

        # 팀명 입력
        self.team_name_input = TextInput(
            label="팀명 (3~8글자, 한글/영어)",
            placeholder="예: Team ER",
            min_length=3,
            max_length=8,
            required=True
        )
        self.add_item(self.team_name_input)

        # 플레이어 입력 (엔터키로 구분)
        self.players_input = TextInput(
            label="플레이어 (3~4명)",
            placeholder="한 줄에 하나씩 입력",
            max_length=200,
            required=True,
            style=discord.TextStyle.paragraph
        )
        self.add_item(self.players_input)

        # 스태프 입력 (엔터키로 구분)
        self.staff_input = TextInput(
            label="스태프 (선택사항)",
            placeholder="한 줄에 하나씩 입력",
            max_length=200,
            required=False,
            style=discord.TextStyle.paragraph
        )
        self.add_item(self.staff_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모달 제출 처리"""
        try:
            # 즉시 응답하여 모달을 닫음
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            # 임시 메시지 전송
            temp_embed = discord.Embed(
                title="⏳ 처리 중...",
                description="팀 정보를 확인하고 등록하고 있습니다.\n잠시만 기다려주세요.",
                color=discord.Color.blue()
            )
            temp_message = await interaction.followup.send(embed=temp_embed, ephemeral=True)

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
                await self._update_temp_message(temp_message, name_error, discord.Color.red())
                return

            # 팀원 중복 검사
            is_duplicate_valid, duplicate_error = check_duplicate_members(players, staff)
            if not is_duplicate_valid:
                await self._update_temp_message(temp_message, duplicate_error, discord.Color.red())
                return
            
            is_valid, error_message = validate_team_data(team_data)
            if not is_valid:
                await self._update_temp_message(temp_message, error_message, discord.Color.red())
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
                    await self._update_temp_message(temp_message, error_msg, discord.Color.red())
                    return
            
            # 팀 등록 처리
            await self.view._process_team_registration(interaction, team_name, team_data, temp_message)
            
        except Exception as e:
            logger.error(f"[모달] 팀 모달 제출 처리 실패: {e}", exc_info=True)
            await self._send_error_message(interaction, "팀 등록 중 오류가 발생했습니다.")
    
    async def _update_temp_message(self, temp_message: discord.Message, message: str, color: discord.Color) -> None:
        """임시 메시지를 업데이트합니다."""
        try:
            embed = discord.Embed(
                title="스크림 안내",
                description=message,
                color=color
            )
            await temp_message.edit(embed=embed)
        except Exception as e:
            logger.error(f"[모달] 임시 메시지 업데이트 실패: {e}", exc_info=True)

    async def _send_error_message(self, interaction: discord.Interaction, message: str) -> None:
        """에러 메시지를 전송합니다."""
        try:
            error_embed = discord.Embed(
                title="스크림 안내",
                description=message,
                color=discord.Color.red()
            )

            # 상호작용이 이미 응답되었는지 확인
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=error_embed, ephemeral=True)
            else:
                # 이미 응답된 경우 followup으로 전송
                await interaction.followup.send(embed=error_embed, ephemeral=True)
        except Exception as e:
            logger.error(f"[모달] 에러 메시지 전송 실패: {e}", exc_info=True)
            # 최후의 수단으로 일반 메시지 전송 시도
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"오류: {message}", ephemeral=True)
                else:
                    await interaction.followup.send(f"오류: {message}", ephemeral=True)
            except Exception as e2:
                logger.error(f"[모달] 최후의 수단 메시지 전송 실패: {e2}", exc_info=True)


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
            label="팀명 (3~8글자, 한글/영어)",
            placeholder="예: Team ER",
            min_length=3,
            max_length=8,
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
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모달 제출 처리"""
        try:
            # 즉시 응답하여 모달을 닫음
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            
            # 임시 메시지 전송
            temp_embed = discord.Embed(
                title="⏳ 처리 중...",
                description="변경된 팀 정보를 확인하고 업데이트하고 있습니다.\n잠시만 기다려주세요.",
                color=discord.Color.blue()
            )
            temp_message = await interaction.followup.send(embed=temp_embed, ephemeral=True)
            
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
                    await self._update_temp_message(temp_message, name_error, discord.Color.red())
                    return
                
                # 팀원 중복 검사
                is_duplicate_valid, duplicate_error = check_duplicate_members(new_players, new_staff)
                if not is_duplicate_valid:
                    await self._update_temp_message(temp_message, duplicate_error, discord.Color.red())
                    return
                
                is_valid, error_message = validate_team_data(new_team_data)
                if not is_valid:
                    await self._update_temp_message(temp_message, error_message, discord.Color.red())
                    return
            
            # 팀 정보 수정 처리
            await self._process_team_edit(interaction, new_team_name, new_team_data, temp_message)
            
        except Exception as e:
            logger.error(f"[모달] 팀 정보 수정 모달 제출 처리 실패: {e}", exc_info=True)
            await self._send_error_message(interaction, "팀 정보 수정 중 오류가 발생했습니다.")
    
    async def _process_team_edit(self, interaction: discord.Interaction, new_team_name: str, new_team_data: dict, temp_message: discord.Message) -> None:
        """팀 정보 수정 처리"""
        try:
            from models.team_processor import TeamProcessor
            from services.bser_api import BSERAPIClient
            from config.settings import settings
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            
            # 조편성 시작 여부 확인
            # GroupRosterView인지 확인하여 로스터 변경인지 판단
            from commands.ui.views import GroupRosterView
            is_roster_change = isinstance(self.view, GroupRosterView)
            
            if team_data_manager.is_team_assignment_started:
                # 조편성 이후에는 개별 팀 수정 불가 (관리자 로스터 변경만 가능)
                if not is_roster_change:
                    await self._update_temp_message(temp_message, "조 편성이 이미 시작되어 팀 수정이 불가능합니다.", discord.Color.red())
                    return
            
            # 조별 공지 로스터 변경(admin) 시 모든 검증을 건너뜀
            if not is_roster_change:
                # 팀 수정 가능 여부 확인 (시간 제한 및 예비팀 상태)
                from utils.helpers import get_current_kst_time
                current_time = get_current_kst_time()
                is_allowed, error_message = await team_data_manager.check_team_edit_allowed(current_time)
                
                if not is_allowed:
                    await self._update_temp_message(temp_message, error_message, discord.Color.red())
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
                            await self._update_temp_message(temp_message, group_error, discord.Color.red())
                            return
                    else:  # 개별 팀 수정
                        # 모든 팀과 중복 검사 (기존 팀 제외)
                        is_bot_valid, bot_error = team_data_manager.check_duplicate_with_bot_teams(new_team_name, team_members, exclude_team=self.original_team_name)
                        if not is_bot_valid:
                            await self._update_temp_message(temp_message, bot_error, discord.Color.red())
                            return
                
                # TeamData 객체에서 안전하게 플레이어와 스태프 추출
                players = new_team_data.get('players', []) if isinstance(new_team_data, dict) else getattr(new_team_data, 'players', [])
                staff = new_team_data.get('staff', []) if isinstance(new_team_data, dict) else getattr(new_team_data, 'staff', [])
                all_members = (players if isinstance(players, (list, tuple)) else []) + (staff if isinstance(staff, (list, tuple)) else [])
                
                # 팀원 중 테스트 계정이 있는지 확인
                team_processor = BotManager.get_instance().get_team_processor()
                
                has_test_account = any(team_processor._is_test_account(member) for member in all_members)

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
                            await self._update_temp_message(temp_message, error_msg, discord.Color.red())
                            return

                # API 닉네임 검증 (게임 내 닉네임 확인)
                if not has_test_account:
                    api_invalid_members = []
                    try:
                        async with BSERAPIClient() as client_instance:
                            for member in all_members:
                                if not await client_instance.get_user_uid(member):
                                    api_invalid_members.append(member)
                    except Exception as e:
                        logger.error(f"[모달] API 닉네임 검증 실패: {e}", exc_info=True)
                        await self._update_temp_message(temp_message, "⚠️ 닉네임 확인 중 문제가 발생했습니다.\n\n💡 잠시 후 다시 시도해주세요.", discord.Color.red())
                        return

                    if api_invalid_members:
                        await self._update_temp_message(
                            temp_message,
                            f"❌ 다음 닉네임들을 찾을 수 없습니다.\n**{', '.join(api_invalid_members) if api_invalid_members and isinstance(api_invalid_members, (list, tuple)) else str(api_invalid_members)}**\n\n💡 게임 내 닉네임을 정확히 입력했는지 확인해주세요.",
                            discord.Color.red()
                        )
                        return
            
            # MMR 계산
            new_team_mmr = 0.0
            try:
                team_processor = BotManager.get_instance().get_team_processor()
                _, _, new_team_mmr = await team_processor.fetch_team_mmr(new_team_name, new_team_data)
            except Exception as e:
                logger.error(f"[모달] 팀 MMR 계산 실패 - 팀명: {new_team_name}: {e}", exc_info=True)
                # MMR 계산 실패해도 팀 수정은 진행
            
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
            
            # 로그 기록
            team_data_manager.log_action("수정", interaction.user, new_team_name)
            
            # 조 내 순위 재정렬 (조 내 팀 수정 시에만)
            from commands.ui.views import GroupRosterView
            is_roster_change = isinstance(self.view, GroupRosterView)
            if is_roster_change:
                await self._reorder_group_teams(team_data_manager, new_team_name, new_team_mmr)
            
            # 해당 조의 MMR 메시지 업데이트 (전체 MMR 메시지는 업데이트하지 않음)
            # 로스터 변경 시에는 조별 공지만 업데이트
            
            # 성공 메시지로 임시 메시지 업데이트
            await self._update_temp_message(
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
                f"[팀수정] {self.original_team_name} → {new_team_name}, MMR: {new_team_mmr:.2f} | "
                f"선수: [{original_players_str}] → [{new_players_str}] | "
                f"스태프: [{original_staff_str}] → [{new_staff_str}]"
            )
            
            # 조별 공지 업데이트 (조 내 팀 수정 시에만)
            if is_roster_change:
                await self._update_group_announcement(interaction)
            else:
                # 개별 팀 수정 시에는 MMR 메시지만 업데이트
                await self._update_mmr_message_for_individual_team(team_data_manager)
            
        except Exception as e:
            logger.error(f"[모달] 팀 정보 수정 실패: {e}", exc_info=True)
            await self._send_error_message(interaction, "팀 정보 수정 중 오류가 발생했습니다.")
    
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
                if isinstance(team_data, dict):
                    existing_players = team_data.get('players', [])
                    existing_staff = team_data.get('staff', [])
                elif hasattr(team_data, 'players'):
                    existing_players = team_data.players
                    existing_staff = team_data.staff
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
    
    async def _reorder_group_teams(self, team_data_manager, new_team_name: str, new_team_mmr: float) -> None:
        """조 내 팀 순위 재정렬"""
        try:
            # 현재 조의 팀들만 수집
            group_teams = []
            for team_name, team_data, mmr in self.view.group_teams:
                if team_name == self.original_team_name:
                    # 수정된 팀은 새로운 정보로 업데이트
                    updated_team_data = team_data_manager.teams[new_team_name]
                    group_teams.append((new_team_name, updated_team_data, new_team_mmr))
                else:
                    # 기존 팀은 현재 MMR로 업데이트
                    current_mmr = team_data_manager.get_team_mmr(team_name) or mmr
                    # team_data_manager.teams에 없는 경우 view의 기존 데이터 사용
                    if team_name in team_data_manager.teams:
                        updated_team_data = team_data_manager.teams[team_name]
                    else:
                        updated_team_data = team_data
                    group_teams.append((team_name, updated_team_data, current_mmr))
            
            # MMR 기준으로 정렬
            group_teams.sort(key=lambda x: x[2], reverse=True)
            
            # view의 group_teams 업데이트 (로스터 변경 메뉴에 반영)
            self.view.update_group_teams(group_teams)
            
            # 조별 역할 업데이트 (제거된 팀의 역할 제거 및 새 팀의 역할 부여)
            await self._update_group_roles(group_teams)
            
            # 조별 음성채널 이름 변경
            await self._update_voice_channels(group_teams)
            
        except Exception as e:
            logger.error(f"[모달] 조 내 팀 순위 재정렬 실패: {e}", exc_info=True)
    
    async def _update_group_roles(self, sorted_teams: List[Tuple[str, 'TeamData', float]]) -> None:
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
            
            # TeamProcessor의 조별 역할 업데이트 메서드 호출
            await team_processor.update_group_roles(guild, group_letter, sorted_teams)
            
        except Exception as e:
            logger.error(f"[모달] 조별 역할 업데이트 실패: {e}", exc_info=True)
    
    async def _update_voice_channels(self, sorted_teams: List[Tuple[str, 'TeamData', float]]) -> None:
        """음성채널 이름을 MMR 순서대로 변경"""
        try:
            from config.settings import settings
            
            client = BotManager.get_instance().get_client()
            guild = client.get_guild(settings.GUILD_ID)
            
            if not guild:
                logger.warning("[모달] 서버 정보를 찾을 수 없음")
                return
            
            group_letter = self.view.group_letter
            category_name = settings.GROUP_CATEGORY_PATTERNS.get(group_letter)
            
            if not category_name:
                logger.warning(f"[모달] 카테고리 패턴이 설정되지 않음 - 조: {group_letter}조")
                return
            
            # 해당 카테고리 찾기
            category = discord.utils.get(guild.categories, name=category_name)
            if not category:
                logger.warning(f"[모달] 카테고리를 찾을 수 없음 - 카테고리: {category_name}")
                return
            
            # 카테고리 내의 음성채널들을 가져오기 (정렬)
            voice_channels = [ch for ch in category.voice_channels if isinstance(ch, discord.VoiceChannel)]
            voice_channels.sort(key=lambda x: x.position)  # 위치 순으로 정렬
            
            # 팀 순서대로 음성채널 이름 변경
            for i, (team_name, team_data, mmr) in enumerate(sorted_teams):
                if i < len(voice_channels):
                    voice_channel = voice_channels[i]
                    new_name = f"{i+1}. {team_name}"
                    
                    try:
                        await voice_channel.edit(name=new_name)
                    except discord.HTTPException as e:
                        logger.error(f"[모달] 음성채널 이름 변경 실패: {e}", exc_info=True)
                    except discord.Forbidden:
                        logger.warning("[모달] 음성채널 이름 변경 권한 없음")
            
            # 남은 채널들을 TBD로 변경
            for i in range(len(sorted_teams), len(voice_channels)):
                voice_channel = voice_channels[i]
                new_name = "TBD"
                
                try:
                    await voice_channel.edit(name=new_name)
                except discord.HTTPException as e:
                    logger.error(f"[모달] 음성채널 이름 변경 실패: {e}", exc_info=True)
                except discord.Forbidden:
                    logger.error("[모달] 음성채널 이름 변경 권한 없음")
            
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
            # 조별 공지 메시지 찾기 (가장 최근 메시지 중에서)
            target_message = None
            async for message in channel.history(limit=20):
                # 조별 공지 메시지인지 확인
                # 1. 메시지에 이미지가 있고
                # 2. 로스터 변경 버튼이 있는 메시지
                if (message.attachments and 
                    message.components and 
                    any(component.type.value == 2 for component in message.components[0].children)):  # 버튼이 있는 메시지
                    target_message = message
                    break
            
            if not target_message:
                logger.warning(f"[모달] 조별 공지 메시지를 찾을 수 없음 - 조: {group_letter}조")
                return
            
            # 조별 팀 데이터 수집
            group_teams = {}
            group_mmr_averages = {}
            
            for team_name, team_data, team_mmr in updated_group_teams:
                # TeamData 객체를 그대로 전달 (ImageGenerator가 TeamData 객체를 기대함)
                group_teams[team_name] = team_data
                group_mmr_averages[team_name] = team_mmr
            
            # 새로운 MMR 이미지 생성
            from services.image_generator import ImageGenerator
            img_io = ImageGenerator.generate_mmr_image(group_teams)
            
            # 새로운 로스터 뷰 생성
            from commands.ui.views import GroupRosterView
            roster_view = GroupRosterView(group_letter, updated_group_teams)
            
            # 조별 공지 메시지 생성
            team_processor = BotManager.get_instance().get_team_processor()
            message_content = team_processor._create_group_announcement_message(group_letter, updated_group_teams)
            
            # 조별 역할 멘션 추가
            role_mention = await team_processor._get_group_role_mention(channel.guild, group_letter)
            if role_mention:
                message_content = role_mention + "\n" + message_content
            
            # 기존 메시지 수정
            if img_io:
                await target_message.edit(
                    content=message_content,
                    embed=None,  # 기존 임베드 제거
                    attachments=[discord.File(img_io, filename='group_mmr_table.png')],
                    view=roster_view
                )
            else:
                await target_message.edit(
                    content=message_content,
                    embed=None,  # 기존 임베드 제거
                    view=roster_view
                )
            
            # 조편성 시작 여부에 따라 로깅 메시지 구분
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            if team_data_manager.is_team_assignment_started:
                logger.debug(f"[모달] 조별 공지 업데이트 완료 - 조: {group_letter}조 (조편성 완료 후 로스터 변경)")
            else:
                logger.debug(f"[모달] 조별 공지 업데이트 완료 - 조: {group_letter}조")
            
        except Exception as e:
            logger.error(f"[모달] 기존 조별 공지 메시지 수정 실패: {e}", exc_info=True)
    
    async def _clear_channel_messages(self, channel: discord.TextChannel) -> None:
        """채널의 모든 메시지를 삭제합니다."""
        try:
            # 메시지 삭제 (limit 없이 모든 메시지)
            async for message in channel.history(limit=None):
                try:
                    await message.delete()
                except discord.NotFound:
                    # 메시지가 이미 삭제된 경우
                    pass
                except discord.Forbidden:
                    # 삭제 권한이 없는 경우
                    logger.warning(f"[모달] 메시지 삭제 권한 없음 - 채널: {channel.name}")
                    break
                except Exception as e:
                    logger.error(f"[모달] 메시지 삭제 실패: {e}", exc_info=True)
                    continue
            
        except Exception as e:
            logger.error(f"[모달] 채널 메시지 삭제 실패 - 채널: {channel.name}: {e}", exc_info=True)
    
    async def _update_temp_message(self, temp_message: discord.Message, message: str, color: discord.Color) -> None:
        """임시 메시지를 업데이트합니다."""
        try:
            embed = discord.Embed(
                title="✅ 완료" if color == discord.Color.green() else "❌ 오류",
                description=message,
                color=color
            )
            await temp_message.edit(embed=embed)
        except Exception as e:
            logger.error(f"[모달] 임시 메시지 업데이트 실패: {e}", exc_info=True)

    async def _send_error_message(self, interaction: discord.Interaction, message: str) -> None:
        """에러 메시지를 전송합니다."""
        try:
            error_embed = discord.Embed(
                title="❌ 오류",
                description=message,
                color=discord.Color.red()
            )

            if not interaction.response.is_done():
                await interaction.response.send_message(embed=error_embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
        except Exception as e:
            logger.error(f"[모달] 에러 메시지 전송 실패: {e}", exc_info=True)


class WarningReasonModal(Modal):
    """
    경고/주의 사유 입력 모달 (통합)

    Select로 유형(주의/경고)과 사유(지각/대타/직접입력)를 선택하고,
    TextInput으로 상세 사유를 입력받습니다.
    """

    def __init__(self, target_user: discord.Member):
        super().__init__(title="제재 부여")
        self.target_user = target_user

        # 유형 선택 (주의/경고)
        self.type_select = Select(
            options=[
                SelectOption(label="주의", value="주의", description="주의 2회 누적 시 경고로 전환"),
                SelectOption(label="경고", value="경고", description="즉시 스크림 참여 제한"),
            ],
            placeholder="유형을 선택하세요",
            required=True,
        )
        self.add_item(Label(text="유형", component=self.type_select))

        # 사유 선택 (지각/대타/직접입력)
        self.reason_select = Select(
            options=[
                SelectOption(label="지각", value="지각"),
                SelectOption(label="대타", value="대타"),
                SelectOption(label="직접입력", value="직접입력", description="상세 사유에 직접 입력"),
            ],
            placeholder="사유를 선택하세요",
            required=True,
        )
        self.add_item(Label(text="사유", component=self.reason_select))

        # 상세 사유 입력
        self.detail_input = TextInput(
            placeholder="직접입력 선택 시 필수 / 그 외 추가 설명 (선택사항)",
            max_length=200,
            required=False,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(Label(
            text="상세 사유",
            description="'지각', '대타'처럼 한 단어로 간략하게 작성해주세요.",
            component=self.detail_input,
        ))

        # 안내 문구
        self.add_item(TextDisplay(content="📢 제재 부여 시 대상자에게 DM으로 알림이 발송됩니다."))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모달 제출 처리"""
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            # 입력 데이터 수집
            warning_type = self.type_select.values[0]
            reason_choice = self.reason_select.values[0]
            detail = self.detail_input.value.strip() if self.detail_input.value else ""

            # 사유 결합
            if reason_choice == "직접입력":
                if not detail:
                    error_embed = discord.Embed(
                        title="❌ 오류",
                        description="직접입력을 선택한 경우 상세 사유를 입력해주세요.",
                        color=discord.Color.red()
                    )
                    await interaction.followup.send(embed=error_embed, ephemeral=True)
                    return
                reason = detail
            else:
                reason = f"{reason_choice} - {detail}" if detail else reason_choice

            target_nickname = self.target_user.display_name or self.target_user.name
            target_id = str(self.target_user.id)
            admin_display_name = interaction.user.display_name or interaction.user.name

            # WarningManager를 통해 경고 추가
            warning_manager = BotManager.get_instance().get_warning_manager()

            success, message, auto_warning, converted_cautions = await warning_manager.add_warning(
                target=target_nickname,
                target_id=target_id,
                warning_type=warning_type,
                reason=reason,
                admin_display_name=admin_display_name
            )

            if success:

                from utils.helpers import get_current_kst_time
                current_time = get_current_kst_time().strftime('%Y-%m-%d %H:%M:%S')

                # 주의 2회 누적으로 경고 전환된 경우
                if auto_warning and converted_cautions:
                    embed = discord.Embed(
                        title="🚨 경고 자동 부여 완료",
                        description="주의 2회 누적으로 경고가 자동 부여되었습니다.",
                        color=0xED4245
                    )
                    embed.add_field(
                        name="📌 대상",
                        value=f"{self.target_user.mention} (`{target_nickname}`)",
                        inline=True
                    )
                    embed.add_field(
                        name="🚫 제한 해제일",
                        value=f"`{auto_warning.get('restricted_until', 'N/A')}`",
                        inline=True
                    )
                    embed.add_field(
                        name="📝 이번 주의 사유",
                        value=reason,
                        inline=False
                    )
                    caution_lines = []
                    for i, caution in enumerate(converted_cautions, 1):
                        caution_date = caution.get('날짜', 'N/A')
                        caution_reason = caution.get('사유', 'N/A')
                        caution_lines.append(f"`{i}회` {caution_date}: {caution_reason}")
                    embed.add_field(
                        name="📋 누적 주의 내역",
                        value="\n".join(caution_lines) if caution_lines else "내역 없음",
                        inline=False
                    )
                    embed.set_footer(text=f"처리일시: {current_time} • DM 발송 완료")

                # 일반 경고인 경우
                elif warning_type == '경고':
                    embed = discord.Embed(
                        title="🚨 경고 부여 완료",
                        color=0xED4245
                    )
                    embed.add_field(
                        name="📌 대상",
                        value=f"{self.target_user.mention} (`{target_nickname}`)",
                        inline=True
                    )
                    embed.add_field(
                        name="🚫 제한 해제일",
                        value=f"`{auto_warning.get('restricted_until', 'N/A') if auto_warning else 'N/A'}`",
                        inline=True
                    )
                    embed.add_field(
                        name="📝 사유",
                        value=reason,
                        inline=False
                    )
                    embed.set_footer(text=f"처리일시: {current_time} • DM 발송 완료")

                # 주의인 경우
                else:
                    embed = discord.Embed(
                        title="⚡ 주의 부여 완료",
                        color=0xFEE75C
                    )
                    embed.add_field(
                        name="📌 대상",
                        value=f"{self.target_user.mention} (`{target_nickname}`)",
                        inline=True
                    )
                    embed.add_field(
                        name="📝 사유",
                        value=reason,
                        inline=False
                    )
                    embed.add_field(
                        name="💡 참고",
                        value="주의 2회 누적 시 경고로 자동 전환됩니다.",
                        inline=False
                    )
                    embed.set_footer(text=f"처리일시: {current_time} • DM 발송 완료")

                # 대상자에게 DM 발송
                await self._send_warning_dm(
                    target_user=self.target_user,
                    warning_type=warning_type,
                    reason=reason,
                    admin_name=admin_display_name,
                    auto_warning=auto_warning,
                    converted_cautions=converted_cautions
                )
            else:
                logger.error(f"[모달] {warning_type} 추가 실패 - 대상: {target_nickname}, 메시지: {message}")
                embed = discord.Embed(
                    title="❌ 처리 실패",
                    description=message,
                    color=0xED4245
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"[모달] 제재 모달 처리 실패 - 대상: {self.target_user.display_name if self.target_user else 'Unknown'}, 오류: {e}", exc_info=True)
            error_embed = discord.Embed(
                title="❌ 오류",
                description="제재 처리 중 오류가 발생했습니다.",
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=error_embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=error_embed, ephemeral=True)

    async def _send_warning_dm(
        self,
        target_user: discord.Member,
        warning_type: str,
        reason: str,
        admin_name: str,
        auto_warning: dict = None,
        converted_cautions: list = None
    ) -> None:
        """경고/주의 부여 시 대상자에게 DM을 발송합니다."""
        try:
            from utils.helpers import get_current_kst_time
            current_time = get_current_kst_time().strftime('%Y-%m-%d %H:%M')

            # 주의 2회 누적으로 경고 전환된 경우
            if auto_warning and converted_cautions:
                restricted_until = auto_warning.get('restricted_until', 'N/A')

                embed = discord.Embed(
                    title="🚨 경고 알림",
                    description="주의 2회 누적으로 인해 **경고**가 부여되었습니다.",
                    color=0xED4245
                )

                caution_lines = []
                for i, caution in enumerate(converted_cautions, 1):
                    caution_date = caution.get('날짜', 'N/A')
                    caution_reason = caution.get('사유', 'N/A')
                    caution_lines.append(f"`{i}회` {caution_date}\n└ {caution_reason}")

                embed.add_field(
                    name="📋 누적 주의 내역",
                    value="\n\n".join(caution_lines) if caution_lines else "내역 없음",
                    inline=False
                )

                embed.add_field(
                    name="🚫 참여 제한",
                    value=f"**{restricted_until}**까지 스크림 참여가 제한됩니다.",
                    inline=False
                )

                embed.set_footer(text=f"처리일시: {current_time} • 문의: #ticket")

            # 일반 경고인 경우 (직접 부여)
            elif warning_type == '경고':
                restricted_until = auto_warning.get('restricted_until', 'N/A') if auto_warning else 'N/A'

                embed = discord.Embed(
                    title="🚨 경고 알림",
                    description="**경고**가 부여되었습니다.",
                    color=0xED4245
                )

                embed.add_field(
                    name="📝 사유",
                    value=reason,
                    inline=False
                )

                embed.add_field(
                    name="🚫 참여 제한",
                    value=f"**{restricted_until}**까지 스크림 참여가 제한됩니다.",
                    inline=False
                )

                embed.set_footer(text=f"처리일시: {current_time} • 문의: #ticket")

            # 주의인 경우
            else:
                embed = discord.Embed(
                    title="⚡ 주의 알림",
                    description="**주의**가 부여되었습니다.",
                    color=0xFEE75C
                )

                embed.add_field(
                    name="📝 사유",
                    value=reason,
                    inline=False
                )

                embed.add_field(
                    name="💡 안내",
                    value="주의 2회 누적 시 경고로 전환되며,\n스크림 참여가 제한됩니다.",
                    inline=False
                )

                embed.set_footer(text=f"처리일시: {current_time} • 문의: #ticket")

            # DM 발송
            await target_user.send(embed=embed)

        except discord.Forbidden:
            logger.warning(f"[모달] DM 발송 실패 (DM 차단) - 대상: {target_user.display_name}")
        except Exception as e:
            logger.error(f"[모달] DM 발송 실패 - 대상: {target_user.display_name}, 오류: {e}", exc_info=True)


class CSVImportModal(Modal):
    """
    CSV 팀 입력 모달

    관리자가 CSV 형식으로 팀을 일괄 등록할 수 있는 폼을 제공합니다.
    CSV 내보내기와 동일한 양식(team_name, players, staff)을 사용합니다.
    """

    def __init__(self, view):
        super().__init__(title="CSV 팀 입력")
        self.view = view

        # CSV 입력
        self.csv_input = TextInput(
            label="CSV 내용",
            placeholder="team_name,players,staff\nTeam1,Player1 Player2,Staff1\nTeam2,Player3 Player4,",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True
        )
        self.add_item(self.csv_input)
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모달 제출 처리"""
        try:
            # 즉시 응답하여 모달을 닫음
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            
            # 임시 메시지 전송
            temp_embed = discord.Embed(
                title="⏳ 처리 중...",
                description="CSV를 파싱하고 팀을 등록하고 있습니다.\n잠시만 기다려주세요.",
                color=discord.Color.blue()
            )
            temp_message = await interaction.followup.send(embed=temp_embed, ephemeral=True)

            csv_content = self.csv_input.value.strip()

            # 입력값이 비어있는지 확인
            if not csv_content:
                await self._update_temp_message(temp_message, "❌ 입력값이 비어있습니다.", discord.Color.red())
                return
            
            # CSV 파싱 및 팀 추가
            await self._process_csv_import(interaction, csv_content, temp_message)
            
        except Exception as e:
            logger.error(f"[모달] CSV 팀 입력 모달 제출 처리 실패: {e}", exc_info=True)
            await self._send_error_message(interaction, "CSV 팀 입력 중 오류가 발생했습니다.")
    
    async def _process_csv_import(self, interaction: discord.Interaction, csv_content: str, temp_message: discord.Message) -> None:
        """CSV 파싱 및 팀 추가 처리"""
        try:
            import io
            import csv as csv_module
            from models.team_data import TeamData
            
            # CSV 파싱
            reader = csv_module.DictReader(io.StringIO(csv_content))
            
            # 필수 컬럼 확인
            required_cols = ["team_name", "players", "staff"]
            if not reader.fieldnames or any(col not in reader.fieldnames for col in required_cols):
                await self._update_temp_message(
                    temp_message,
                    f"❌ CSV 형식 오류\n\n필요한 컬럼이 없습니다: {', '.join(required_cols)}",
                    discord.Color.red()
                )
                return
            
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            teams_payload = {}
            errors = []
            
            # CSV 행 파싱
            for row_num, row in enumerate(reader, start=2):  # 헤더 제외하고 2부터 시작
                team_name = row.get("team_name", "").strip()
                players_str = row.get("players", "").strip()
                staff_str = row.get("staff", "").strip()
                
                # DictReader가 이미 따옴표를 처리했으므로, 쉼표로 구분된 값들을 파싱
                # 플레이어 파싱 (쉼표로 구분, DictReader가 따옴표 처리 완료)
                players = []
                if players_str:
                    # 쉼표로 구분 (DictReader가 이미 따옴표 제거 및 필드 분리 완료)
                    players = [p.strip() for p in players_str.split(",") if p.strip()]
                
                # 스태프 파싱 (쉼표로 구분, DictReader가 따옴표 처리 완료)
                staff = []
                if staff_str:
                    # 쉼표로 구분 (DictReader가 이미 따옴표 제거 및 필드 분리 완료)
                    staff = [s.strip() for s in staff_str.split(",") if s.strip()]
                
                if not team_name:
                    errors.append(f"행 {row_num}: 팀명이 없습니다")
                    continue

                if not players:
                    errors.append(f"행 {row_num}: 플레이어가 없습니다")
                    continue
                
                teams_payload[team_name] = {
                    "players": players,
                    "staff": staff,
                }
            
            if not teams_payload:
                error_msg = "❌ 유효한 팀을 찾을 수 없습니다."
                if errors:
                    error_msg += f"\n\n오류 내역:\n{chr(10).join(errors[:5])}"  # 최대 5개 오류만 표시
                await self._update_temp_message(temp_message, error_msg, discord.Color.red())
                return
            
            # 팀 추가 (관리자 오버라이드 사용)
            success_count = 0
            fail_count = 0
            fail_messages = []
            
            for team_name, data in teams_payload.items():
                try:
                    team_obj = TeamData(name=team_name, players=data["players"], staff=data["staff"])
                    success, failure_reason = await team_data_manager.add_team(
                        team_name,
                        team_obj,
                        interaction.user,
                        allow_admin_override=True
                    )
                    
                    if success:
                        success_count += 1
                    else:
                        fail_count += 1
                        fail_messages.append(f"{team_name}: {failure_reason}")
                except Exception as e:
                    fail_count += 1
                    fail_messages.append(f"{team_name}: {str(e)}")
            
            # 파싱 오류도 fail로 합산
            fail_count += len(errors)
            fail_messages = errors + fail_messages

            # 결과 메시지 생성
            if success_count > 0 and fail_count == 0:
                result_msg = f"✅ **{success_count}개 팀 등록 완료**"
                result_color = discord.Color.green()
            elif success_count > 0 and fail_count > 0:
                result_msg = f"⚠️ **부분 성공**\n\n성공: {success_count}개 팀\n실패: {fail_count}개 팀"
                if fail_messages:
                    result_msg += f"\n\n**실패 상세:**\n{chr(10).join(fail_messages[:10])}"
                result_color = discord.Color.orange()
            else:
                result_msg = f"❌ **모든 팀 등록 실패** ({fail_count}개 팀)"
                if fail_messages:
                    result_msg += f"\n\n**실패 상세:**\n{chr(10).join(fail_messages[:10])}"
                result_color = discord.Color.red()

            await self._update_temp_message(temp_message, result_msg, result_color)
            
            # MMR 갱신 및 메시지 업데이트 (백그라운드에서 수행)
            if success_count > 0:
                import asyncio
                task = asyncio.create_task(self._update_mmr_background(team_data_manager, interaction.channel))
                team_data_manager._pending_tasks.add(task)
                task.add_done_callback(team_data_manager._pending_tasks.discard)
            
        except Exception as e:
            logger.error(f"[모달] CSV 팀 입력 처리 실패: {e}", exc_info=True)
            await self._update_temp_message(
                temp_message,
                f"❌ CSV 처리 중 오류가 발생했습니다.\n\n{str(e)}",
                discord.Color.red()
            )
    
    async def _update_temp_message(self, temp_message: discord.Message, content: str, color: discord.Color) -> None:
        """임시 메시지를 업데이트합니다."""
        try:
            embed = discord.Embed(
                description=content,
                color=color
            )
            await temp_message.edit(embed=embed)
        except Exception as e:
            logger.error(f"[모달] 임시 메시지 업데이트 실패: {e}", exc_info=True)
    
    async def _update_mmr_background(self, team_data_manager, channel) -> None:
        """백그라운드에서 MMR 갱신 및 메시지 업데이트"""
        try:
            # MMR 갱신
            try:
                await team_data_manager._update_all_team_mmr()
            except Exception as e:
                logger.error(f"[모달] 팀 MMR 갱신 실패: {e}", exc_info=True)
            
            # MMR 메시지 업데이트
            try:
                await team_data_manager.update_mmr_message(channel)
            except Exception as e:
                logger.error(f"[모달] MMR 메시지 업데이트 실패: {e}", exc_info=True)
                # 실패 시 재시도
                try:
                    team_data_manager.mmr_message = None
                    await team_data_manager.update_mmr_message(channel)
                except Exception as e2:
                    logger.error(f"[모달] MMR 메시지 재생성 실패: {e2}", exc_info=True)
                    
        except Exception as e:
            logger.error(f"[모달] 백그라운드 MMR 갱신 실패: {e}", exc_info=True)
    
    async def _send_error_message(self, interaction: discord.Interaction, message: str) -> None:
        """에러 메시지를 전송합니다."""
        try:
            error_embed = discord.Embed(
                title="❌ 오류",
                description=message,
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=error_embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
        except Exception as e:
            logger.error(f"[모달] 에러 메시지 전송 실패: {e}", exc_info=True)
