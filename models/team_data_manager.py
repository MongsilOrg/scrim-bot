"""팀 데이터 관리 모델

스크림 팀 등록, 취소, 인덱스 관리 등의 핵심 CRUD 기능을 담당합니다.
메모리 기반으로 팀 데이터를 관리하며, 백업/조편성/MMR 갱신은 전용 모듈에 위임합니다.
"""
import asyncio
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union, TYPE_CHECKING

import discord

from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_current_kst_time
from utils.validators import normalize_nickname_for_comparison

from .team_data import TeamData
from .team_backup import TeamBackup
from .scrim_orchestrator import ScrimOrchestrator
from .mmr_updater import MmrUpdater

if TYPE_CHECKING:  # pragma: no cover
    from bot.manager import BotManager  # type: ignore

logger = get_logger('team_data_manager')

# 로그 액션 타입별 이모지 (신청/수정/취소 + 운영진 강제취소)
ACTION_EMOJI = {"신청": "📝", "취소": "❌", "수정": "✏️", "강제취소": "🔨"}


class TeamDataManager:
    """
    팀 데이터 관리 클래스

    스크림 팀의 등록, 취소, 조회 등 CRUD 기능을 담당합니다.
    백업/복구, 조편성 오케스트레이션, MMR 갱신은 각각 전용 모듈에 위임합니다.
    """

    BACKUP_FILE = os.getenv('TEAM_BACKUP_PATH', 'data/teams_backup.json')

    def __init__(self, client=None):
        self.client = client  # 클라이언트 참조 저장
        self._teams_lock = asyncio.Lock()  # 팀 데이터 동시 수정 방지
        self.teams: Dict[str, TeamData] = {}  # 새로운 TeamData 구조 사용
        self.user_teams: Dict[str, str] = {}  # 사용자 ID -> 팀명 매핑 (O(1) 탐색)
        self.team_by_member: Dict[str, str] = {}  # 멤버명 -> 팀명 매핑 (O(1) 탐색)
        self.team_mmr_index: Dict[float, List[str]] = {}  # MMR -> 팀명 리스트 (정렬용)
        self.scrim_day: Optional[int] = None
        self.scrim_month: Optional[int] = None
        self.auto_assignment_task: Optional[asyncio.Task] = None
        self.mmr_update_task: Optional[asyncio.Task] = None
        self.last_auto_assignment: Optional[datetime] = None
        self.LOG_CHANNEL_ID: int = settings.LOG_CHANNEL_ID
        self.is_team_assignment_started: bool = False
        self.mmr_message: Optional[discord.Message] = None
        self.mmr_message_id: Optional[int] = None  # 백업용 MMR 메시지 ID
        self.scrim_channel_id: Optional[int] = None  # 스크림 명령어가 실행된 채널 ID
        self._pending_tasks: set = set()  # fire-and-forget 태스크 추적
        self.groups: Optional[List[List[Tuple[str, TeamData, float]]]] = None
        self.group_message_ids: Dict[str, int] = {}  # "A" → message_id
        self.group_message_texts: Dict[str, str] = {}  # "A" → message_text
        self.dashboard_message_id: Optional[int] = None  # 스크림 대시보드 메시지 ID
        self.unverified_teams: set = set()  # 점검 중 신청/수정된 팀 (BSER 닉네임 미검증)
        self._is_maintenance: bool = False  # 현재 점검 상태
        self._last_success_time: str = ""  # 마지막 성공 갱신 시각 (HH:MM)

        # 위임 모듈 초기화
        self._backup = TeamBackup(self)
        self._orchestrator = ScrimOrchestrator(self)
        self._mmr_updater = MmrUpdater(self)

    # ──────────────────────────────────────────────
    # 백업/복구 위임 (TeamBackup)
    # ──────────────────────────────────────────────

    def _save_backup(self) -> None:
        self._backup.save()

    def load_backup(self) -> bool:
        return self._backup.load()

    def should_restore_backup(self) -> bool:
        return self._backup.should_restore()

    def clear_backup(self) -> None:
        self._backup.clear()

    # ──────────────────────────────────────────────
    # 조편성 오케스트레이션 위임 (ScrimOrchestrator)
    # ──────────────────────────────────────────────

    async def check_and_auto_assign(self) -> None:
        await self._orchestrator.check_and_auto_assign()

    async def _start_team_assignment(self, total_teams: int, spare_teams: int) -> None:
        await self._orchestrator.start_team_assignment(total_teams, spare_teams)

    async def execute_auto_assignment(self) -> None:
        await self._orchestrator.execute_auto_assignment()

    async def _execute_discord_services(self, client, groups, unmatched_teams):
        await self._orchestrator._execute_discord_services(client, groups, unmatched_teams)

    async def restore_group_roster_views(self, client) -> None:
        await self._orchestrator.restore_group_roster_views(client)

    # ──────────────────────────────────────────────
    # MMR 갱신 위임 (MmrUpdater)
    # ──────────────────────────────────────────────

    async def update_mmr_message(self, channel: discord.TextChannel, mmr_fail_count: int = 0) -> None:
        await self._mmr_updater.update_mmr_message(channel, mmr_fail_count)

    async def mmr_update_loop(self) -> None:
        await self._mmr_updater.mmr_update_loop()

    async def _update_all_team_mmr(self, force: bool = False) -> Tuple[int, int]:
        return await self._mmr_updater.update_all_team_mmr(force=force)

    async def _verify_unverified_teams(self) -> None:
        await self._mmr_updater.verify_unverified_teams()

    async def _send_verification_dm(self, team_name: str, team_data, invalid_members: list) -> None:
        await self._mmr_updater._send_verification_dm(team_name, team_data, invalid_members)

    # ──────────────────────────────────────────────
    # 인덱스 관리
    # ──────────────────────────────────────────────

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

    # ──────────────────────────────────────────────
    # 상태 초기화
    # ──────────────────────────────────────────────

    async def reset_team_data(self) -> None:
        """비동기 태스크를 완전히 종료할 때까지 대기한 후 모든 팀 데이터를 초기화합니다."""
        try:
            logger.debug("[팀데이터] 초기화 시작")

            # 비동기 태스크를 완전히 종료할 때까지 대기
            await self._cancel_task_and_wait(self.auto_assignment_task, "auto_assignment_task")
            await self._cancel_task_and_wait(self.mmr_update_task, "mmr_update_task")

            # fire-and-forget 태스크 취소
            for task in self._pending_tasks:
                if not task.done():
                    task.cancel()
            self._pending_tasks.clear()

            self.teams.clear()
            self.user_teams.clear()
            self.team_by_member.clear()
            self.team_mmr_index.clear()
            self.last_auto_assignment = None

            self.auto_assignment_task = None
            self.mmr_update_task = None

            self.mmr_message = None
            self.mmr_message_id = None
            self.is_team_assignment_started = False
            self.scrim_channel_id = None
            self.scrim_day = None
            self.scrim_month = None
            self.groups = None
            self.group_message_ids = {}
            self.group_message_texts = {}
            self.unverified_teams.clear()
            self._is_maintenance = False
            self._last_success_time = ""

            self.clear_backup()
            self.log_state_snapshot(prefix="reset")
            logger.info("[팀데이터] 초기화 완료")
        except Exception as e:
            logger.error(f"[팀데이터] 초기화 실패: {e}", exc_info=True)

    async def initialize_new_scrim(self, scrim_day: int, scrim_month: int, scrim_channel_id: int) -> None:
        """
        새로운 스크림 날짜/채널을 설정합니다.

        Note: reset_team_data()는 호출자(reset_team_data_manager)가 이미 수행합니다.
        """
        self.scrim_day = scrim_day
        self.scrim_month = scrim_month
        self.scrim_channel_id = scrim_channel_id
        self._save_backup()
        self.log_state_snapshot(prefix="initialize_new_scrim")

    # ──────────────────────────────────────────────
    # 비동기 태스크 관리
    # ──────────────────────────────────────────────

    def _cancel_task(self, task: Optional[asyncio.Task], label: str) -> None:
        """비동기 태스크를 안전하게 취소 (동기 버전 - 레거시 호환용)"""
        try:
            if task and not task.done():
                task.cancel()
        except Exception as exc:
            logger.warning(f"{label} 취소 중 예외 무시: {exc}")

    async def _cancel_task_and_wait(self, task: Optional[asyncio.Task], label: str, timeout: float = 10.0) -> None:
        """비동기 태스크를 취소하고 완전히 종료될 때까지 대기합니다."""
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

    # ──────────────────────────────────────────────
    # 팀 등록/취소/수정 규칙
    # ──────────────────────────────────────────────

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
            return False, "❌ 17시 조편성이 완료되어 팀 등록이 불가능합니다. 다음 스크림에 신청해주세요."

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
                            f"제한 해제일: {restricted_until}"
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
                            f"제한 해제일: {restricted_until}"
                        )

        # 스크림 날짜가 다른 경우 등록 가능 (관리자 오버라이드와 무관)
        if self.scrim_day != current_time.day:
            return True, ""

        if current_time.hour < 17 or allow_admin_override:
            return True, ""

        # 17시 이후 로직
        if current_time.hour >= 17 and not allow_admin_override:
            return False, "⏰ 17:00 이후에는 추가 등록이 불가능합니다.\n💡 관리자에게 문의하세요."

        return True, ""

    async def check_team_cancellation_allowed(self, current_time: datetime) -> Tuple[bool, str]:
        """팀 취소가 가능한지 확인합니다."""
        return True, ""

    async def check_team_edit_allowed(self, current_time: datetime) -> Tuple[bool, str]:
        """팀 수정이 가능한지 확인합니다."""
        # 조편성 시작/완료 이후인지 확인 (가장 우선)
        if self.is_team_assignment_started:
            return False, "❌ 17시 조편성이 완료되어 팀 수정이 불가능합니다."

        # 스크림 날짜가 다른 경우 수정 가능
        if self.scrim_day != current_time.day:
            return True, ""

        if current_time.hour < 17:
            return True, ""

        # 17시 이후 로직
        if current_time.hour >= 17:
            return False, "⏰ 17:00 이후에는 팀 수정이 불가능합니다.\n💡 관리자에게 문의하세요."

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

    # ──────────────────────────────────────────────
    # 로깅
    # ──────────────────────────────────────────────

    def log_state_snapshot(self, prefix: str = "state") -> None:
        """현재 스크림 상태를 로그로 남깁니다."""
        try:
            auto_alive = bool(self.auto_assignment_task and not self.auto_assignment_task.done())
            mmr_alive = bool(self.mmr_update_task and not self.mmr_update_task.done())
            logger.debug(
                f"[{prefix}] teams={len(self.teams)}, scrim_date={self.scrim_month}/{self.scrim_day}, "
                f"scrim_channel={self.scrim_channel_id}, auto_task_alive={auto_alive}, mmr_task_alive={mmr_alive}"
            )
        except Exception as exc:
            logger.warning(f"[팀데이터] 상태 스냅샷 로깅 실패: {exc}")

    def log_action(self, action_type: str, user: discord.Member, team_name: str,
                   *, detail: str = '') -> None:
        """액션 로그를 Discord 채널로 전송합니다."""
        try:
            current_time = get_current_kst_time()
            task = asyncio.create_task(
                self._send_log_to_channel(action_type, user, team_name, current_time, detail)
            )
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
        except Exception as e:
            logger.error(f"[팀데이터] 로그 전송 실패: {e}", exc_info=True)

    async def _send_log_to_channel(
        self,
        action_type: str,
        user: discord.Member,
        team_name: str,
        timestamp: datetime,
        detail: str = '',
    ) -> None:
        """로그 메시지를 지정 채널로 전송합니다."""
        try:
            if not self.client:
                return
            channel = self.client.get_channel(self.LOG_CHANNEL_ID)
            if not channel:
                return

            emoji = ACTION_EMOJI.get(action_type, "📌")
            unix_ts = int(timestamp.timestamp())

            msg = f"{emoji} <t:{unix_ts}:t> **{team_name}** {user.mention}"
            if detail:
                msg += f" | {detail}"

            await channel.send(msg)
        except Exception as e:
            logger.error(f"[팀데이터] 로그 채널 전송 실패: {e}", exc_info=True)

    # ──────────────────────────────────────────────
    # 팀 CRUD
    # ──────────────────────────────────────────────

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

            async with self._teams_lock:
                # 락 내부에서 팀명 중복 재검사 (레이스 컨디션 방지)
                if team_name in self.teams:
                    existing = self.teams[team_name]
                    if hasattr(existing, 'user_id') and existing.user_id != str(user.id):
                        return False, f"'{team_name}' 팀명이 이미 다른 사용자에 의해 등록되었습니다."

                team.user_id = str(user.id)
                self.teams[team_name] = team

                # 인덱스 업데이트
                self._update_member_index(team_name, team)
                self._update_mmr_index(team_name, 0.0, team.mmr)

                # 사용자-팀 매핑 업데이트
                for member in team.all_members:
                    key = self._normalize_member_key(member)
                    self.user_teams[key] = team_name

            self._save_backup()
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
            async with self._teams_lock:
                # 팀 존재 확인
                if team_name not in self.teams:
                    return False, "등록되지 않은 팀명입니다."

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
                self.unverified_teams.discard(team_name)

            self._save_backup()
            return True, ""

        except Exception as e:
            logger.error(f"[팀데이터] 팀 제거 실패: {e}", exc_info=True)
            return False, f"팀 제거 중 오류가 발생했습니다: {str(e)}"

    def find_user_team(self, user_id: str, user_nickname: str = '') -> Optional[str]:
        """사용자 ID 또는 닉네임으로 등록한 팀명을 찾습니다."""
        for team_name, team_data in self.teams.items():
            if hasattr(team_data, 'user_id') and team_data.user_id == user_id:
                return team_name
        if user_nickname:
            return self.get_team_by_member(user_nickname)
        return None

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

    async def set_team_mmr(self, team_name: str, mmr: float) -> None:
        """팀의 평균 MMR을 설정합니다."""
        async with self._teams_lock:
            team = self.teams.get(team_name)
            if team:
                old_mmr = team.mmr
                team.mmr = mmr
                team.mmr_updated_at = get_current_kst_time()
                # MMR 인덱스 업데이트
                self._update_mmr_index(team_name, old_mmr, mmr)


    async def replace_team(self, old_team_name: str, new_team: TeamData, new_mmr: float) -> None:
        """기존 팀을 새 팀으로 교체하며 인덱스와 MMR 정보를 일관되게 갱신합니다."""
        async with self._teams_lock:
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
        self._save_backup()

    def set_scrim_date(self, day: int, month: int) -> None:
        """스크림 날짜를 설정합니다."""
        self.scrim_day = day
        self.scrim_month = month


    def set_scrim_channel(self, channel_id: int) -> None:
        """스크림 명령어가 실행된 채널을 설정합니다."""
        self.scrim_channel_id = channel_id

    # ──────────────────────────────────────────────
    # 중복 검사
    # ──────────────────────────────────────────────

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
                    # 원본 닉네임 + 소속 팀명으로 중복 상세 표시
                    duplicate_details = []
                    for new_member in team_members:
                        if normalize_nickname_for_comparison(new_member) in duplicate_members:
                            duplicate_details.append(f"• {new_member} → {existing_team_name}")
                    detail_str = "\n".join(duplicate_details)
                    return False, f"❌ 이미 등록된 팀원이 있습니다.\n{detail_str}"

            return True, ""  # 중복 없음

        except Exception as e:
            logger.error(f"[팀데이터] 봇 팀 중복 검사 실패: {e}", exc_info=True)
            return True, ""  # 오류 발생 시 중복 없음으로 처리
