import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import KST, get_current_kst_time, save_json_atomic
from utils.layout_helpers import format_kr_date

logger = get_logger('schedule_manager')

WEEKDAYS = ['월', '화', '수', '목', '금', '토', '일']
# 토요일과 일요일은 운영진 근무 없이 자율 진행
ACTIVE_DAYS = [0, 1, 2, 3, 4]
# 다음 주 일정이 토요일 22시에 열리고, 미응답자 알림은 일요일 21시
REMINDER_WEEKDAY = 6
REMINDER_HOUR = 21
POOL_SIZE = 6

EXCLUDED_USER_IDS: Set[int] = {settings.TEST_ACCOUNT_CONTACT_ID}

BACKUP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data',
    'schedule_backup.json',
)


def _week_label(monday: datetime) -> str:
    return f"{format_kr_date(monday)}부터 {format_kr_date(monday + timedelta(days=ACTIVE_DAYS[-1]))}까지"


class ScheduleManager:

    def __init__(self):
        self.week_label: str = ''
        self.week_start: Optional[datetime] = None

        self.availability: Dict[str, Set[int]] = {}
        # 전체 불참은 요일 인덱스 대신 키 -1
        self.absence_reasons: Dict[str, Dict[int, str]] = {}
        self.admin_names: Dict[str, str] = {}

        self.assignments: Dict[int, List[str]] = {}
        self.actual_deployments: Dict[int, List[str]] = {}

        self.status_message_id: Optional[int] = None
        self.status_channel_id: Optional[int] = None

    def initialize_week(self) -> str:
        now = get_current_kst_time()
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        next_monday = (now + timedelta(days=days_until_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.week_start = next_monday
        self.week_label = _week_label(next_monday)
        # 상태 메시지 참조는 새 주차 현황 갱신에 재사용
        self.availability.clear()
        self.absence_reasons.clear()
        self.admin_names.clear()
        self.assignments.clear()
        self.actual_deployments.clear()

        self.save_backup()
        return self.week_label

    def register_schedule(
        self,
        user_id: str,
        display_name: str,
        available_days: Set[int],
        absence_reason: Optional[str] = None,
    ) -> None:
        self.admin_names[user_id] = display_name

        if not available_days:
            self.availability[user_id] = set()
            self.absence_reasons[user_id] = {-1: absence_reason or '사유 없음'}
        else:
            self.availability[user_id] = available_days
            self.absence_reasons.pop(user_id, None)

        self.save_backup()
        answer = ', '.join(WEEKDAYS[d] for d in sorted(available_days)) if available_days else '불참'
        logger.info(f"[일정] 응답 - {display_name}: {answer}, 주차: {self.week_label}")

    def get_responded_user_ids(self) -> Set[str]:
        responded = set(self.availability.keys())
        responded.update(self.absence_reasons.keys())
        return responded

    def get_status_text(self, all_admin_ids: List[Tuple[str, str]]) -> str:
        """all_admin_ids 항목은 user_id, display_name 쌍."""
        responded = self.get_responded_user_ids()
        total = len(all_admin_ids)
        resp_count = len(responded)

        lines = [f"## 📅 주간 일정\n{self.week_label}"]

        lines.append('')
        lines.append(f'**응답 현황** {total}명 중 {resp_count}명 응답')
        if not responded:
            lines.append('> 아직 응답한 관리자가 없습니다.')
        else:
            for uid in sorted(responded, key=lambda u: self.admin_names.get(u, '')):
                name = self.admin_names.get(uid, '알 수 없음')
                avail = self.availability.get(uid, set())
                reasons = self.absence_reasons.get(uid, {})

                if -1 in reasons:
                    lines.append(f'> {name}: 불참, 사유 {reasons[-1]}')
                elif avail:
                    day_labels = ', '.join(WEEKDAYS[d] for d in sorted(avail))
                    lines.append(f'> {name}: {day_labels}')
                else:
                    lines.append(f'> {name}: 가능한 요일 없음')

        not_responded = [
            (uid, name) for uid, name in all_admin_ids if uid not in responded
        ]
        if not_responded:
            lines.append('')
            lines.append(f'**미응답** {len(not_responded)}명')
            names = ', '.join(name for _, name in not_responded)
            lines.append(f'> {names}')
        elif total > 0:
            lines.append('')
            lines.append('> 모든 관리자가 응답했습니다.')
        if not_responded and not self.assignments:
            lines.append(f'-# 응답하지 않은 관리자는 {WEEKDAYS[REMINDER_WEEKDAY]}요일 {REMINDER_HOUR}시에 알림을 받습니다.')

        if self.assignments:
            total_assigned = sum(len(v) for v in self.assignments.values())
            lines.append('')
            lines.append(f'**주간 편성** {total_assigned}건')
            for day_idx in ACTIVE_DAYS:
                members = self.assignments.get(day_idx, [])
                deployed = self.actual_deployments.get(day_idx, [])

                all_uids = list(members)
                extra_deployed = [uid for uid in deployed if uid not in members]

                name_parts = []
                for uid in all_uids:
                    name = self.admin_names.get(uid, '알 수 없음')
                    if uid in deployed:
                        name_parts.append(f'**{name}** 투입')
                    else:
                        name_parts.append(name)
                for uid in extra_deployed:
                    name = self.admin_names.get(uid, '알 수 없음')
                    name_parts.append(f'**{name}** 투입')

                total_for_day = len(all_uids) + len(extra_deployed)
                if name_parts:
                    lines.append(
                        f'> **{WEEKDAYS[day_idx]}** {total_for_day}명: '
                        f'{", ".join(name_parts)}'
                    )
                else:
                    lines.append(f'> **{WEEKDAYS[day_idx]}** 배정 없음')

        return '\n'.join(lines)

    def generate_assignments(self) -> Dict[int, List[str]]:
        day_candidates: Dict[int, List[str]] = defaultdict(list)
        for uid, days in self.availability.items():
            for day in days:
                day_candidates[day].append(uid)

        assign_count: Dict[str, int] = defaultdict(int)
        avail_count: Dict[str, int] = {
            uid: len(days) for uid, days in self.availability.items()
        }

        assignments: Dict[int, List[str]] = {}

        sorted_days = sorted(
            day_candidates.keys(),
            key=lambda d: len(day_candidates[d]),
        )

        for day in sorted_days:
            candidates = day_candidates[day]
            if not candidates:
                assignments[day] = []
                continue

            ranked = sorted(
                candidates,
                key=lambda uid: (
                    assign_count[uid],
                    avail_count.get(uid, 0),
                    uid,
                ),
            )

            selected = ranked[:POOL_SIZE]
            assignments[day] = selected

            for uid in selected:
                assign_count[uid] += 1

        for day in ACTIVE_DAYS:
            if day not in assignments:
                assignments[day] = []

        self.assignments = assignments
        self.save_backup()
        logger.info("[일정] 편성 완료")
        return assignments

    def toggle_self_deployment(self, day_index: int, user_id: str) -> Tuple[bool, List[Tuple[int, List[str], List[str]]]]:
        """반환은 등록 여부와 재배정으로 바뀐 요일별 추가, 제외 목록."""
        if day_index not in self.actual_deployments:
            self.actual_deployments[day_index] = []

        deployed = self.actual_deployments[day_index]
        if user_id in deployed:
            deployed.remove(user_id)
            if not deployed:
                del self.actual_deployments[day_index]
        else:
            deployed.append(user_id)
            self.admin_names.setdefault(user_id, user_id)

        changes = self._readjust_remaining()
        self.save_backup()
        return user_id in self.actual_deployments.get(day_index, []), changes

    def _locked_days(self) -> Set[int]:
        # 지난 요일과 시작 시각이 지난 오늘은 이미 진행되어 다시 배정하지 않음
        if self.week_start is None:
            return set()
        now = get_current_kst_time()
        today = now.date()
        locked = set()
        for day in ACTIVE_DAYS:
            day_date = (self.week_start + timedelta(days=day)).date()
            if day_date < today or (day_date == today and now.hour >= settings.SCRIM_START_HOUR):
                locked.add(day)
        return locked

    def _readjust_remaining(self) -> List[Tuple[int, List[str], List[str]]]:
        deploy_count: Dict[str, int] = defaultdict(int)
        for day_idx, deployed in self.actual_deployments.items():
            for uid in deployed:
                deploy_count[uid] += 1

        locked = self._locked_days()
        remaining_days = sorted(
            d for d in self.assignments
            if not self.actual_deployments.get(d) and d not in locked
        )

        if not remaining_days:
            return []

        before = {d: list(self.assignments.get(d, [])) for d in remaining_days}

        day_candidates: Dict[int, List[str]] = defaultdict(list)
        for uid, days in self.availability.items():
            for day in days:
                if day in remaining_days:
                    day_candidates[day].append(uid)

        avail_count: Dict[str, int] = {
            uid: len(days) for uid, days in self.availability.items()
        }

        sorted_remaining = sorted(
            remaining_days,
            key=lambda d: len(day_candidates.get(d, [])),
        )

        assign_count: Dict[str, int] = defaultdict(int)

        for day in sorted_remaining:
            candidates = day_candidates.get(day, [])
            if not candidates:
                self.assignments[day] = []
                continue

            ranked = sorted(
                candidates,
                key=lambda uid: (
                    deploy_count.get(uid, 0),
                    assign_count.get(uid, 0),
                    avail_count.get(uid, 0),
                    uid,
                ),
            )
            selected = ranked[:POOL_SIZE]
            self.assignments[day] = selected

            for uid in selected:
                assign_count[uid] += 1

        changes = []
        for day in remaining_days:
            after = self.assignments.get(day, [])
            added = [uid for uid in after if uid not in before[day]]
            removed = [uid for uid in before[day] if uid not in after]
            if added or removed:
                changes.append((day, added, removed))
        return changes

    def save_backup(self) -> None:
        try:
            data = {
                'week_label': self.week_label,
                'week_start': self.week_start.isoformat() if self.week_start else None,
                'availability': {
                    uid: sorted(days) for uid, days in self.availability.items()
                },
                'absence_reasons': {
                    uid: {str(k): v for k, v in reasons.items()}
                    for uid, reasons in self.absence_reasons.items()
                },
                'admin_names': self.admin_names,
                'assignments': {
                    str(k): v for k, v in self.assignments.items()
                },
                'actual_deployments': {
                    str(k): v for k, v in self.actual_deployments.items()
                },
                'status_message_id': self.status_message_id,
                'status_channel_id': self.status_channel_id,
            }
            save_json_atomic(BACKUP_PATH, data, indent=2)
        except Exception as e:
            logger.error(f"[일정] 백업 저장 실패: {e}", exc_info=True)

    def load_backup(self) -> bool:
        if not os.path.exists(BACKUP_PATH):
            return False
        try:
            with open(BACKUP_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.week_label = data.get('week_label', '')
            ws = data.get('week_start')
            if ws:
                dt = datetime.fromisoformat(ws)
                self.week_start = (
                    dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)
                )
            else:
                self.week_start = None
            if self.week_start:
                self.week_label = _week_label(self.week_start)

            self.availability = {
                uid: set(days)
                for uid, days in data.get('availability', {}).items()
            }
            self.absence_reasons = {
                uid: {int(k): v for k, v in reasons.items()}
                for uid, reasons in data.get('absence_reasons', {}).items()
            }
            self.admin_names = data.get('admin_names', {})
            self.assignments = {
                int(k): v for k, v in data.get('assignments', {}).items()
            }
            self.actual_deployments = {
                int(k): v
                for k, v in data.get('actual_deployments', {}).items()
                if v
            }
            self.status_message_id = data.get('status_message_id')
            self.status_channel_id = data.get('status_channel_id')

            logger.info(f"[일정] 백업 복구 완료: {self.week_label}")
            return True
        except Exception as e:
            logger.error(f"[일정] 백업 복구 실패: {e}", exc_info=True)
            return False

    def clear_backup(self) -> None:
        try:
            if os.path.exists(BACKUP_PATH):
                os.remove(BACKUP_PATH)
        except Exception as e:
            logger.error(f"[일정] 백업 삭제 실패: {e}", exc_info=True)
