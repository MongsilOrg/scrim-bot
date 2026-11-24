import hashlib
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import KST, get_current_kst_time, save_json_atomic
from utils.layout_helpers import format_kr_date

logger = get_logger('schedule_manager')

WEEKDAYS = ['월', '화', '수', '목', '금', '토', '일']
# 토요일과 일요일은 운영진 근무 없이 자율 진행
ACTIVE_DAYS = [0, 1, 2, 3, 4]
# 다음 주 일정은 마지막 근무일 22시에 열리고, 미응답자 알림은 일요일 22시
OPEN_WEEKDAY = ACTIVE_DAYS[-1]
REMINDER_WEEKDAY = 6
REMINDER_HOUR = 22
POOL_SIZE = 6
# 이 시각까지 조를 고르지 않은 대상은 남은 조에 순위대로 넣음
PICK_CLOSE_HOUR = 19
# 확정한 조 목록은 다음 날 다시 확정하지 않을 만큼만 남김
CONFIRMED_KEEP_DAYS = 14

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
        # 다음 주 일정이 열릴 때 지울 미응답 알림
        self.reminder_message_ids: List[int] = []

        # 날짜별 투입자. 순위를 시즌 누적으로 세므로 주가 바뀌어도 지우지 않음
        self.deploy_history: Dict[str, List[str]] = {}
        # 기록으로 확정한 '날짜:조'. 투입 버튼으로 뺀 사람이 다시 들어가지 않게 한 번만 확정
        self.confirmed: List[str] = []
        # 지금 정규 시즌이 시작한 날. 시즌 목록을 받지 못하면 None이고 전부 셈
        self.count_since: Optional[str] = None
        self.pick: Optional[dict] = None

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

        counts = self.season_counts()
        if counts:
            ordered = sorted(all_admin_ids, key=lambda item: (counts.get(item[0], 0), item[1]))
            lines.append('')
            lines.append('**시즌 투입** ' + ', '.join(f'{name} {counts.get(uid, 0)}회' for uid, name in ordered))

        return '\n'.join(lines)

    def _day_date(self, day_index: int) -> Optional[date]:
        return (self.week_start + timedelta(days=day_index)).date() if self.week_start else None

    def season_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = defaultdict(int)
        for day, uids in self.deploy_history.items():
            if self.count_since and day < self.count_since:
                continue
            for uid in uids:
                counts[uid] += 1
        return counts

    def _last_deployed(self) -> Dict[str, str]:
        last: Dict[str, str] = {}
        for day, uids in self.deploy_history.items():
            for uid in uids:
                if day > last.get(uid, ''):
                    last[uid] = day
        return last

    def ranked_for_day(self, day_index: int) -> List[str]:
        """그날 가능하다고 응답한 사람을 투입 순위대로"""
        counts = self.season_counts()
        last = self._last_deployed()
        week = self.week_start.date().isoformat() if self.week_start else ''
        candidates = [uid for uid, days in self.availability.items() if day_index in days]
        return sorted(
            candidates,
            key=lambda uid: (
                counts.get(uid, 0),
                last.get(uid, ''),
                len(self.availability[uid]),
                # ID 순이면 동점일 때 늘 같은 사람이 앞에 와서 주마다 순서를 바꿈
                hashlib.sha1(f"{week}:{uid}".encode()).hexdigest(),
            ),
        )

    def generate_assignments(self) -> Dict[int, List[str]]:
        days = set(ACTIVE_DAYS)
        for available in self.availability.values():
            days |= available
        self.assignments = {day: self.ranked_for_day(day)[:POOL_SIZE] for day in sorted(days)}
        self.save_backup()
        logger.info("[일정] 편성 완료")
        return self.assignments

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
        self._sync_history(day_index)

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

    def _sync_history(self, day_index: int) -> None:
        day = self._day_date(day_index)
        if day is None:
            return
        uids = list(self.actual_deployments.get(day_index, []))
        if uids:
            self.deploy_history[day.isoformat()] = uids
        else:
            self.deploy_history.pop(day.isoformat(), None)

    def confirm_deployment(self, day: date, group_letter: str, user_ids: List[str]) -> bool:
        """기록으로 판정한 그 조의 투입자를 한 번만 반영. 반영했으면 True"""
        key = f"{day.isoformat()}:{group_letter}"
        if key in self.confirmed:
            return False
        oldest = (day - timedelta(days=CONFIRMED_KEEP_DAYS)).isoformat()
        self.confirmed = [k for k in self.confirmed if k >= oldest] + [key]
        history = self.deploy_history.setdefault(day.isoformat(), [])
        history.extend(uid for uid in user_ids if uid not in history)
        if not history:
            del self.deploy_history[day.isoformat()]
        day_index = (day - self.week_start.date()).days if self.week_start else -1
        if 0 <= day_index < 7 and user_ids:
            deployed = self.actual_deployments.setdefault(day_index, [])
            deployed.extend(uid for uid in user_ids if uid not in deployed)
            for uid in user_ids:
                self.admin_names.setdefault(uid, uid)
        self._readjust_remaining()
        self.save_backup()
        return True

    def _readjust_remaining(self) -> List[Tuple[int, List[str], List[str]]]:
        locked = self._locked_days()
        remaining_days = sorted(
            d for d in self.assignments
            if not self.actual_deployments.get(d) and d not in locked
        )
        changes = []
        for day in remaining_days:
            before = self.assignments.get(day, [])
            after = self.ranked_for_day(day)[:POOL_SIZE]
            self.assignments[day] = after
            added = [uid for uid in after if uid not in before]
            removed = [uid for uid in before if uid not in after]
            if added or removed:
                changes.append((day, added, removed))
        return changes

    def open_pick(self, day: date, letters: List[str]) -> dict:
        """그날 조 고르기를 시작. 같은 날 다시 열면 남아 있는 조의 선택을 그대로 둠"""
        day_index = (day - self.week_start.date()).days if self.week_start else -1
        ranked = self.ranked_for_day(day_index)[:POOL_SIZE] if 0 <= day_index < 7 else []
        old = self.pick if self.pick and self.pick.get('date') == day.isoformat() else None
        declined = [uid for uid in (old or {}).get('declined', [])]
        choices = {g: uid for g, uid in (old or {}).get('choices', {}).items() if g in letters}
        ranked = [uid for uid in ranked if uid not in declined]
        # 이미 고른 사람은 순위가 밀려도 대상으로 둠
        chosen = [uid for uid in choices.values() if uid in ranked]
        rest = [uid for uid in ranked if uid not in chosen]
        targets = chosen + rest[:max(0, len(letters) - len(choices))]
        self.pick = {
            'date': day.isoformat(),
            'message_id': None,
            'groups': list(letters),
            'targets': targets,
            'reserves': [uid for uid in rest if uid not in targets],
            'choices': choices,
            'declined': declined,
            'closed': False,
            # 봇이 붙인 조 역할만 적어, 참가자로 받은 역할은 빼지 않음
            'granted': dict((old or {}).get('granted', {})),
            'notice_ids': [],
        }
        self.save_backup()
        return self.pick

    def picked_on(self, day: date) -> Dict[str, Set[int]]:
        if not self.pick or self.pick.get('date') != day.isoformat():
            return {}
        return {letter: {int(uid)} for letter, uid in self.pick['choices'].items()}

    def _open_groups(self) -> List[str]:
        return [g for g in self.pick['groups'] if g not in self.pick['choices']]

    def pick_group(self, user_id: str, letter: str) -> str:
        """picked, cancelled, taken, not_allowed 가운데 하나"""
        pick = self.pick
        if letter not in pick['groups']:
            return 'not_allowed'
        holder = pick['choices'].get(letter)
        if holder == user_id:
            del pick['choices'][letter]
            self.save_backup()
            return 'cancelled'
        if holder:
            return 'taken'
        if user_id not in pick['targets']:
            waiting = [uid for uid in pick['targets'] if uid not in pick['choices'].values()]
            # 대상이 고를 조는 남겨 두고, 그보다 많은 빈 조만 다른 운영진에게 엶
            if len(self._open_groups()) <= len(waiting):
                return 'not_allowed'
        for group, uid in list(pick['choices'].items()):
            if uid == user_id:
                del pick['choices'][group]
        pick['choices'][letter] = user_id
        if user_id in pick['declined']:
            pick['declined'].remove(user_id)
        self.save_backup()
        return 'picked'

    def decline(self, user_id: str) -> dict:
        """status는 declined, reserve_removed, not_target. 대상이 빠지면 예비 1순위를 promoted로 올림"""
        pick = self.pick
        result = {'status': 'not_target', 'promoted': None, 'assigned': None}
        if user_id in pick['reserves']:
            pick['reserves'].remove(user_id)
            pick['declined'].append(user_id)
            result['status'] = 'reserve_removed'
        elif user_id in pick['targets'] or user_id in pick['choices'].values():
            if user_id in pick['targets']:
                pick['targets'].remove(user_id)
            for group, uid in list(pick['choices'].items()):
                if uid == user_id:
                    del pick['choices'][group]
            pick['declined'].append(user_id)
            result['status'] = 'declined'
            waiting = [uid for uid in pick['targets'] if uid not in pick['choices'].values()]
            if pick['reserves'] and len(self._open_groups()) > len(waiting):
                promoted = pick['reserves'].pop(0)
                pick['targets'].append(promoted)
                result['promoted'] = promoted
                if pick['closed']:
                    result['assigned'] = self._open_groups()[0]
                    pick['choices'][result['assigned']] = promoted
        else:
            return result
        self.save_backup()
        return result

    def close_pick(self) -> List[Tuple[str, str]]:
        """마감. 고르지 않은 대상을 남은 조에 순위대로 넣고 넣은 (user_id, 조) 목록을 돌려줌"""
        pick = self.pick
        if not pick or pick['closed']:
            return []
        pick['closed'] = True
        assigned = []
        for uid in pick['targets']:
            open_groups = self._open_groups()
            if not open_groups:
                break
            if uid in pick['choices'].values():
                continue
            pick['choices'][open_groups[0]] = uid
            assigned.append((uid, open_groups[0]))
        self.save_backup()
        return assigned

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
                'reminder_message_ids': self.reminder_message_ids,
                'deploy_history': self.deploy_history,
                'confirmed': self.confirmed,
                'count_since': self.count_since,
                'pick': self.pick,
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
            self.reminder_message_ids = data.get('reminder_message_ids', [])
            self.deploy_history = data.get('deploy_history') or {}
            if 'deploy_history' not in data:
                # 시즌 누적을 두기 전 백업은 이번 주 투입 기록에서 채움
                for day_index in self.actual_deployments:
                    self._sync_history(day_index)
            self.confirmed = data.get('confirmed') or []
            self.count_since = data.get('count_since')
            self.pick = data.get('pick')

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
