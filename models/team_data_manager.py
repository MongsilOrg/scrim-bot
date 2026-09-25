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
from .warning_manager import MASTERS_NOT_DEDUCTED
from .team_backup import TeamBackup
from .scrim_orchestrator import ScrimOrchestrator
from .mmr_updater import MmrUpdater

logger = get_logger('team_data_manager')

ACTION_EMOJI = {"신청": "📝", "취소": "❌", "수정": "✏️", "강제취소": "🔨"}

ASSIGNMENT_CLOSED_EDIT_MSG = (
    f"{settings.TEAM_REGISTRATION_DEADLINE_HOUR}시 조편성이 완료되어 팀 수정이 불가능합니다."
)
ASSIGNMENT_CLOSED_REGISTER_MSG = (
    f"{settings.TEAM_REGISTRATION_DEADLINE_HOUR}시 조편성이 완료되어 팀 등록이 불가능합니다. "
    "다음 스크림에 신청해주세요."
)


class TeamDataManager:
    BACKUP_FILE = os.getenv('TEAM_BACKUP_PATH', 'data/teams_backup.json')

    def __init__(self, client=None):
        self.client = client
        self._teams_lock = asyncio.Lock()
        self.teams: Dict[str, TeamData] = {}
        self.team_by_member: Dict[str, str] = {}
        self.scrim_day: Optional[int] = None
        self.scrim_month: Optional[int] = None
        self.auto_assignment_task: Optional[asyncio.Task] = None
        self.mmr_update_task: Optional[asyncio.Task] = None
        self.last_auto_assignment: Optional[datetime] = None
        self.LOG_CHANNEL_ID: int = settings.LOG_CHANNEL_ID
        self.is_team_assignment_started: bool = False
        self.mmr_message: Optional[discord.Message] = None
        self.mmr_message_id: Optional[int] = None
        self.scrim_channel_id: Optional[int] = None
        self._pending_tasks: set = set()
        self.groups: Optional[List[List[Tuple[str, TeamData, float]]]] = None
        self.group_message_ids: Dict[str, int] = {}
        self.group_message_texts: Dict[str, str] = {}
        self.dashboard_message_id: Optional[int] = None
        self.unverified_teams: set = set()
        self.is_maintenance: bool = False
        self._last_success_time: str = ""
        self._selected_weathers: Dict[str, List[str]] = {}
        self._mmr_dirty: bool = True

        self._backup = TeamBackup(self)
        self._orchestrator = ScrimOrchestrator(self)
        self._mmr_updater = MmrUpdater(self)

    def save_backup(self) -> None:
        self._backup.save()

    def load_backup(self) -> bool:
        return self._backup.load()

    def should_restore_backup(self) -> bool:
        return self._backup.should_restore()

    def clear_backup(self) -> None:
        self._backup.clear()

    async def check_and_auto_assign(self) -> None:
        await self._orchestrator.check_and_auto_assign()

    async def start_team_assignment(self) -> None:
        await self._orchestrator.start_team_assignment()

    async def execute_auto_assignment(self) -> None:
        await self._orchestrator.execute_auto_assignment()

    async def restore_group_roster_views(self, client) -> None:
        await self._orchestrator.restore_group_roster_views(client)

    async def update_mmr_message(self, channel: discord.TextChannel, mmr_fail_count: int = 0) -> None:
        await self._mmr_updater.update_mmr_message(channel, mmr_fail_count)

    async def mmr_update_loop(self) -> None:
        await self._mmr_updater.mmr_update_loop()

    async def update_all_team_mmr(self, force: bool = False) -> Tuple[int, int]:
        return await self._mmr_updater.update_all_team_mmr(force=force)

    def mark_mmr_success(self) -> None:
        self._last_success_time = get_current_kst_time().strftime('%H:%M')

    def resolve_mmr_channel(self) -> Optional[discord.abc.Messageable]:
        if self.mmr_message and self.mmr_message.channel:
            return self.mmr_message.channel
        if self.scrim_channel_id and self.client:
            return self.client.get_channel(self.scrim_channel_id)
        return None

    def add_selected_weather(self, group_letter: str, weather: str) -> None:
        self._selected_weathers.setdefault(group_letter, []).append(weather)
        self.save_backup()

    def get_selected_weathers(self, group_letter: str) -> List[str]:
        return self._selected_weathers.get(group_letter, [])

    def mark_unverified(self, team_name: str) -> None:
        if team_name in self.unverified_teams:
            return
        self.unverified_teams.add(team_name)
        self._mmr_dirty = True
        self.save_backup()

    def clear_unverified(self, team_name: str) -> None:
        if team_name not in self.unverified_teams:
            return
        self.unverified_teams.discard(team_name)
        self._mmr_dirty = True
        self.save_backup()

    def _update_member_index(self, team_name: str, team: TeamData) -> None:
        self._remove_member_index(team_name, team)
        self._add_member_index(team_name, team)

    def _remove_member_index(self, team_name: str, team: TeamData) -> None:
        for member in team.all_members:
            key = normalize_nickname_for_comparison(member)
            if self.team_by_member.get(key) == team_name:
                self.team_by_member.pop(key, None)

    def _add_member_index(self, team_name: str, team: TeamData) -> None:
        for member in team.all_members:
            key = normalize_nickname_for_comparison(member)
            self.team_by_member[key] = team_name

    def get_team_by_member(self, member_name: str) -> Optional[str]:
        key = normalize_nickname_for_comparison(member_name)
        return self.team_by_member.get(key)

    async def reset_team_data(self) -> None:
        try:
            logger.debug("[팀데이터] 초기화 시작")

            await self._cancel_task_and_wait(self.auto_assignment_task, "auto_assignment_task")
            await self._cancel_task_and_wait(self.mmr_update_task, "mmr_update_task")

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
        self.scrim_day = scrim_day
        self.scrim_month = scrim_month
        self.scrim_channel_id = scrim_channel_id
        self.save_backup()
        self.log_state_snapshot(prefix="새스크림설정")

    def start_background_tasks(self) -> None:
        if not (self.auto_assignment_task and not self.auto_assignment_task.done()):
            self.auto_assignment_task = asyncio.create_task(self.check_and_auto_assign())
        if not (self.mmr_update_task and not self.mmr_update_task.done()):
            self.mmr_update_task = asyncio.create_task(self.mmr_update_loop())

    def spawn_task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._pending_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("[팀데이터] 백그라운드 태스크 실패", exc_info=task.exception())

    async def _cancel_task_and_wait(self, task: Optional[asyncio.Task], label: str, timeout: float = 10.0) -> None:
        if not task:
            return

        if task.done():
            logger.debug(f"{label}: 태스크가 이미 완료됨")
            return

        try:
            task.cancel()
            logger.info(f"{label}: 태스크 취소 요청됨, 종료 대기 중...")

            try:
                await asyncio.wait_for(task, timeout=timeout)
            except asyncio.CancelledError:
                logger.info(f"{label}: 태스크 취소 완료")
            except asyncio.TimeoutError:
                logger.warning(f"{label}: 태스크 취소 타임아웃 ({timeout}초), 강제 종료 시도")
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=2.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
            except Exception as e:
                logger.warning(f"{label}: 태스크 종료 중 예외 발생: {e}")

            if not task.done():
                logger.warning(f"{label}: 태스크가 완전히 종료되지 않았지만 계속 진행합니다.")
        except Exception as exc:
            logger.warning(f"{label}: 태스크 취소 중 예외 무시: {exc}")

    def check_team_time_rules(self, current_time: datetime, *, is_edit: bool = False) -> Tuple[bool, str]:
        if self.is_team_assignment_started:
            return False, ASSIGNMENT_CLOSED_EDIT_MSG if is_edit else ASSIGNMENT_CLOSED_REGISTER_MSG

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
        new_team: Optional[TeamData] = None,
        previous_members: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        warning_manager = BotManager.get_instance().get_warning_manager()
        if not (warning_manager and warning_manager.worksheet):
            return True, ""

        member_names = list(new_team.all_members) if new_team else []
        if previous_members is not None:
            known = {normalize_nickname_for_comparison(name) for name in previous_members}
            member_names = [
                name for name in member_names
                if normalize_nickname_for_comparison(name) not in known
            ]
        member_names = list(dict.fromkeys(member_names))
        if not member_names:
            return True, ""

        # 닉네임만으로 검사하면 개명으로 우회 가능
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

        # is_restricted는 캐시 미스 때 시트를 동기 조회
        blocked = await asyncio.to_thread(_scan_restricted)
        if blocked:
            member, restricted_until = blocked
            return False, (
                f"⚠️ 팀원 '{member}'이(가) 경고로 인해 스크림 참가가 제한되었습니다.\n"
                f"{restricted_until}까지 참여가 제한됩니다.\n"
                f"💡 {MASTERS_NOT_DEDUCTED}"
            )
        return True, ""

    def _should_check_auto_assign(self) -> bool:
        current_time = get_current_kst_time()

        if not self.is_scrim_date_today(current_time):
            return False

        if (self.last_auto_assignment and
            self.last_auto_assignment.date() == current_time.date()):
            return False

        return True

    def is_scrim_date_today(self, current_time: Optional[datetime] = None) -> bool:
        if not self.scrim_day or not self.scrim_month:
            return False
        if current_time is None:
            current_time = get_current_kst_time()
        return (
            current_time.day == self.scrim_day
            and current_time.month == self.scrim_month
        )

    def log_state_snapshot(self, prefix: str = "상태") -> None:
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

    async def add_team(
        self,
        team_name: str,
        team_data: TeamData,
        user: discord.Member
    ) -> Tuple[bool, str]:
        """반환: 성공 여부, 실패 사유 또는 빈 문자열."""
        try:
            team = team_data
            if team.name != team_name:
                team.name = team_name

            # 파이프라인 검증 뒤 MMR 조회 대기 중 조편성 시작 레이스
            is_allowed, reason = self.check_team_time_rules(get_current_kst_time())
            if not is_allowed:
                return False, reason

            async with self._teams_lock:
                if team_name in self.teams:
                    existing = self.teams[team_name]
                    if existing.user_id != str(user.id):
                        return False, f"'{team_name}' 팀명이 이미 다른 사용자에 의해 등록되었습니다."
                    # 새 로스터 기준 갱신만으로는 빠진 멤버 키 잔존
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
        """반환: 성공 여부, 실패 사유 또는 빈 문자열."""
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
        team = self.teams.get(team_name)
        return team.mmr if team else None

    async def set_team_mmr(self, team_name: str, mmr: float) -> None:
        async with self._teams_lock:
            team = self.teams.get(team_name)
            if team:
                if team.mmr != mmr:
                    self._mmr_dirty = True
                team.mmr = mmr
                team.mmr_updated_at = get_current_kst_time()


    async def replace_team(self, old_team_name: str, new_team: TeamData, new_mmr: float) -> Tuple[bool, str]:
        """반환: 성공 여부, 실패 사유."""
        async with self._teams_lock:
            if old_team_name not in self.teams:
                # 검증과 저장 사이에 취소된 팀, 여기서 추가하면 부활
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

    def check_duplicate_with_bot_teams(self, team_name: str, team_members: List[str], exclude_team: str = None) -> Tuple[bool, str]:
        try:
            normalized_new_members = [normalize_nickname_for_comparison(member) for member in team_members]
            normalized_new_team_name = normalize_team_name(team_name)
            normalized_exclude = normalize_team_name(exclude_team) if exclude_team else None

            for existing_team_name, existing_team in self.teams.items():
                if exclude_team and normalize_team_name(existing_team_name) == normalized_exclude:
                    continue

                if normalize_team_name(existing_team_name) == normalized_new_team_name:
                    return False, f"이미 등록된 팀명입니다: {team_name}"

                existing_members = existing_team.all_members

                normalized_existing_members = [normalize_nickname_for_comparison(member) for member in existing_members]

                duplicate_members = set(normalized_new_members) & set(normalized_existing_members)
                if duplicate_members:
                    duplicate_details = []
                    for new_member in team_members:
                        if normalize_nickname_for_comparison(new_member) in duplicate_members:
                            duplicate_details.append(f"- {new_member}: {existing_team_name} 팀")
                    detail_str = "\n".join(duplicate_details)
                    return False, f"❌ 이미 등록된 팀원이 있습니다.\n{detail_str}"

            return True, ""

        except Exception as e:
            logger.error(f"[팀데이터] 봇 팀 중복 검사 실패: {e}", exc_info=True)
            return True, ""
