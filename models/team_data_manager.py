"""팀 데이터 관리 모델

스크림 팀 등록, 취소, MMR 관리, 조편성 등의 핵심 기능을 담당합니다.
메모리 기반으로 팀 데이터를 관리하며 자동 조편성과 MMR 업데이트를 수행합니다.
"""
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union, TYPE_CHECKING

import discord

from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_current_kst_time
from utils.validators import normalize_nickname_for_comparison

from .team_data import TeamData

if TYPE_CHECKING:  # pragma: no cover
    from bot.manager import BotManager  # type: ignore

logger = get_logger('team_data_manager')


class TeamDataManager:
    """
    팀 데이터 관리 클래스
    
    스크림 팀의 등록, 취소, MMR 관리, 조편성 등의 모든 기능을 담당합니다.
    CSV 파일과 사용자 등록 팀을 통합 관리하며, 자동 조편성 및 MMR 업데이트를 수행합니다.
    """
    
    def __init__(self, client=None):
        self.client = client  # 클라이언트 참조 저장
        self.teams: Dict[str, TeamData] = {}  # 새로운 TeamData 구조 사용
        self.user_teams: Dict[str, str] = {}  # 사용자 ID -> 팀명 매핑 (O(1) 탐색)
        self.team_by_member: Dict[str, str] = {}  # 멤버명 -> 팀명 매핑 (O(1) 탐색)
        self.team_mmr_index: Dict[float, List[str]] = {}  # MMR -> 팀명 리스트 (정렬용)
        self.scrim_day: Optional[int] = None
        self.scrim_month: Optional[int] = None
        self.auto_assignment_task: Optional[asyncio.Task] = None
        self.mmr_update_task: Optional[asyncio.Task] = None
        self.last_auto_assignment: Optional[datetime] = None
        self.logs: Dict[str, List] = {"신청": [], "취소": [], "수정": []}
        self.is_team_assignment_started: bool = False
        self.mmr_message: Optional[discord.Message] = None
        self.additional_mmr_messages: List[discord.Message] = []
        self.scrim_channel_id: Optional[int] = None  # 스크림 명령어가 실행된 채널 ID
    
    def _update_member_index(self, team_name: str, team: TeamData) -> None:
        """멤버 인덱스를 업데이트합니다."""
        self._remove_member_index(team_name, team)
        self._add_member_index(team_name, team)

    def _remove_member_index(self, team_name: str, team: TeamData) -> None:
        """멤버 인덱스에서 팀 멤버를 제거합니다."""
        for member in team.all_members:
            key = self._normalize_member_key(member)
            if self.team_by_member.get(key) == team_name:
                self.team_by_member.pop(key, None)

    def _add_member_index(self, team_name: str, team: TeamData) -> None:
        """멤버 인덱스에 팀 멤버를 추가합니다."""
        for member in team.all_members:
            key = self._normalize_member_key(member)
            self.team_by_member[key] = team_name

    @staticmethod
    def _normalize_member_key(member: str) -> str:
        """멤버 키 정규화 (대소문자/공백 무시)"""
        return normalize_nickname_for_comparison(member)
    
    def _update_mmr_index(self, team_name: str, old_mmr: float, new_mmr: float) -> None:
        """MMR 인덱스를 업데이트합니다."""
        # 기존 MMR에서 제거
        if old_mmr in self.team_mmr_index:
            if team_name in self.team_mmr_index[old_mmr]:
                self.team_mmr_index[old_mmr].remove(team_name)
            if not self.team_mmr_index[old_mmr]:  # 빈 리스트면 키 삭제
                del self.team_mmr_index[old_mmr]
        
        # 새 MMR에 추가
        if new_mmr not in self.team_mmr_index:
            self.team_mmr_index[new_mmr] = []
        if team_name not in self.team_mmr_index[new_mmr]:
            self.team_mmr_index[new_mmr].append(team_name)
    
    def get_team_by_member(self, member_name: str) -> Optional[str]:
        """멤버명으로 팀을 O(1)로 조회합니다."""
        key = self._normalize_member_key(member_name)
        return self.team_by_member.get(key)
    
    
    async def reset_team_data(self) -> None:
        """모든 팀 데이터를 초기화합니다.
        
        비동기 태스크를 완전히 종료할 때까지 대기한 후 데이터를 초기화합니다.
        """
        try:
            logger.info("[팀데이터] 초기화 시작")
            
            # 비동기 태스크를 완전히 종료할 때까지 대기
            await self._cancel_task_and_wait(self.auto_assignment_task, "auto_assignment_task")
            await self._cancel_task_and_wait(self.mmr_update_task, "mmr_update_task")

            self.teams.clear()
            self.user_teams.clear()
            self.team_by_member.clear()
            self.team_mmr_index.clear()
            self.last_auto_assignment = None

            self.auto_assignment_task = None
            self.mmr_update_task = None

            self.logs = {"신청": [], "취소": [], "수정": []}
            self.mmr_message = None
            self.additional_mmr_messages = []
            self.is_team_assignment_started = False
            self.scrim_channel_id = None
            self.scrim_day = None
            self.scrim_month = None

            self.log_state_snapshot(prefix="reset")
            logger.info("[팀데이터] 초기화 완료")
        except Exception as e:
            logger.error(f"[팀데이터] 초기화 실패: {e}", exc_info=True)

    async def initialize_new_scrim(self, scrim_day: int, scrim_month: int, scrim_channel_id: int) -> None:
        """
        새로운 스크림을 시작하기 위한 하드 리셋 및 날짜/채널 설정.
        
        이전 스크림의 모든 태스크를 완전히 종료하고 데이터를 초기화합니다.
        """
        await self.reset_team_data()
        self.scrim_day = scrim_day
        self.scrim_month = scrim_month
        self.scrim_channel_id = scrim_channel_id
        self.log_state_snapshot(prefix="initialize_new_scrim")

    def _cancel_task(self, task: Optional[asyncio.Task], label: str) -> None:
        """비동기 태스크를 안전하게 취소 (동기 버전 - 레거시 호환용)"""
        try:
            if task and not task.done():
                task.cancel()
        except Exception as exc:
            logger.warning(f"{label} 취소 중 예외 무시: {exc}")
    
    async def _cancel_task_and_wait(self, task: Optional[asyncio.Task], label: str, timeout: float = 10.0) -> None:
        """비동기 태스크를 취소하고 완전히 종료될 때까지 대기합니다.
        
        Args:
            task: 취소할 태스크
            label: 로깅용 태스크 이름
            timeout: 태스크 종료 대기 타임아웃 (초) - 기본값 10초로 증가
        """
        if not task:
            return
        
        if task.done():
            logger.debug(f"{label}: 태스크가 이미 완료됨")
            return
        
        try:
            # 취소 요청
            task.cancel()
            logger.info(f"{label}: 태스크 취소 요청됨, 종료 대기 중...")
            
            try:
                # 태스크가 완전히 종료될 때까지 대기 (타임아웃 적용)
                await asyncio.wait_for(task, timeout=timeout)
            except asyncio.CancelledError:
                # 정상적인 취소
                logger.info(f"{label}: 태스크가 정상적으로 취소됨")
            except asyncio.TimeoutError:
                # 타임아웃 - 강제 종료 시도
                logger.warning(f"{label}: 태스크 취소 타임아웃 ({timeout}초), 강제 종료 시도")
                # 태스크가 여전히 실행 중이면 다시 취소 요청
                if not task.done():
                    task.cancel()
                    # 추가 대기 시간
                    try:
                        await asyncio.wait_for(task, timeout=2.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
            except Exception as e:
                # 태스크 내부에서 발생한 예외
                logger.warning(f"{label}: 태스크 종료 중 예외 발생: {e}")
            
            # 최종 확인: 태스크가 완전히 종료되었는지 확인
            if not task.done():
                logger.warning(f"{label}: 태스크가 완전히 종료되지 않았지만 계속 진행합니다.")
        except Exception as exc:
            logger.warning(f"{label}: 태스크 취소 중 예외 무시: {exc}")
    
    def get_team_counts(self) -> Tuple[int, int, int]:
        """전체 팀 수, 완성된 그룹 수, 예비팀 수를 반환합니다."""
        try:
            total_teams = len(self.teams)
            num_groups = total_teams // settings.TEAMS_PER_GROUP
            num_complete_groups = num_groups * settings.TEAMS_PER_GROUP
            spare_teams = total_teams - num_complete_groups
            
            return total_teams, num_groups, spare_teams
        except Exception as e:
            logger.error(f"[팀데이터] 팀 카운트 계산 실패: {e}", exc_info=True)
            return 0, 0, 0
    
    async def check_team_registration_allowed(
        self,
        current_time: datetime,
        new_team: Optional[TeamData] = None,
        allow_admin_override: bool = False
    ) -> Tuple[bool, str]:
        """팀 등록이 가능한지 확인합니다."""
        # 조편성 시작 이후인지 확인 (관리자 오버라이드 시 건너뜀)
        if self.is_team_assignment_started and not allow_admin_override:
            return False, "❌ 조편성이 이미 시작되어 팀 등록이 불가능합니다.\n\n❌ Team registration is not available as group assignment has already started."

        # 경고 제한 확인
        from bot.manager import BotManager

        warning_manager = BotManager.get_instance().get_warning_manager()
        
        if warning_manager and warning_manager.worksheet:
            # 새로 등록하려는 팀의 멤버 확인
            if new_team:
                for member in new_team.all_members:
                    # 멤버명으로 제한 확인 (Discord ID는 팀 등록 시 저장되지 않으므로 닉네임으로 확인)
                    is_restricted, restricted_until = warning_manager.is_restricted(
                        target_name=member,
                        check_date=current_time
                    )
                    if is_restricted:
                        return False, (
                            f"⚠️ 팀원 '{member}'이(가) 경고로 인해 스크림 참가가 제한되었습니다.\n"
                            f"제한 해제일: {restricted_until}\n\n"
                            f"⚠️ Team member '{member}' is restricted from participating in scrims due to a warning.\n"
                            f"Restriction lifted on: {restricted_until}"
                        )
            
            # 이미 등록된 팀 멤버별 제한 확인
            for team_name, team_data in self.teams.items():
                for member in team_data.all_members:
                    # 멤버명으로 제한 확인 (Discord ID는 팀 등록 시 저장되지 않으므로 닉네임으로 확인)
                    is_restricted, restricted_until = warning_manager.is_restricted(
                        target_name=member,
                        check_date=current_time
                    )
                    if is_restricted:
                        return False, (
                            f"⚠️ 팀원 '{member}'이(가) 경고로 인해 스크림 참가가 제한되었습니다.\n"
                            f"제한 해제일: {restricted_until}\n\n"
                            f"⚠️ Team member '{member}' is restricted from participating in scrims due to a warning.\n"
                            f"Restriction lifted on: {restricted_until}"
                        )
        
        # 스크림 날짜가 다른 경우 등록 가능 (관리자 오버라이드와 무관)
        if self.scrim_day != current_time.day:
            return True, ""

        # 17시 이전에는 모든 팀 등록 가능
        if current_time.hour < 17 or allow_admin_override:
            return True, ""

        # 17시 이후 로직
        if current_time.hour >= 17 and not allow_admin_override:
            return False, (
                "⏰ 17:00 이후에는 추가 등록이 불가능합니다.\n\n"
                "⏰ Team registration is closed after 17:00."
            )

        return True, ""

    async def check_team_cancellation_allowed(self, current_time: datetime) -> Tuple[bool, str]:
        """팀 취소가 가능한지 확인합니다.
        
        Note: 조편성 체크는 호출하는 쪽(_process_team_cancellation)에서 이미 수행되므로
        여기서는 추가적인 시간 제한 등이 필요한 경우에만 체크합니다.
        현재는 조편성 시작 이전에는 항상 취소 가능합니다.
        """
        # 조편성 체크는 _process_team_cancellation에서 이미 수행됨
        # 조편성 시작 이전에는 항상 취소 가능
        return True, ""
    
    async def check_team_edit_allowed(self, current_time: datetime) -> Tuple[bool, str]:
        """팀 수정이 가능한지 확인합니다."""
        # 조편성 시작/완료 이후인지 확인 (가장 우선)
        if self.is_team_assignment_started:
            return False, (
                "❌ 조편성이 이미 시작되어 팀 수정이 불가능합니다.\n\n"
                "❌ Team edit is not available as group assignment has already started."
            )
        
        # 스크림 날짜가 다른 경우 수정 가능
        if self.scrim_day != current_time.day:
            return True, ""
        
        # 17시 이전에는 모든 팀 수정 가능
        if current_time.hour < 17:
            return True, ""
        
        # 17시 이후 로직
        if current_time.hour >= 17:
            return False, (
                "⏰ 17:00 이후에는 팀 수정이 불가능합니다.\n\n"
                "⏰ Team edit is not available after 17:00."
            )
        
        return True, ""

    def _should_check_auto_assign(self) -> bool:
        """자동 조편성 체크가 필요한지 확인합니다."""
        if not self.scrim_day or not self.scrim_month:
            return False
            
        current_time = get_current_kst_time()
        
        # 현재 날짜와 스크림 날짜/월이 일치하는지 확인
        if current_time.day != self.scrim_day or current_time.month != self.scrim_month:
            return False
            
        if (self.last_auto_assignment and 
            self.last_auto_assignment.date() == current_time.date()):
            return False
            
        return True

    def _is_scrim_date_today(self) -> bool:
        """스크림 설정 날짜가 오늘과 일치하는지 확인"""
        if not self.scrim_day or not self.scrim_month:
            return False
        current_time = get_current_kst_time()
        return (
            current_time.day == self.scrim_day
            and current_time.month == self.scrim_month
        )

    def log_state_snapshot(self, prefix: str = "state") -> None:
        """현재 스크림 상태를 로그로 남깁니다."""
        try:
            auto_alive = bool(self.auto_assignment_task and not self.auto_assignment_task.done())
            mmr_alive = bool(self.mmr_update_task and not self.mmr_update_task.done())
            logger.info(
                f"[{prefix}] teams={len(self.teams)}, scrim_date={self.scrim_month}/{self.scrim_day}, "
                f"scrim_channel={self.scrim_channel_id}, auto_task_alive={auto_alive}, mmr_task_alive={mmr_alive}"
            )
        except Exception as exc:
            logger.warning(f"[팀데이터] 상태 스냅샷 로깅 실패: {exc}")

    def log_action(self, action_type: str, user: discord.Member, team_name: str) -> None:
        """액션 로그를 기록합니다."""
        try:
            current_time = get_current_kst_time()
            log_entry = {
                "user": user.display_name,
                "user_id": user.id,
                "team": team_name,
                "time": current_time.strftime('%H:%M'),
                "timestamp": current_time
            }
            
            self.logs[action_type].append(log_entry)
        except Exception as e:
            logger.error(f"[팀데이터] 로그 기록 실패: {e}", exc_info=True)

    def is_admin(self, user: discord.Member) -> bool:
        """사용자가 관리자인지 확인합니다."""
        return any(role.id in settings.ADMIN_ROLE_IDS for role in user.roles)

    async def add_team(
        self,
        team_name: str,
        team_data: Union[Dict, List[str], TeamData],
        user: discord.Member,
        allow_admin_override: bool = False
    ) -> Tuple[bool, str]:
        """팀을 추가합니다.
        
        Returns:
            (성공 여부, 실패 사유 또는 빈 문자열)
        """
        try:
            # TeamData 입력 형태에 따라 객체 준비
            if isinstance(team_data, TeamData):
                # 이름이 다르면 주어진 팀명으로 덮어씁니다.
                if team_data.name != team_name:
                    team_data.name = team_name
                team = team_data
            elif isinstance(team_data, dict):
                team = TeamData.from_dict(team_name, team_data)
            elif isinstance(team_data, list):
                # 리스트인 경우 (레거시 지원)
                team = TeamData(name=team_name, players=team_data, staff=[])
            else:
                raise TypeError(f"지원하지 않는 팀 데이터 형식: {type(team_data)}")
            
            # 팀 등록 가능 여부 확인 (새 팀 멤버 포함)
            current_time = get_current_kst_time()
            is_allowed, reason = await self.check_team_registration_allowed(
                current_time,
                new_team=team,
                allow_admin_override=allow_admin_override
            )
            
            if not is_allowed:
                return False, reason
            
            team.user_id = str(user.id)
            self.teams[team_name] = team
            
            # 인덱스 업데이트
            self._update_member_index(team_name, team)
            self._update_mmr_index(team_name, 0.0, team.mmr)
            
            # 사용자-팀 매핑 업데이트
            for member in team.all_members:
                key = self._normalize_member_key(member)
                self.user_teams[key] = team_name
            
            return True, ""
            
        except Exception as e:
            logger.error(f"[팀데이터] 팀 추가 실패: {e}", exc_info=True)
            return False, f"팀 추가 중 오류가 발생했습니다: {str(e)}"

    async def remove_team(self, team_name: str) -> Tuple[bool, str]:
        """팀을 제거합니다.
        
        Returns:
            Tuple[bool, str]: (성공 여부, 실패 사유 또는 빈 문자열)
        """
        try:
            # 조편성 체크는 호출하는 쪽에서 이미 수행하므로 여기서는 제거
            # 팀 존재 확인
            if team_name not in self.teams:
                return False, "등록되지 않은 팀명입니다. / Team name not registered."
            
            team = self.teams[team_name]
            
            # 인덱스에서 제거
            self._remove_member_index(team_name, team)
            self._update_mmr_index(team_name, team.mmr, 0.0)
            
            # 사용자-팀 매핑에서 제거
            for member in team.all_members:
                key = self._normalize_member_key(member)
                if key in self.user_teams:
                    del self.user_teams[key]
            
            # 팀 데이터 제거
            del self.teams[team_name]
            
            return True, ""
            
        except Exception as e:
            logger.error(f"[팀데이터] 팀 제거 실패: {e}", exc_info=True)
            return False, f"팀 제거 중 오류가 발생했습니다: {str(e)}"

    def get_team_data(self, team_name: str) -> Optional[TeamData]:
        """팀 데이터를 가져옵니다."""
        return self.teams.get(team_name)

    def get_all_teams(self) -> Dict[str, TeamData]:
        """모든 팀 데이터를 가져옵니다."""
        return self.teams.copy()

    def get_team_mmr(self, team_name: str) -> Optional[float]:
        """팀의 평균 MMR을 가져옵니다."""
        team = self.teams.get(team_name)
        return team.mmr if team else None
    
    def set_team_mmr(self, team_name: str, mmr: float) -> None:
        """팀의 평균 MMR을 설정합니다."""
        team = self.teams.get(team_name)
        if team:
            old_mmr = team.mmr
            team.mmr = mmr
            # MMR 인덱스 업데이트
            self._update_mmr_index(team_name, old_mmr, mmr)

    def get_logs(self) -> Dict[str, List]:
        """로그를 가져옵니다."""
        return self.logs.copy()

    def replace_team(self, old_team_name: str, new_team: TeamData, new_mmr: float) -> None:
        """기존 팀을 새 팀으로 교체하며 인덱스·MMR 정보를 일관되게 갱신합니다."""
        # 기존 팀 제거
        if old_team_name in self.teams:
            old_team = self.teams[old_team_name]
            self._remove_member_index(old_team_name, old_team)
            self._update_mmr_index(old_team_name, old_team.mmr, 0.0)
            for member in old_team.all_members:
                key = self._normalize_member_key(member)
                self.user_teams.pop(key, None)
            del self.teams[old_team_name]

        # 새 팀 추가
        self.teams[new_team.name] = new_team
        new_team.mmr = new_mmr
        self._add_member_index(new_team.name, new_team)
        self._update_mmr_index(new_team.name, 0.0, new_mmr)
        for member in new_team.all_members:
            key = self._normalize_member_key(member)
            self.user_teams[key] = new_team.name

    def set_scrim_date(self, day: int, month: int) -> None:
        """스크림 날짜를 설정합니다."""
        self.scrim_day = day
        self.scrim_month = month

    
    def set_scrim_channel(self, channel_id: int) -> None:
        """스크림 명령어가 실행된 채널을 설정합니다."""
        self.scrim_channel_id = channel_id
    
    async def check_and_auto_assign(self) -> None:
        """자동 조편성 조건을 체크하고 실행합니다."""
        while True:
            try:
                await asyncio.sleep(settings.AUTO_ASSIGNMENT_CHECK_INTERVAL)
                
                # ✅ 최신 TeamDataManager 인스턴스를 동적으로 가져오기
                from bot.manager import BotManager
                team_data_manager = BotManager.get_instance().get_team_data_manager()
                
                # 최신 인스턴스에서 데이터 가져오기
                if not team_data_manager._should_check_auto_assign():
                    continue
                
                current_time = get_current_kst_time()
                total_teams, _, spare_teams = team_data_manager.get_team_counts()

                # 17시가 되면 즉시 조편성 시작
                if current_time.hour >= 17:
                    await team_data_manager._start_team_assignment(total_teams, spare_teams)
                    break
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[조편성] 자동 조편성 체크 실패: {e}", exc_info=True)
                await asyncio.sleep(settings.AUTO_ASSIGNMENT_CHECK_INTERVAL)
    
    async def _start_team_assignment(self, total_teams: int, spare_teams: int) -> None:
        """조편성을 시작합니다."""
        start_channel = None  # 변수를 메서드 시작 부분에서 초기화
        try:
            # ✅ 최신 TeamDataManager 인스턴스를 동적으로 가져오기
            from bot.manager import BotManager
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            
            # 날짜 검증 강화: 현재 날짜와 스크림 날짜가 일치하는지 재확인
            current_time = get_current_kst_time()
            if not team_data_manager._is_scrim_date_today():
                logger.warning(
                    f"[조편성] 날짜 불일치로 중단 - scrim_date: {team_data_manager.scrim_month}/{team_data_manager.scrim_day}, "
                    f"현재 날짜: {current_time.month}/{current_time.day}"
                )
                return
            
            # 추가 검증: 날짜가 정확히 일치하는지 확인
            if (team_data_manager.scrim_day != current_time.day or 
                team_data_manager.scrim_month != current_time.month):
                logger.warning(
                    f"[조편성] 날짜 불일치로 중단 - scrim_date: {team_data_manager.scrim_month}/{team_data_manager.scrim_day}, "
                    f"현재 날짜: {current_time.month}/{current_time.day}"
                )
                return

            total_teams_current = len(team_data_manager.teams)
            team_data_manager.log_state_snapshot(prefix="start_team_assignment")
            
            logger.info(f"[조편성] 조편성 시작 - 총 팀 수: {total_teams_current}개, 예비팀: {spare_teams}개")
            
            team_data_manager.is_team_assignment_started = True
            
            # ✅ scrim.py 방식: 직접 가져오기, 체크 없음
            client = BotManager.get_instance().get_client()
            
            # 조편성 시작 공지 채널
            if client:
                start_channel = client.get_channel(settings.AUTO_ASSIGNMENT_START_CHANNEL_ID)
            else:
                start_channel = None
            if start_channel:
                # 조편성 시작 임베드 생성
                start_embed = discord.Embed(
                    title="⚙️ 자동 조편성 시작",
                    description="팀 데이터를 처리하고 있습니다...",
                    color=discord.Color.blue()
                )
                start_embed.add_field(
                    name="현재 시각",
                    value=current_time.strftime('%H:%M'),
                    inline=True
                )
                start_embed.add_field(
                    name="총 팀 수",
                    value=str(total_teams_current),
                    inline=True
                )
                start_embed.add_field(
                    name="예비팀 수",
                    value=str(total_teams_current % settings.TEAMS_PER_GROUP),
                    inline=True
                )
                start_embed.set_footer(text="ER Scrim", icon_url=settings.THUMBNAIL_URL)
                
                await start_channel.send(embed=start_embed)
                
                # ✅ 조편성 실행
                await team_data_manager.execute_auto_assignment()
                
                team_data_manager.last_auto_assignment = current_time
            else:
                logger.warning("[조편성] 자동 조편성 시작 채널을 찾을 수 없음 - 조편성은 계속 진행")
                # 채널이 없어도 조편성은 실행
                await team_data_manager.execute_auto_assignment()
                team_data_manager.last_auto_assignment = get_current_kst_time()
        except Exception as e:
            error_msg = f"❌ 자동 조편성 중 오류가 발생했습니다: {str(e)}"
            logger.error(f"[조편성] 자동 조편성 중 오류: {str(e)}", exc_info=True)
            # 오류 발생 시 조편성 플래그 해제 (최신 인스턴스에서)
            from bot.manager import BotManager
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            team_data_manager.is_team_assignment_started = False
            
            if start_channel:
                error_embed = discord.Embed(
                    title="❌ 오류 발생",
                    description=error_msg,
                    color=discord.Color.red()
                )
                error_embed.set_footer(text="ER Scrim", icon_url=settings.THUMBNAIL_URL)
                await start_channel.send(embed=error_embed)
    
    async def execute_auto_assignment(self) -> None:
        """실제 조편성을 실행합니다."""
        try:
            # ✅ 최신 TeamDataManager 인스턴스에서 팀 데이터 가져오기
            from bot.manager import BotManager
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            
            # 팀 데이터 검증
            if not team_data_manager.teams or len(team_data_manager.teams) == 0:
                error_msg = "조편성 실행 시 팀 데이터가 없습니다."
                logger.error("[조편성] 팀 데이터 없음")
                raise ValueError("팀 데이터가 없어 조편성을 실행할 수 없습니다.")
            
            # 저장된 클라이언트 참조 사용 (초기화 시 설정됨)
            client = team_data_manager.client
            if not client:
                client = BotManager.get_instance().get_client()
            
            # ✅ BotManager에서 싱글톤 TeamProcessor 가져오기
            team_processor = BotManager.get_instance().get_team_processor()
            
            # 조편성 실행 (Discord 작업 제외) - 최신 인스턴스의 팀 데이터 사용
            groups, unmatched_teams = await team_processor.process_teams_background(team_data_manager.teams, None)
            
            logger.info(f"[조편성] 조편성 실행 완료 - 조 수: {len(groups)}개, 매칭되지 않은 팀: {len(unmatched_teams)}개")
            
            # Discord 서비스 실행 (클라이언트가 있을 때만)
            if client:
                await team_data_manager._execute_discord_services(client, groups, unmatched_teams)
            else:
                logger.warning("클라이언트가 없어 Discord 서비스를 건너뜁니다.")
        except Exception as e:
            error_msg = f"자동 조편성 실행 중 오류 발생: {e}"
            logger.error(error_msg)
            # 오류 발생 시 조편성 플래그 해제 (최신 인스턴스에서)
            from bot.manager import BotManager
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            team_data_manager.is_team_assignment_started = False
            
            # 오류 메시지 전송 (클라이언트가 있으면)
            try:
                client = team_data_manager.client
                if client and team_data_manager.scrim_channel_id:
                    channel = client.get_channel(team_data_manager.scrim_channel_id)
                    if channel:
                        error_embed = discord.Embed(
                            title="❌ 조편성 오류",
                            description=error_msg,
                            color=discord.Color.red()
                        )
                        await channel.send(embed=error_embed)
            except Exception as e2:
                logger.error(f"[Discord] 오류 메시지 전송 실패: {e2}", exc_info=True)
        finally:
            # 조편성 완료 (플래그는 영구적으로 유지)
            pass
    
    async def _execute_discord_services(self, client, groups, unmatched_teams):
        """Discord 서비스를 실행합니다 (공지, 역할, 채널 관리 등)."""
        try:
            # ✅ BotManager에서 싱글톤 TeamProcessor 가져오기
            from bot.manager import BotManager

            team_processor = BotManager.get_instance().get_team_processor()
            
            # 서버 가져오기
            guild = client.get_guild(settings.GUILD_ID)
            if not guild:
                logger.warning(f"[Discord] 서버를 찾을 수 없음 - 서버 ID: {settings.GUILD_ID}")
                return
            
            # Discord 서비스 실행
            
            # 전체 공지 전송
            await team_processor._send_global_announcement(guild, groups, unmatched_teams)
            
            # 조별 공지 전송
            await team_processor._send_notices(guild, groups, unmatched_teams)
            
            
        except Exception as e:
            logger.error(f"[Discord] 서비스 실행 실패: {e}", exc_info=True)
    
    async def update_mmr_message(self, channel: discord.TextChannel) -> None:
        """MMR 메시지를 이미지로 업데이트합니다."""
        try:
            # 조편성 시작 이후인지 확인
            if self.is_team_assignment_started:
                logger.warning("[MMR메시지] 조편성 시작 이후이므로 갱신 불가")
                return
            
            # 이미지 생성
            from services.image_generator import ImageGenerator
            img_io = ImageGenerator.generate_mmr_image(self.teams)
            
            if not img_io:
                logger.error("[MMR메시지] 이미지 생성 실패", exc_info=True)
                return
            
            # 임베드 생성
            embed = discord.Embed(
                title="📊 팀 MMR 정보 / Team MMR Info",
                description=f"Total {len(self.teams)} teams • Last updated: {get_current_kst_time().strftime('%H:%M')}",
                color=discord.Color.blue()
            )
            embed.set_image(url="attachment://mmr_table.png")
            
            # 공지사항 추가 (설정에서 가져오기)
            if settings.ANNOUNCEMENT_MESSAGE:
                embed.add_field(
                    name="📢 공지사항 / Announcement",
                    value=settings.ANNOUNCEMENT_MESSAGE,
                    inline=False
                )
            
            embed.set_footer(text="ER Scrim", icon_url=settings.THUMBNAIL_URL)
            
            # 기존 메시지가 있는지 확인하고 업데이트 시도
            if self.mmr_message:
                try:
                    # 기존 메시지 편집 시도
                    await self.mmr_message.edit(
                        embed=embed,
                        attachments=[discord.File(img_io, filename='mmr_table.png')]
                    )
                    return
                except discord.NotFound:
                    # 메시지가 삭제된 경우 새로 생성
                    logger.warning("[MMR메시지] 기존 메시지를 찾을 수 없음 - 새로 생성")
                    self.mmr_message = None
                except discord.HTTPException as e:
                    # HTTP 오류 (클라이언트 재생성 등으로 인한 무효화)
                    logger.warning(f"[MMR메시지] 기존 메시지 편집 중 HTTP 오류 - 새로 생성: {e}")
                    self.mmr_message = None
                except Exception as e:
                    logger.error(f"[MMR메시지] 기존 메시지 편집 실패: {e}", exc_info=True)
                    # 편집 실패 시 새로 생성
                    self.mmr_message = None
            
            # 기존 메시지가 없거나 편집에 실패한 경우 새로 생성
            new_message = await channel.send(
                embed=embed, 
                file=discord.File(img_io, filename='mmr_table.png')
            )
            
            # 메시지 참조 업데이트
            self.mmr_message = new_message
            self.additional_mmr_messages = []

        except Exception as e:
            logger.error(f"[MMR메시지] 업데이트 실패: {e}", exc_info=True)
            raise
    
    async def mmr_update_loop(self) -> None:
        """MMR 정보를 주기적으로 업데이트합니다."""
        try:
            # 첫 번째 실행 시 즉시 체크
            first_run = True
            
            while True:
                if not first_run:
                    await asyncio.sleep(300)  # 5분마다 업데이트
                first_run = False
                
                try:
                    # ✅ 최신 TeamDataManager 인스턴스를 동적으로 가져오기
                    from bot.manager import BotManager
                    team_data_manager = BotManager.get_instance().get_team_data_manager()
                    
                    # 현재 시간 확인
                    current_time = get_current_kst_time()
                    
                    # 17시 이후에는 MMR 업데이트 중단
                    if current_time.hour >= 17:
                        team_data_manager.mmr_update_task = None
                        return
                    
                    # 팀이 있고 MMR 메시지가 있는 경우에만 업데이트
                    if team_data_manager.teams and team_data_manager.mmr_message and team_data_manager.mmr_message.channel:
                        # 모든 팀의 MMR을 주기적으로 갱신
                        await team_data_manager._update_all_team_mmr()
                        await team_data_manager.update_mmr_message(team_data_manager.mmr_message.channel)
                except discord.NotFound:
                    # 최신 인스턴스에서 메시지 참조 제거
                    from bot.manager import BotManager
                    team_data_manager = BotManager.get_instance().get_team_data_manager()
                    team_data_manager.mmr_message = None
                except Exception as e:
                    logger.error(f"[MMR갱신] 업데이트 루프 실패: {e}", exc_info=True)
                    await asyncio.sleep(60)  # 오류 발생 시 1분 대기 후 재시도
        except asyncio.CancelledError:
            # 최신 인스턴스에서 태스크 참조 제거
            from bot.manager import BotManager
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            team_data_manager.mmr_update_task = None
        except Exception as e:
            logger.error(f"[MMR갱신] 업데이트 루프 종료: {e}", exc_info=True)
            # 태스크 재시작 (최신 인스턴스에서)
            from bot.manager import BotManager
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            asyncio.create_task(team_data_manager.mmr_update_loop())
    
    async def _update_all_team_mmr(self) -> None:
        """모든 팀의 MMR을 갱신합니다."""
        try:
            # ✅ BotManager에서 싱글톤 TeamProcessor 가져오기
            from bot.manager import BotManager

            team_processor = BotManager.get_instance().get_team_processor()
            
            # 딕셔너리 순회 중 변경을 방지하기 위해 복사본 사용
            teams_copy = dict(self.teams)
            for team_name, team_data in teams_copy.items():
                try:
                    # MMR 계산
                    _, _, team_mmr = await team_processor.fetch_team_mmr(team_name, team_data)
                    self.set_team_mmr(team_name, team_mmr)
                    
                except Exception as e:
                    logger.error(f"[MMR갱신] 팀 MMR 갱신 실패 - 팀명: {team_name}: {e}", exc_info=True)
                    continue
                    
        except Exception as e:
            logger.error(f"[MMR갱신] 전체 팀 MMR 갱신 실패: {e}", exc_info=True)
    def check_duplicate_with_bot_teams(self, team_name: str, team_members: List[str], exclude_team: str = None) -> Tuple[bool, str]:
        """봇 신청 팀이 이미 봇으로 등록된 팀들과 중복되는지 검사합니다. (대소문자 구별 없이)"""
        try:
            from utils.validators import normalize_nickname_for_comparison
            from utils.validators import normalize_team_name
            
            # 새 팀원들을 정규화된 형태로 변환
            normalized_new_members = [normalize_nickname_for_comparison(member) for member in team_members]
            normalized_new_team_name = normalize_team_name(team_name)
            normalized_exclude = normalize_team_name(exclude_team) if exclude_team else None
            
            for existing_team_name, existing_team in self.teams.items():
                # 제외할 팀이면 스킵 (팀 수정 시 기존 팀과의 중복은 허용)
                if exclude_team and normalize_team_name(existing_team_name) == normalized_exclude:
                    continue
                
                # 팀명 중복 검사 (정규화)
                if normalize_team_name(existing_team_name) == normalized_new_team_name:
                    return False, f"이미 등록된 팀명입니다: {team_name}"
                
                # 팀원 중복 검사
                existing_members = existing_team.all_members
                
                # 기존 팀원들을 정규화된 형태로 변환
                normalized_existing_members = [normalize_nickname_for_comparison(member) for member in existing_members]
                
                # 팀원 중복 검사 (대소문자 구별 없이)
                duplicate_members = set(normalized_new_members) & set(normalized_existing_members)
                if duplicate_members:
                    # 원본 닉네임으로 중복된 멤버 찾기
                    original_duplicates = []
                    for new_member in team_members:
                        if normalize_nickname_for_comparison(new_member) in duplicate_members:
                            original_duplicates.append(new_member)
                    return False, f"이미 등록된 팀원과 중복됩니다: {', '.join(original_duplicates)}"
            
            return True, ""  # 중복 없음
            
        except Exception as e:
            logger.error(f"[팀데이터] 봇 팀 중복 검사 실패: {e}", exc_info=True)
            return True, ""  # 오류 발생 시 중복 없음으로 처리