"""팀 데이터 관리 모델

스크림 팀 등록, 취소, 인덱스 관리 등의 핵심 CRUD 기능을 담당합니다.
메모리 기반으로 팀 데이터를 관리하며, 백업/조편성/MMR 갱신은 전용 모듈에 위임합니다.
"""
import asyncio
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import discord

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import build_member_lookup, get_current_kst_time
from utils.validators import member_name_keys, normalize_nickname_for_comparison, normalize_team_name

from .team_data import TeamData
from .team_backup import TeamBackup
from .scrim_orchestrator import ScrimOrchestrator
from .mmr_updater import MmrUpdater

logger = get_logger('team_data_manager')

# 로그 액션 타입별 이모지 (신청/수정/취소 + 운영진 강제취소)
ACTION_EMOJI = {"신청": "📝", "취소": "❌", "수정": "✏️", "강제취소": "🔨"}

# 조편성 마감 안내 문구의 단일 출처. 뷰 프리체크와 모델 검증이 같은 문구를 쓴다
ASSIGNMENT_CLOSED_EDIT_MSG = (
    f"{settings.TEAM_REGISTRATION_DEADLINE_HOUR}시 조편성이 완료되어 팀 수정이 불가능합니다."
)
ASSIGNMENT_CLOSED_REGISTER_MSG = (
    f"{settings.TEAM_REGISTRATION_DEADLINE_HOUR}시 조편성이 완료되어 팀 등록이 불가능합니다. "
    "다음 스크림에 신청해주세요."
)


class TeamDataManager:
    """팀 데이터 관리 클래스"""

    BACKUP_FILE = os.getenv('TEAM_BACKUP_PATH', 'data/teams_backup.json')

    def __init__(self, client=None):
        self.client = client
        self._teams_lock = asyncio.Lock()  # 팀 데이터 동시 수정 방지
        self.teams: Dict[str, TeamData] = {}
        self.team_by_member: Dict[str, str] = {}  # 멤버명 -> 팀명 매핑 (O(1) 탐색)
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
        self.is_maintenance: bool = False  # 현재 점검 상태
        self._last_success_time: str = ""  # 마지막 성공 갱신 시각 (HH:MM)
        self._selected_weathers: Dict[str, List[str]] = {}  # 조별 선택된 서브 날씨
        self._mmr_dirty: bool = True  # MMR 메시지 재렌더 필요 여부 (기동 직후 첫 사이클은 무조건 갱신)

        # 위임 모듈 초기화
        self._backup = TeamBackup(self)
        self._orchestrator = ScrimOrchestrator(self)
        self._mmr_updater = MmrUpdater(self)

    # ──────────────────────────────────────────────
    # 백업/복구 위임 (TeamBackup)
    # ──────────────────────────────────────────────

    def save_backup(self) -> None:
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

    async def start_team_assignment(self) -> None:
        await self._orchestrator.start_team_assignment()

    async def execute_auto_assignment(self) -> None:
        await self._orchestrator.execute_auto_assignment()

    async def restore_group_roster_views(self, client) -> None:
        await self._orchestrator.restore_group_roster_views(client)

    # ──────────────────────────────────────────────
    # MMR 갱신 위임 (MmrUpdater)
    # ──────────────────────────────────────────────

    async def update_mmr_message(self, channel: discord.TextChannel, mmr_fail_count: int = 0) -> None:
        await self._mmr_updater.update_mmr_message(channel, mmr_fail_count)

    async def mmr_update_loop(self) -> None:
        await self._mmr_updater.mmr_update_loop()

    async def update_all_team_mmr(self, force: bool = False) -> Tuple[int, int]:
        return await self._mmr_updater.update_all_team_mmr(force=force)

    def mark_mmr_success(self) -> None:
        """마지막 MMR 갱신 성공 시각(HH:MM)을 기록합니다."""
        self._last_success_time = get_current_kst_time().strftime('%H:%M')

    def resolve_mmr_channel(self) -> Optional[discord.abc.Messageable]:
        """MMR 메시지를 게시할 채널을 해석합니다 (기존 메시지 채널 우선, 없으면 스크림 채널)."""
        if self.mmr_message and self.mmr_message.channel:
            return self.mmr_message.channel
        if self.scrim_channel_id and self.client:
            return self.client.get_channel(self.scrim_channel_id)
        return None

    # ──────────────────────────────────────────────
    # 서브 날씨 선택 상태
    # ──────────────────────────────────────────────

    def add_selected_weather(self, group_letter: str, weather: str) -> None:
        self._selected_weathers.setdefault(group_letter, []).append(weather)
        self.save_backup()

    def get_selected_weathers(self, group_letter: str) -> List[str]:
        return self._selected_weathers.get(group_letter, [])

    # ──────────────────────────────────────────────
    # 미검증 팀 마커
    # ──────────────────────────────────────────────

    def mark_unverified(self, team_name: str) -> None:
        """팀을 미검증으로 표시합니다 (마커 + 재렌더 + 백업을 원자로 묶음)."""
        if team_name in self.unverified_teams:
            return
        self.unverified_teams.add(team_name)
        self._mmr_dirty = True
        self.save_backup()

    def clear_unverified(self, team_name: str) -> None:
        """팀의 미검증 마커를 제거합니다 (마커 + 재렌더 + 백업을 원자로 묶음)."""
        if team_name not in self.unverified_teams:
            return
        self.unverified_teams.discard(team_name)
        self._mmr_dirty = True
        self.save_backup()

    # ──────────────────────────────────────────────
    # 인덱스 관리
    # ──────────────────────────────────────────────

    def _update_member_index(self, team_name: str, team: TeamData) -> None:
        self._remove_member_index(team_name, team)
        self._add_member_index(team_name, team)

    def _remove_member_index(self, team_name: str, team: TeamData) -> None:
        for member in team.all_members:
            key = self._normalize_member_key(member)
            if self.team_by_member.get(key) == team_name:
                self.team_by_member.pop(key, None)

    def _add_member_index(self, team_name: str, team: TeamData) -> None:
        for member in team.all_members:
            key = self._normalize_member_key(member)
            self.team_by_member[key] = team_name

    @staticmethod
    def _normalize_member_key(member: str) -> str:
        """멤버 키 정규화 (대소문자/공백 무시)"""
        return normalize_nickname_for_comparison(member)

    def get_team_by_member(self, member_name: str) -> Optional[str]:
        """멤버명으로 팀을 O(1)로 조회합니다."""
        key = self._normalize_member_key(member_name)
        return self.team_by_member.get(key)

    # ──────────────────────────────────────────────
    # 상태 초기화
    # ──────────────────────────────────────────────

    async def reset_team_data(self) -> None:
        try:
            logger.debug("[팀데이터] 초기화 시작")

            await self._cancel_task_and_wait(self.auto_assignment_task, "auto_assignment_task")
            await self._cancel_task_and_wait(self.mmr_update_task, "mmr_update_task")

            # fire-and-forget 태스크 취소
            for task in self._pending_tasks:
                if not task.done():
                    task.cancel()
            self._pending_tasks.clear()

            self.teams.clear()
            self.team_by_member.clear()
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
            self.dashboard_message_id = None
            self.unverified_teams.clear()
            self.is_maintenance = False
            self._last_success_time = ""
            self._selected_weathers.clear()
            self._mmr_dirty = True

            self.clear_backup()
            self.log_state_snapshot(prefix="초기화")
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
        self.save_backup()
        self.log_state_snapshot(prefix="새스크림설정")

    # ──────────────────────────────────────────────
    # 비동기 태스크 관리
    # ──────────────────────────────────────────────

    def spawn_task(self, coro) -> asyncio.Task:
        """fire-and-forget 태스크를 생성하고 리셋 시 취소되도록 추적합니다."""
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    async def _cancel_task_and_wait(self, task: Optional[asyncio.Task], label: str, timeout: float = 10.0) -> None:
        """비동기 태스크를 취소하고 완전히 종료될 때까지 대기합니다."""
        if not task:
            return

        if task.done():
            logger.debug(f"{label}: 태스크가 이미 완료됨")
            return

        try:
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

    def check_team_time_rules(self, current_time: datetime, *, is_edit: bool = False) -> Tuple[bool, str]:
        """조편성 여부와 마감 시각 기준으로 팀 등록/수정 가능 여부를 확인합니다."""
        if self.is_team_assignment_started:
            return False, ASSIGNMENT_CLOSED_EDIT_MSG if is_edit else ASSIGNMENT_CLOSED_REGISTER_MSG

        # 스크림 날짜가 다른 경우 마감 제한 없음
        if not self.is_scrim_date_today(current_time):
            return True, ""

        if current_time.hour < settings.TEAM_REGISTRATION_DEADLINE_HOUR:
            return True, ""

        action = "팀 수정이" if is_edit else "추가 등록이"
        return False, (
            f"⏰ {settings.TEAM_REGISTRATION_DEADLINE_HOUR}:00 이후에는 {action} 불가능합니다.\n"
            f"💡 관리자에게 문의해주세요."
        )

    async def check_member_restrictions(
        self,
        current_time: datetime,
        new_team: Optional[TeamData] = None
    ) -> Tuple[bool, str]:
        """신규 팀과 등록된 전체 팀원의 경고 제한 여부를 확인합니다."""
        warning_manager = BotManager.get_instance().get_warning_manager()
        if not (warning_manager and warning_manager.worksheet):
            return True, ""

        member_names = list(new_team.all_members) if new_team else []
        for team_data in self.teams.values():
            member_names.extend(team_data.all_members)
        member_names = list(dict.fromkeys(member_names))
        if not member_names:
            return True, ""

        # 닉네임을 Discord ID로 해석해 개명 우회 차단
        client = self.client
        guild = client.get_guild(settings.GUILD_ID) if client else None
        member_map = build_member_lookup(guild)

        def _scan_restricted():
            for member in member_names:
                resolved = member_map.get(normalize_nickname_for_comparison(member))
                target_id = str(resolved.id) if resolved else None
                restricted, restricted_until = warning_manager.is_restricted(
                    target_id, member, current_time
                )
                if restricted:
                    return member, restricted_until
            return None

        # is_restricted가 캐시 미스 시 시트를 읽으므로 스캔 전체를 스레드 1회로 넘긴다
        blocked = await asyncio.to_thread(_scan_restricted)
        if blocked:
            member, restricted_until = blocked
            return False, (
                f"⚠️ 팀원 '{member}'이(가) 경고로 인해 스크림 참가가 제한되었습니다.\n"
                f"제한 해제일: {restricted_until}"
            )
        return True, ""

    def _should_check_auto_assign(self) -> bool:
        """자동 조편성 체크가 필요한지 확인합니다."""
        current_time = get_current_kst_time()

        if not self.is_scrim_date_today(current_time):
            return False

        if (self.last_auto_assignment and
            self.last_auto_assignment.date() == current_time.date()):
            return False

        return True

    def is_scrim_date_today(self, current_time: Optional[datetime] = None) -> bool:
        """스크림 설정 날짜가 기준 시각(기본: 현재)과 일치하는지 확인"""
        if not self.scrim_day or not self.scrim_month:
            return False
        if current_time is None:
            current_time = get_current_kst_time()
        return (
            current_time.day == self.scrim_day
            and current_time.month == self.scrim_month
        )

    # ──────────────────────────────────────────────
    # 로깅
    # ──────────────────────────────────────────────

    def log_state_snapshot(self, prefix: str = "상태") -> None:
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
            self.spawn_task(
                self._send_log_to_channel(action_type, user, team_name, current_time, detail)
            )
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

            msg = f"{emoji} <t:{unix_ts}:t> **{team_name}** - {user.mention}"
            if detail:
                msg += f" / {detail}"

            await channel.send(msg)
        except Exception as e:
            logger.error(f"[팀데이터] 로그 채널 전송 실패: {e}", exc_info=True)

    # ──────────────────────────────────────────────
    # 팀 CRUD
    # ──────────────────────────────────────────────

    async def add_team(
        self,
        team_name: str,
        team_data: TeamData,
        user: discord.Member
    ) -> Tuple[bool, str]:
        """팀을 추가합니다.

        Returns:
            (성공 여부, 실패 사유 또는 빈 문자열)
        """
        try:
            team = team_data
            if team.name != team_name:
                team.name = team_name

            # 파이프라인 검증 후 MMR 조회 대기 중 조편성이 시작되는 레이스 방지.
            # 경고 제한 스캔은 파이프라인이 이미 수행했으므로 여기서는 반복하지 않는다
            is_allowed, reason = self.check_team_time_rules(get_current_kst_time())
            if not is_allowed:
                return False, reason

            async with self._teams_lock:
                # 락 내부에서 팀명 중복 재검사 (레이스 컨디션 방지)
                if team_name in self.teams:
                    existing = self.teams[team_name]
                    if existing.user_id != str(user.id):
                        return False, f"'{team_name}' 팀명이 이미 다른 사용자에 의해 등록되었습니다."
                    # 덮어쓰기 전 이전 로스터 인덱스 제거 (빠진 멤버 키 잔존 방지)
                    self._remove_member_index(team_name, existing)

                team.user_id = str(user.id)
                self.teams[team_name] = team

                self._update_member_index(team_name, team)
                self._mmr_dirty = True

            self.save_backup()
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
                if team_name not in self.teams:
                    return False, "등록되지 않은 팀명입니다."

                team = self.teams[team_name]

                self._remove_member_index(team_name, team)

                del self.teams[team_name]
                self._mmr_dirty = True

            self.clear_unverified(team_name)
            self.save_backup()
            return True, ""

        except Exception as e:
            logger.error(f"[팀데이터] 팀 제거 실패: {e}", exc_info=True)
            return False, f"팀 제거 중 오류가 발생했습니다: {str(e)}"

    def find_user_team(self, user_id: str, member: Optional[discord.Member] = None) -> Optional[str]:
        """사용자 ID 또는 이름 키(표시명/전역명/계정명)로 등록한 팀명을 찾습니다."""
        for team_name, team_data in self.teams.items():
            if team_data.user_id == user_id:
                return team_name
        if member is not None:
            for name_key in member_name_keys(member):
                team_name = self.team_by_member.get(name_key)
                if team_name:
                    return team_name
        return None

    def get_team_data(self, team_name: str) -> Optional[TeamData]:
        return self.teams.get(team_name)

    def get_all_teams(self) -> Dict[str, TeamData]:
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
                if team.mmr != mmr:
                    self._mmr_dirty = True
                team.mmr = mmr
                team.mmr_updated_at = get_current_kst_time()


    async def replace_team(self, old_team_name: str, new_team: TeamData, new_mmr: float) -> Tuple[bool, str]:
        """기존 팀을 새 팀으로 교체하며 인덱스와 MMR 정보를 일관되게 갱신합니다.

        Returns:
            (성공 여부, 실패 사유 또는 빈 문자열)
        """
        async with self._teams_lock:
            if old_team_name not in self.teams:
                # 검증과 저장 사이에 팀이 취소된 경우. 여기서 추가하면 취소된 팀이 부활한다
                logger.warning(f"[팀데이터] 교체 대상 팀 없음 - 교체 중단: {old_team_name}")
                return False, f"'{old_team_name}' 팀이 등록되어 있지 않습니다. 이미 취소되었을 수 있습니다."

            old_team = self.teams[old_team_name]
            self._remove_member_index(old_team_name, old_team)
            del self.teams[old_team_name]

            self.teams[new_team.name] = new_team
            new_team.mmr = new_mmr
            self._add_member_index(new_team.name, new_team)
            self._mmr_dirty = True
        self.save_backup()
        return True, ""

    # ──────────────────────────────────────────────
    # 중복 검사
    # ──────────────────────────────────────────────

    def check_duplicate_with_bot_teams(self, team_name: str, team_members: List[str], exclude_team: str = None) -> Tuple[bool, str]:
        """봇 신청 팀이 이미 봇으로 등록된 팀들과 중복되는지 검사합니다. (대소문자 구별 없이)"""
        try:
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

                existing_members = existing_team.all_members

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
