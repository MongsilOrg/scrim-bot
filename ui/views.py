"""
Discord View 컴포넌트들
"""
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple
import io
import csv

import discord
from discord import ButtonStyle, Color, Embed, SelectOption
from discord.ui import Button, Select, View

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from models.team_data_manager import TeamDataManager
from models.team_processor import TeamProcessor
from services.bser_api import BSERAPIClient
from utils.helpers import get_current_kst_time, is_admin
from utils.validators import validate_team_name

# 버튼 cooldown 관리 (사용자별 마지막 클릭 시간)
_button_cooldowns: Dict[int, float] = {}
BUTTON_COOLDOWN_SECONDS = 3


async def send_error_message(interaction: discord.Interaction, message: str) -> None:
    """에러 메시지를 전송하는 공통 유틸리티 함수"""
    try:
        error_embed = Embed(
            title="스크림 안내",
            description=message,
            color=Color.red()
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
        else:
            await interaction.followup.send(embed=error_embed, ephemeral=True)
    except Exception as e:
        logger.error(f"[뷰] 에러 메시지 전송 실패: {e}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"오류: {message}", ephemeral=True)
            else:
                await interaction.followup.send(f"오류: {message}", ephemeral=True)
        except Exception:
            pass


_COOLDOWN_CLEANUP_THRESHOLD = 100  # 이 크기 초과 시 만료 항목 정리


async def _check_cooldown(interaction: discord.Interaction, cooldown_seconds: float = BUTTON_COOLDOWN_SECONDS) -> bool:
    """버튼 cooldown을 확인합니다. True면 cooldown 중이므로 무시해야 합니다."""
    user_id = interaction.user.id
    now = time.monotonic()
    last_click = _button_cooldowns.get(user_id, 0)
    if now - last_click < cooldown_seconds:
        remaining = cooldown_seconds - (now - last_click)
        await interaction.response.send_message(
            f"⏳ 잠시 후 다시 시도해주세요. ({remaining:.0f}초)",
            ephemeral=True
        )
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


class TeamInputView(View):
    """
    팀 입력 및 관리 뷰
    
    스크림 참가 신청, 취소, 관리자 기능에 대한 버튼들을 제공합니다.
    사용자의 팀 등록 상태에 따라 적절한 모달을 표시합니다.
    """
    
    def __init__(self, embed: discord.Embed):
        super().__init__(timeout=None)
        self.embed = embed
        
        # 팀 신청/수정 버튼
        self.add_team_button = Button(
            label="신청/수정",
            style=ButtonStyle.primary,
            emoji="✏️"
        )
        self.add_team_button.callback = self.add_team_callback
        self.add_item(self.add_team_button)

        # 팀 취소 버튼
        self.cancel_team_button = Button(
            label="취소",
            style=ButtonStyle.secondary,
            emoji="🚫"
        )
        self.cancel_team_button.callback = self.cancel_team_callback
        self.add_item(self.cancel_team_button)

        # 관리자 기능 버튼
        self.admin_log_button = Button(
            label="관리",
            style=ButtonStyle.danger,
            emoji="⚙️"
        )
        self.admin_log_button.callback = self.admin_log_callback
        self.add_item(self.admin_log_button)
    
    async def add_team_callback(self, interaction: discord.Interaction) -> None:
        """팀 추가 버튼 콜백 (기존 팀이 있으면 수정 모달 표시)"""
        if await _check_cooldown(interaction):
            return
        try:
            # 전역 team_data_manager 인스턴스 사용 (순환 참조 방지)
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            
            # 조편성 시작 이후인지 확인
            if team_data_manager.is_team_assignment_started:
                await self._send_error_message(interaction, "조 편성이 이미 시작되어 팀 등록이 불가능합니다.")
                return
            
            # 기존 등록된 팀이 있는지 확인 (취소 버튼과 동일한 로직)
            user_id = str(interaction.user.id)
            user_team = None
            
            for team_name, team_data in team_data_manager.get_all_teams().items():
                if hasattr(team_data, 'user_id') and team_data.user_id == user_id:
                    user_team = team_name
                    break
            
            # ID로 찾지 못한 경우 닉네임으로 검색
            if not user_team:
                user_nickname = interaction.user.display_name
                user_team = self._find_team_by_nickname(team_data_manager, user_nickname)
            
            if user_team:
                # 기존 팀이 있는 경우 - 팀 수정 모달 표시
                await self._show_team_edit_modal(interaction, user_team, team_data_manager)
            else:
                # 기존 팀이 없는 경우 - 새 팀 등록 모달 표시
                from .modals import TeamModal
                if not interaction.response.is_done():
                    await interaction.response.send_modal(TeamModal(self, interaction.user))
                else:
                    await interaction.followup.send("모달을 표시할 수 없습니다. 다시 시도해주세요.", ephemeral=True)

        except Exception as e:
            logger.error(f"[뷰] 팀 추가 콜백 처리 실패: {e}", exc_info=True)
            await self._send_error_message(interaction, "팀 추가 중 오류가 발생했습니다.")
    
    async def cancel_team_callback(self, interaction: discord.Interaction) -> None:
        """팀 취소 버튼 콜백 (신청자 ID 또는 닉네임 기반)"""
        if await _check_cooldown(interaction, cooldown_seconds=1):
            return
        try:
            # 전역 team_data_manager 인스턴스 사용 (순환 참조 방지)
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            
            # 신청자가 등록한 팀이 있는지 확인 (ID 기반)
            user_id = str(interaction.user.id)
            user_team = None
            
            for team_name, team_data in team_data_manager.get_all_teams().items():
                if hasattr(team_data, 'user_id') and team_data.user_id == user_id:
                    user_team = team_name
                    break
            
            # ID로 찾지 못한 경우 닉네임으로 검색
            if not user_team:
                user_nickname = interaction.user.display_name
                user_team = self._find_team_by_nickname(team_data_manager, user_nickname)
            
            if not user_team:
                await self._send_error_message(interaction, "등록한 팀이 없습니다.")
                return

            # 팀 정보 가져오기
            team_data = team_data_manager.get_team_data(user_team)
            team_mmr = team_data_manager.get_team_mmr(user_team) or 0.0

            players = []
            staff = []
            if team_data:
                if isinstance(team_data, dict):
                    players = team_data.get('players', [])
                    staff = team_data.get('staff', [])
                else:
                    players = getattr(team_data, 'players', [])
                    staff = getattr(team_data, 'staff', [])

            # 확인 Embed 생성
            confirm_embed = Embed(
                title="🚫 팀 등록 취소 확인",
                description=f"**{user_team}** 팀의 등록을 취소하시겠습니까?",
                color=Color.orange()
            )
            members_str = ', '.join(players) if players else '(없음)'
            staff_str = ', '.join(staff) if staff else '(없음)'
            confirm_embed.add_field(name="선수", value=members_str, inline=False)
            if staff:
                confirm_embed.add_field(name="스태프", value=staff_str, inline=False)
            confirm_embed.add_field(name="MMR", value=f"{team_mmr:.2f}", inline=True)

            # 확인 View 표시
            confirm_view = CancelConfirmView(self, user_team)
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=confirm_embed, view=confirm_view, ephemeral=True)
                confirm_view.message = await interaction.original_response()
            else:
                msg = await interaction.followup.send(embed=confirm_embed, view=confirm_view, ephemeral=True, wait=True)
                confirm_view.message = msg

        except Exception as e:
            logger.error(f"[뷰] 팀 취소 콜백 처리 실패: {e}", exc_info=True)
            await self._send_error_message(interaction, "팀 취소 중 오류가 발생했습니다.")
    
    def _find_team_by_nickname(self, team_data_manager, nickname: str) -> str:
        """닉네임으로 팀을 찾는 헬퍼 메서드 (대소문자 구별 없이)"""
        from utils.validators import normalize_nickname_for_comparison
        
        all_teams = team_data_manager.get_all_teams()
        normalized_nickname = normalize_nickname_for_comparison(nickname)
        
        for team_name, team_data in all_teams.items():
            if hasattr(team_data, 'all_members'):
                # TeamData 객체인 경우
                all_members = team_data.all_members
                
                # 대소문자 구별 없이 비교
                for member in all_members:
                    if normalize_nickname_for_comparison(member) == normalized_nickname:
                        return team_name
            elif isinstance(team_data, dict):
                # 딕셔너리 형식인 경우 (레거시 지원)
                players = team_data.get('players', [])
                staff = team_data.get('staff', [])
                all_members = players + staff
                
                # 대소문자 구별 없이 비교
                for member in all_members:
                    if normalize_nickname_for_comparison(member) == normalized_nickname:
                        return team_name
            else:
                # 기존 형식 (리스트)
                for member in team_data:
                    if normalize_nickname_for_comparison(member) == normalized_nickname:
                        return team_name
        
        return None
    
    async def _show_team_edit_modal(self, interaction: discord.Interaction, team_name: str, team_data_manager) -> None:
        """기존 팀 수정 모달을 표시합니다."""
        try:
            # 팀 데이터 가져오기
            team_data = team_data_manager.get_team_data(team_name)
            if not team_data:
                await self._send_error_message(interaction, "팀 정보를 찾을 수 없습니다.")
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

        except Exception as e:
            logger.error(f"[뷰] 팀 수정 모달 표시 실패: {e}", exc_info=True)
            await self._send_error_message(interaction, "팀 수정 모달 표시 중 오류가 발생했습니다.")
    
    async def admin_log_callback(self, interaction: discord.Interaction) -> None:
        """관리자 로그 버튼 콜백"""
        try:
            # 관리자 권한 확인
            if not is_admin(interaction.user):
                error_embed = Embed(
                    title="스크림 안내",
                    description="관리자 권한이 없습니다.",
                    color=Color.red()
                )
                # 상호작용이 이미 응답되었는지 확인
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=error_embed, ephemeral=True)
                else:
                    # 이미 응답된 경우 followup으로 전송
                    await interaction.followup.send(embed=error_embed, ephemeral=True)
                return

            # 로그 임베드 생성 및 길이 확인
            log_embed = self._create_log_embed()
            
            # 임베드 필드 값이 1024자를 초과하는지 확인
            max_field_length = 1024
            use_file = False
            for field in log_embed.fields:
                if len(field.value) > max_field_length:
                    use_file = True
                    break
            
            # 로그가 너무 길면 텍스트 파일로 전송
            if use_file:
                await self._send_log_as_file(interaction)
            else:
                # 로그가 짧으면 임베드로 전송
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=log_embed, ephemeral=True, view=AdminView(self))
                else:
                    # 이미 응답된 경우 followup으로 전송
                    await interaction.followup.send(embed=log_embed, ephemeral=True, view=AdminView(self))
            
        except Exception as e:
            logger.error(f"[뷰] 관리자 로그 콜백 처리 실패: {e}", exc_info=True)
            await self._send_error_message(interaction, "로그 조회 중 오류가 발생했습니다.")
    
    async def _process_team_registration(self, interaction: discord.Interaction, team_name: str, team_data: dict, temp_message: discord.Message = None) -> None:
        """팀 등록 처리"""
        try:
            # 현재 시간 확인
            current_time = get_current_kst_time()
            
            # 전역 team_data_manager 인스턴스 사용 (순환 참조 방지)
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            
            # 조편성 시작 이후인지 확인
            if team_data_manager.is_team_assignment_started:
                if temp_message:
                    await self._update_temp_message(temp_message, "조 편성이 이미 시작되어 팀 등록이 불가능합니다.", discord.Color.red())
                else:
                    await self._send_error_message(interaction, "조 편성이 이미 시작되어 팀 등록이 불가능합니다.")
                return
            
            # 팀 등록 가능 여부 확인
            is_allowed, error_message = await team_data_manager.check_team_registration_allowed(current_time)
            
            if not is_allowed:
                if temp_message:
                    await self._update_temp_message(temp_message, error_message, discord.Color.red())
                else:
                    await self._send_error_message(interaction, error_message)
                return
            
            # 팀원 목록 생성
            if isinstance(team_data, dict):
                team_members = team_data.get('players', []) + team_data.get('staff', [])
            else:
                team_members = team_data
            
            # 봇으로 등록된 팀들과 중복 검사
            is_bot_valid, bot_error = team_data_manager.check_duplicate_with_bot_teams(team_name, team_members)
            if not is_bot_valid:
                if temp_message:
                    await self._update_temp_message(temp_message, bot_error, discord.Color.red())
                else:
                    await self._send_error_message(interaction, bot_error)
                return
            
            # 팀원 중 테스트 계정이 있는지 확인
            team_processor = BotManager.get_instance().get_team_processor()
            
            has_test_account = any(team_processor._is_test_account(member) for member in team_members)

            # 닉네임 검증 (테스트 계정이 없는 경우에만)
            if not has_test_account:
                # 디스코드 서버 멤버 검증 (로컬, API 호출 전에 먼저 수행)
                from utils.validators import validate_members_in_guild
                client = BotManager.get_instance().get_client()
                guild = client.get_guild(settings.GUILD_ID) if client else None

                if guild:
                    is_guild_valid, not_found_members = validate_members_in_guild(guild, team_members)
                    if not is_guild_valid:
                        not_found_str = ', '.join(not_found_members)
                        error_msg = (
                            f"❌ 다음 닉네임이 디스코드 서버에서 확인되지 않습니다.\n\n"
                            f"**{not_found_str}**\n\n"
                            f"💡 디스코드 서버 닉네임과 동일하게 입력해주세요."
                        )
                        if temp_message:
                            await self._update_temp_message(temp_message, error_msg, discord.Color.red())
                        else:
                            await self._send_error_message(interaction, error_msg)
                        return

                # API 닉네임 검증 (게임 내 닉네임 확인)
                api_invalid_members = []
                try:
                    async with BSERAPIClient() as client_instance:
                        for member in team_members:
                            if not await client_instance.get_user_uid(member):
                                api_invalid_members.append(member)
                except Exception as e:
                    logger.error(f"[뷰] API 닉네임 검증 실패: {e}", exc_info=True)
                    if temp_message:
                        await self._update_temp_message(temp_message, "닉네임 확인 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요.", discord.Color.red())
                    else:
                        await self._send_error_message(interaction, "닉네임 확인 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요.")
                    return

                if api_invalid_members:
                    error_msg = f"❌ 다음 닉네임들을 찾을 수 없습니다.\n**{', '.join(api_invalid_members) if api_invalid_members and isinstance(api_invalid_members, (list, tuple)) else str(api_invalid_members)}**\n\n💡 게임 내 닉네임을 정확히 입력했는지 확인해주세요."
                    if temp_message:
                        await self._update_temp_message(temp_message, error_msg, discord.Color.red())
                    else:
                        await self._send_error_message(interaction, error_msg)
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
                    await self._update_temp_message(temp_message, error_message, discord.Color.red())
                else:
                    await self._send_error_message(interaction, error_message)
                return
            
            team_data_manager.log_action("신청", interaction.user, team_name)
            
            # 팀원과 스태프 목록 추출
            if isinstance(team_data, dict):
                players = team_data.get('players', [])
                staff = team_data.get('staff', [])
            else:
                players = getattr(team_data, 'players', [])
                staff = getattr(team_data, 'staff', [])
            
            # 성공 메시지 처리
            players_str = ', '.join(players) if players else '(없음)'
            staff_str = ', '.join(staff) if staff else '(없음)'
            logger.info(f"[팀등록] 팀 등록 완료 - 팀명: {team_name}, MMR: {team_mmr:.2f}, 선수: [{players_str}], 스태프: [{staff_str}]")

            success_msg = (
                f"**{team_name}** 팀이 성공적으로 등록되었습니다!\n\n"
                f"🎮 선수: {players_str}\n"
                f"🛠️ 스태프: {staff_str}\n"
                f"📊 팀 평균 MMR: **{team_mmr:.2f}**"
            )

            if temp_message:
                # 임시 메시지를 성공 메시지로 업데이트
                await self._update_temp_message(temp_message, success_msg, discord.Color.green())
            else:
                # 기존 방식으로 성공 메시지 전송
                success_embed = Embed(
                    title="✅ 완료",
                    description=success_msg,
                    color=Color.green()
                )
                
                # 상호작용이 이미 응답되었는지 확인
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=success_embed, ephemeral=True)
                else:
                    # 이미 응답된 경우 followup으로 전송
                    await interaction.followup.send(embed=success_embed, ephemeral=True)
            
            # 백그라운드에서 MMR 갱신 및 메시지 업데이트
            import asyncio
            task = asyncio.create_task(self._update_mmr_background(team_data_manager, interaction.channel))
            team_data_manager._pending_tasks.add(task)
            task.add_done_callback(team_data_manager._pending_tasks.discard)

        except Exception as e:
            logger.error(f"[뷰] 팀 등록 실패: {e}", exc_info=True)
            await self._send_error_message(
                interaction,
                "❌ 팀 등록 중 오류가 발생했습니다.\n\n💡 다시 시도해도 문제가 지속되면 관리자에게 문의해주세요."
            )

    async def _update_temp_message(self, temp_message: discord.Message, message: str, color: discord.Color) -> None:
        """임시 메시지를 업데이트합니다."""
        try:
            embed = discord.Embed(
                title="✅ 완료" if color == discord.Color.green() else "스크림 안내",
                description=message,
                color=color
            )
            await temp_message.edit(embed=embed)
        except Exception as e:
            logger.error(f"[뷰] 임시 메시지 업데이트 실패: {e}", exc_info=True)
    
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
            
            # MMR 메시지 업데이트
            try:
                await team_data_manager.update_mmr_message(channel)
            except Exception as e:
                logger.error(f"[뷰] MMR 메시지 업데이트 실패: {e}", exc_info=True)
                # 실패 시 재시도
                try:
                    team_data_manager.mmr_message = None
                    await team_data_manager.update_mmr_message(channel)
                except Exception as e2:
                    logger.error(f"[뷰] MMR 메시지 재생성 실패: {e2}", exc_info=True)
                    
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
                await self._send_error_message(interaction, "조 편성이 이미 시작되어 팀 취소가 불가능합니다.")
                return
            
            # 팀 취소 가능 여부 확인
            is_allowed, error_message = await team_data_manager.check_team_cancellation_allowed(current_time)
            
            if not is_allowed:
                await self._send_error_message(interaction, error_message)
                return
            
            # 팀 존재 확인
            if team_name not in team_data_manager.get_all_teams():
                await self._send_error_message(interaction, "등록되지 않은 팀명입니다.")
                return

            # 일반 취소 처리
            await self._execute_team_cancellation(interaction, team_name, team_data_manager)

        except Exception as e:
            logger.error(f"[뷰] 팀 취소 실패: {e}", exc_info=True)
            await self._send_error_message(interaction, "팀 취소 중 오류가 발생했습니다.")
    
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
                    "❌ 팀 취소가 실패했습니다.\n\n"
                    "💡 취소 시간 제한을 확인해주세요."
                )
                error_embed = Embed(
                    title="❌ 오류",
                    description=error_message,
                    color=Color.red()
                )
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=error_embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=error_embed, ephemeral=True)
                return

            team_data_manager.log_action("취소", interaction.user, team_name)

            # 성공 메시지 전송
            success_embed = Embed(
                title="✅ 완료",
                description=f"**{team_name}** 팀이 성공적으로 취소되었습니다.",
                color=Color.green()
            )
            
            # 상호작용이 이미 응답되었는지 확인
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=success_embed, ephemeral=True)
            else:
                # 이미 응답된 경우 followup으로 전송
                await interaction.followup.send(embed=success_embed, ephemeral=True)
            
            # 백그라운드에서 MMR 갱신 및 메시지 업데이트
            import asyncio
            asyncio.create_task(self._update_mmr_background(team_data_manager, interaction.channel))
            
        except Exception as e:
            logger.error(f"[뷰] 팀 취소 실행 실패: {e}", exc_info=True)
            error_embed = Embed(
                title="❌ 오류",
                description="팀 취소 중 오류가 발생했습니다.",
                color=Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=error_embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
    
    def _create_log_embed(self) -> discord.Embed:
        """로그 임베드를 생성합니다."""
        team_data_manager = BotManager.get_instance().get_team_data_manager()
        logs = team_data_manager.get_logs()

        embed = Embed(
            title="📋 스크림 로그",
            color=Color.blue()
        )

        # 신청 로그 (모든 로그 표시)
        if logs["신청"]:
            application_logs = []
            for log in logs["신청"]:  # 모든 로그 표시
                user_mention = f"<@{log['user_id']}>"
                application_logs.append(f"**{log['team']}** {user_mention} `{log['time']}`")
            log_text = "\n".join(application_logs) if application_logs and isinstance(application_logs, (list, tuple)) else "없음"
            # 1024자 제한 적용
            if len(log_text) > 1024:
                log_text = log_text[:1021] + "..."
            embed.add_field(
                name="📝 신청",
                value=log_text,
                inline=False
            )

        # 취소 로그 (모든 로그 표시)
        if logs["취소"]:
            cancellation_logs = []
            for log in logs["취소"]:  # 모든 로그 표시
                user_mention = f"<@{log['user_id']}>"
                cancellation_logs.append(f"**{log['team']}** {user_mention} `{log['time']}`")
            log_text = "\n".join(cancellation_logs) if cancellation_logs and isinstance(cancellation_logs, (list, tuple)) else "없음"
            # 1024자 제한 적용
            if len(log_text) > 1024:
                log_text = log_text[:1021] + "..."
            embed.add_field(
                name="❌ 취소",
                value=log_text,
                inline=False
            )

        # 수정 로그 (모든 로그 표시)
        if logs["수정"]:
            modification_logs = []
            for log in logs["수정"]:  # 모든 로그 표시
                user_mention = f"<@{log['user_id']}>"
                modification_logs.append(f"**{log['team']}** {user_mention} `{log['time']}`")
            log_text = "\n".join(modification_logs) if modification_logs and isinstance(modification_logs, (list, tuple)) else "없음"
            # 1024자 제한 적용
            if len(log_text) > 1024:
                log_text = log_text[:1021] + "..."
            embed.add_field(
                name="✏️ 수정",
                value=log_text,
                inline=False
            )

        return embed
    
    async def _send_log_as_file(self, interaction: discord.Interaction) -> None:
        """로그를 텍스트 파일로 전송합니다."""
        import tempfile
        import os
        from datetime import datetime
        
        try:
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            logs = team_data_manager.get_logs()
            
            # 임시 파일 생성
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', delete=False) as f:
                f.write("=" * 60 + "\n")
                f.write("스크림 로그\n")
                f.write("=" * 60 + "\n\n")

                # 신청 로그
                f.write("📝 신청\n")
                f.write("-" * 60 + "\n")
                if logs["신청"]:
                    for log in logs["신청"]:
                        f.write(f"[{log['time']}] {log['user']} ({log['user_id']}) - {log['team']}\n")
                else:
                    f.write("없음\n")
                f.write("\n")

                # 취소 로그
                f.write("❌ 취소\n")
                f.write("-" * 60 + "\n")
                if logs["취소"]:
                    for log in logs["취소"]:
                        f.write(f"[{log['time']}] {log['user']} ({log['user_id']}) - {log['team']}\n")
                else:
                    f.write("없음\n")
                f.write("\n")

                # 수정 로그
                f.write("✏️ 수정\n")
                f.write("-" * 60 + "\n")
                if logs["수정"]:
                    for log in logs["수정"]:
                        f.write(f"[{log['time']}] {log['user']} ({log['user_id']}) - {log['team']}\n")
                else:
                    f.write("없음\n")

                temp_file_path = f.name
            
            # 파일 크기 확인 (Discord 최대 25MB)
            file_size = os.path.getsize(temp_file_path)
            if file_size > 25 * 1024 * 1024:  # 25MB
                os.unlink(temp_file_path)
                error_embed = Embed(
                    title="❌ 오류",
                    description="로그 파일이 너무 큽니다. (25MB 초과)",
                    color=Color.red()
                )
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=error_embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=error_embed, ephemeral=True)
                return

            # 파일명 생성
            current_time = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"scrim_log_{current_time}.txt"

            # 파일 전송
            info_embed = Embed(
                title="📋 스크림 로그",
                description="로그가 너무 길어 텍스트 파일로 전송합니다.",
                color=Color.blue()
            )
            
            with open(temp_file_path, 'rb') as f:
                file = discord.File(f, filename=filename)
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=info_embed, file=file, ephemeral=True, view=AdminView(self))
                else:
                    await interaction.followup.send(embed=info_embed, file=file, ephemeral=True, view=AdminView(self))
            
            # 임시 파일 삭제
            try:
                os.unlink(temp_file_path)
            except Exception:
                pass  # 임시 파일 삭제 실패 (치명적이지 않음)
                
        except Exception as e:
            logger.error(f"[뷰] 로그 파일 전송 실패: {e}", exc_info=True)
            error_embed = Embed(
                title="❌ 오류",
                description="로그 파일 생성 중 오류가 발생했습니다.",
                color=Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=error_embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
    
    async def _send_error_message(self, interaction: discord.Interaction, message: str) -> None:
        await send_error_message(interaction, message)


class GroupRosterView(View):
    """
    조별 로스터 관리 뷰
    
    조별 공지에서 팀 로스터 변경 기능을 제공합니다.
    관리자만 접근 가능하며, 드롭다운을 통해 팀을 선택하고 수정할 수 있습니다.
    """
    
    def __init__(self, group_letter: str, group_teams: List[Tuple[str, 'TeamData', float]]):
        super().__init__(timeout=None)
        self.group_letter = group_letter
        self.group_teams = group_teams
        
        # 로스터 변경 버튼 (관리자만 사용 가능)
        self.roster_change_button = Button(
            label="로스터 변경",
            style=ButtonStyle.primary,
            emoji="✏️"
        )
        self.roster_change_button.callback = self.roster_change_callback
        self.add_item(self.roster_change_button)
    
    async def on_timeout(self) -> None:
        """View timeout 시 호출되는 메서드 (timeout=None이므로 실제로는 호출되지 않음)"""
        # timeout=None이므로 이 메서드는 호출되지 않지만, 안전을 위해 구현
        pass
    
    def update_group_teams(self, new_group_teams: List[Tuple[str, 'TeamData', float]]) -> None:
        """팀 정보를 업데이트합니다."""
        self.group_teams = new_group_teams
    
    async def _send_error_message(self, interaction: discord.Interaction, message: str) -> None:
        await send_error_message(interaction, message)

    async def roster_change_callback(self, interaction: discord.Interaction) -> None:
        """로스터 변경 버튼 콜백"""
        try:
            # 관리자 권한 확인
            if not is_admin(interaction.user):
                error_embed = Embed(
                    title="❌ 오류",
                    description="관리자 권한이 없습니다.",
                    color=Color.red()
                )
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=error_embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=error_embed, ephemeral=True)
                return

            # 팀 선택 드랍다운 메뉴 표시
            from .views import TeamSelectionView
            team_selection_view = TeamSelectionView(self)

            embed = Embed(
                title="팀 선택",
                description="변경할 팀을 선택하세요.",
                color=Color.blue()
            )
            
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, view=team_selection_view, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, view=team_selection_view, ephemeral=True)
                
        except discord.InteractionResponded:
            # 이미 응답된 상호작용 - 무시
            pass  # 이미 응답된 상호작용 무시
        except discord.NotFound:
            # 상호작용을 찾을 수 없음 - View가 만료되었을 가능성
            pass  # 상호작용을 찾을 수 없음 - View 만료 가능성
            # 새로운 View로 메시지를 업데이트 시도
            await self._recreate_view_on_message(interaction)
        except Exception as e:
            logger.error(f"로스터 변경 콜백 처리 실패: {e}", exc_info=True)
            await self._send_error_message(interaction, "로스터 변경 중 오류가 발생했습니다.")
    
    async def _recreate_view_on_message(self, interaction: discord.Interaction) -> None:
        """View가 만료된 경우 메시지를 새로운 View로 업데이트"""
        try:
            # 현재 메시지를 찾아서 새로운 View로 업데이트
            if hasattr(interaction, 'message') and interaction.message:
                # 새로운 View 생성
                new_view = GroupRosterView(self.group_letter, self.group_teams)
                
                # 메시지 업데이트
                await interaction.message.edit(view=new_view)
        except Exception as e:
            logger.error(f"View 재생성 실패: {e}", exc_info=True)


class TeamSelectionView(View):
    """
    팀 선택 뷰
    
    조별 로스터 변경 시 변경할 팀을 선택하는 드롭다운을 제공합니다.
    선택된 팀의 정보를 TeamEditModal로 전달합니다.
    """
    
    def __init__(self, parent_view: 'GroupRosterView'):
        super().__init__(timeout=None)  # 영구적으로 작동
        self.parent_view = parent_view
        self.is_empty = not parent_view.group_teams

        if self.is_empty:
            # 빈 팀 리스트일 때 placeholder 옵션 추가
            options = [SelectOption(label="등록된 팀이 없습니다", value="_empty", description="팀이 등록되면 선택 가능합니다")]
        else:
            options = [
                SelectOption(
                    label=f"{i+1}. {team_name} (MMR: {mmr:.2f})",
                    value=team_name,
                    description=f"팀원: {', '.join(team_data.players[:3]) if hasattr(team_data, 'players') and team_data.players and isinstance(team_data.players, (list, tuple)) else ', '.join(team_data.get('players', [])[:3]) if isinstance(team_data, dict) and team_data.get('players') and isinstance(team_data.get('players'), (list, tuple)) else '정보 없음'}"
                )
                for i, (team_name, team_data, mmr) in enumerate(parent_view.group_teams)
            ]

        # 팀 선택 드랍다운
        self.team_select = Select(
            placeholder="변경할 팀을 선택하세요",
            options=options,
            disabled=self.is_empty
        )
        self.team_select.callback = self.team_select_callback
        self.add_item(self.team_select)
    
    async def on_timeout(self) -> None:
        """View timeout 시 호출되는 메서드 (timeout=None이므로 실제로는 호출되지 않음)"""
        # timeout=None이므로 이 메서드는 호출되지 않지만, 안전을 위해 구현
        pass
    
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
                error_embed = Embed(
                    title="❌ 오류",
                    description="선택된 팀 정보를 찾을 수 없습니다.",
                    color=Color.red()
                )
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=error_embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=error_embed, ephemeral=True)
                return
            
            # 팀 정보 수정 모달 표시
            from .modals import TeamEditModal
            if not interaction.response.is_done():
                await interaction.response.send_modal(TeamEditModal(self.parent_view, selected_team_data))
            else:
                await interaction.followup.send_modal(TeamEditModal(self.parent_view, selected_team_data))
                
        except discord.InteractionResponded:
            # 이미 응답된 상호작용 - 무시
            pass  # 이미 응답된 상호작용 무시 (팀 선택)
        except discord.NotFound:
            # 상호작용을 찾을 수 없음 - View가 만료되었을 가능성
            pass  # 상호작용을 찾을 수 없음 - 팀 선택 View 만료 가능성
        except Exception as e:
            logger.error(f"팀 선택 콜백 처리 실패: {e}", exc_info=True)
            error_embed = Embed(
                title="❌ 오류",
                description="팀 선택 중 오류가 발생했습니다.",
                color=Color.red()
            )
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=error_embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=error_embed, ephemeral=True)
            except Exception as e2:
                logger.error(f"에러 메시지 전송 실패: {e2}", exc_info=True)


class CancelConfirmView(View):
    """
    팀 취소 확인 뷰

    취소 버튼 클릭 후 팀 정보를 표시하고 최종 확인을 받습니다.
    """

    def __init__(self, parent_view: 'TeamInputView', team_name: str):
        super().__init__(timeout=30)
        self.parent_view = parent_view
        self.team_name = team_name
        self.message: Optional[discord.Message] = None

        self.confirm_button = Button(label="등록 취소하기", style=ButtonStyle.danger, emoji="⚠️")
        self.confirm_button.callback = self.confirm_callback
        self.add_item(self.confirm_button)

        self.back_button = Button(label="돌아가기", style=ButtonStyle.secondary, emoji="↩️")
        self.back_button.callback = self.back_callback
        self.add_item(self.back_button)

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
                embed=Embed(title="❌ 오류", description="팀 취소 중 오류가 발생했습니다.", color=Color.red()),
                ephemeral=True
            )

    async def back_callback(self, interaction: discord.Interaction) -> None:
        """돌아가기 버튼 콜백"""
        try:
            cancel_embed = Embed(
                title="스크림 안내",
                description="이전 화면으로 돌아갔습니다.",
                color=Color.blue()
            )
            await interaction.response.edit_message(embed=cancel_embed, view=None)
        except Exception as e:
            logger.error(f"[뷰] 팀 취소 돌아가기 콜백 실패: {e}", exc_info=True)

    async def on_timeout(self) -> None:
        """타임아웃 시 버튼 비활성화 및 안내 메시지"""
        self.confirm_button.disabled = True
        self.back_button.disabled = True
        if self.message:
            try:
                timeout_embed = Embed(
                    title="⏳ 시간 초과",
                    description="시간이 초과되었습니다. 다시 시도해주세요.",
                    color=Color.greyple()
                )
                await self.message.edit(embed=timeout_embed, view=self)
            except Exception:
                pass


class AdminView(View):
    """
    관리자 전용 뷰
    
    관리자 권한이 있는 사용자만 접근 가능합니다.
    """
    
    def __init__(self, original_view: TeamInputView):
        super().__init__(timeout=None)
        self.original_view = original_view

        # 팀 목록 CSV 내보내기 버튼
        self.export_csv_button = Button(
            label="CSV 내보내기",
            style=ButtonStyle.secondary,
            emoji="📄"
        )
        self.export_csv_button.callback = self.export_csv_callback
        self.add_item(self.export_csv_button)
        
        # CSV 팀 입력 버튼
        self.import_csv_button = Button(
            label="CSV 불러오기",
            style=ButtonStyle.secondary,
            emoji="📥"
        )
        self.import_csv_button.callback = self.import_csv_callback
        self.add_item(self.import_csv_button)
    
    async def export_csv_callback(self, interaction: discord.Interaction) -> None:
        """현재 등록된 팀 목록을 CSV로 내보냅니다."""
        try:
            if not is_admin(interaction.user):
                await interaction.response.send_message("관리자만 사용할 수 있습니다.", ephemeral=True)
                return

            team_data_manager = BotManager.get_instance().get_team_data_manager()
            teams = team_data_manager.get_all_teams()

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["team_name", "players", "staff"])

            for team_name in sorted(teams.keys()):
                team = teams[team_name]
                players = team.players if hasattr(team, "players") else team.get("players", [])
                staff = team.staff if hasattr(team, "staff") else team.get("staff", [])
                writer.writerow([
                    team_name,
                    ", ".join(players),
                    ", ".join(staff)
                ])

            csv_bytes = output.getvalue().encode("utf-8-sig")
            file = discord.File(io.BytesIO(csv_bytes), filename="teams.csv")

            if not interaction.response.is_done():
                await interaction.response.send_message(file=file, ephemeral=True)
            else:
                await interaction.followup.send(file=file, ephemeral=True)

        except Exception as e:
            logger.error(f"팀 CSV 내보내기 실패: {e}", exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("CSV 내보내기 중 오류가 발생했습니다.", ephemeral=True)
                else:
                    await interaction.followup.send("CSV 내보내기 중 오류가 발생했습니다.", ephemeral=True)
            except Exception:
                pass
    
    async def import_csv_callback(self, interaction: discord.Interaction) -> None:
        """CSV 형식으로 팀을 일괄 등록합니다."""
        try:
            if not is_admin(interaction.user):
                await interaction.response.send_message("관리자만 사용할 수 있습니다.", ephemeral=True)
                return

            # CSV 입력 모달 표시
            from .modals import CSVImportModal
            if not interaction.response.is_done():
                await interaction.response.send_modal(CSVImportModal(self))
            else:
                await interaction.followup.send("모달을 표시할 수 없습니다. 다시 시도해주세요.", ephemeral=True)
                
        except Exception as e:
            logger.error(f"CSV 입력 콜백 처리 실패: {e}", exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("CSV 입력 중 오류가 발생했습니다.", ephemeral=True)
                else:
                    await interaction.followup.send("CSV 입력 중 오류가 발생했습니다.", ephemeral=True)
            except Exception:
                pass

    async def _send_error_message(self, interaction: discord.Interaction, message: str) -> None:
        await send_error_message(interaction, message)


class ScrimResetConfirmView(View):
    """
    스크림 초기화 확인 뷰

    진행 중인 스크림이 있을 때 /스크림 명령어 실행 시 확인을 받습니다.
    """

    def __init__(self):
        super().__init__(timeout=30)
        self.confirmed: Optional[bool] = None
        self.message: Optional[discord.Message] = None

        self.confirm_button = Button(label="초기화", style=ButtonStyle.danger, emoji="⚠️")
        self.confirm_button.callback = self.confirm_callback
        self.add_item(self.confirm_button)

        self.cancel_button = Button(label="취소", style=ButtonStyle.secondary, emoji="↩️")
        self.cancel_button.callback = self.cancel_callback
        self.add_item(self.cancel_button)

    async def confirm_callback(self, interaction: discord.Interaction) -> None:
        self.confirmed = True
        self.confirm_button.disabled = True
        self.cancel_button.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def cancel_callback(self, interaction: discord.Interaction) -> None:
        self.confirmed = False
        self.confirm_button.disabled = True
        self.cancel_button.disabled = True
        cancel_embed = Embed(
            title="스크림 안내",
            description="스크림 초기화가 취소되었습니다.",
            color=Color.blue()
        )
        await interaction.response.edit_message(embed=cancel_embed, view=self)
        self.stop()

    async def on_timeout(self) -> None:
        self.confirmed = None
        self.confirm_button.disabled = True
        self.cancel_button.disabled = True
        if self.message:
            try:
                timeout_embed = Embed(
                    title="⏳ 시간 초과",
                    description="시간이 초과되었습니다. 다시 시도해주세요.",
                    color=Color.greyple()
                )
                await self.message.edit(embed=timeout_embed, view=self)
            except Exception:
                pass
