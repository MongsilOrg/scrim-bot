"""
주간 일정 관리 모듈

관리자들의 다음 주 참가 가능 요일을 수집하고,
Load-Balanced Greedy 알고리즘으로 요일별 관리자를 배정합니다.
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

import pytz

from config.logging_config import get_logger
from utils.helpers import get_current_kst_time

logger = get_logger('schedule_manager')

# 요일 상수
WEEKDAYS = ['월', '화', '수', '목', '금', '토', '일']
WEEKDAY_FULL = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']
ACTIVE_DAYS = [0, 1, 2, 3, 4, 5]  # 월~토 (일요일 제외)
POOL_SIZE = 6  # 요일별 후보 풀 크기

# 일정 관리 대상에서 제외할 사용자 ID
EXCLUDED_USER_IDS: Set[int] = {602522819594551306}

BACKUP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data',
    'schedule_backup.json',
)


class ScheduleManager:
    """주간 일정 관리자"""

    def __init__(self):
        self.week_label: str = ''  # 예: "3/24 ~ 3/30"
        self.week_start: Optional[datetime] = None  # 주 시작일 (월요일)

        # 관리자별 가용 요일: {user_id: set(요일 인덱스)}
        self.availability: Dict[str, Set[int]] = {}
        # 관리자별 불참 사유: {user_id: {day_index: reason} 또는 전체 불참 시 {-1: reason}}
        self.absence_reasons: Dict[str, Dict[int, str]] = {}
        # 관리자 이름 매핑: {user_id: display_name}
        self.admin_names: Dict[str, str] = {}

        # 편성 결과: {day_index: [user_id, ...]}
        self.assignments: Dict[int, List[str]] = {}
        # 실투입 기록: {day_index: [user_id, ...]}
        self.actual_deployments: Dict[int, List[str]] = {}

        # 상태 메시지 참조
        self.status_message_id: Optional[int] = None
        self.status_channel_id: Optional[int] = None

    # ------------------------------------------------------------------
    # 주차 설정
    # ------------------------------------------------------------------

    def initialize_week(self) -> str:
        """다음 주 월~토 기간을 자동으로 설정합니다."""
        now = get_current_kst_time()
        # 다음 주 월요일 계산
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        next_monday = (now + timedelta(days=days_until_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        next_saturday = next_monday + timedelta(days=5)

        self.week_start = next_monday
        self.week_label = (
            f"{next_monday.month}/{next_monday.day} ~ "
            f"{next_saturday.month}/{next_saturday.day}"
        )
        # 초기화
        self.availability.clear()
        self.absence_reasons.clear()
        self.admin_names.clear()
        self.assignments.clear()
        self.actual_deployments.clear()
        self.status_message_id = None
        self.status_channel_id = None

        self._save_backup()
        logger.info(f"[일정] 주간 일정 초기화: {self.week_label}")
        return self.week_label

    # ------------------------------------------------------------------
    # 관리자 응답 등록
    # ------------------------------------------------------------------

    def register_availability(
        self,
        user_id: str,
        display_name: str,
        available_days: Set[int],
    ) -> None:
        """관리자의 가용 요일을 등록합니다."""
        self.admin_names[user_id] = display_name
        self.availability[user_id] = available_days
        # 가용일이 있으면 해당 요일의 불참 사유 제거
        if user_id in self.absence_reasons:
            for day in available_days:
                self.absence_reasons[user_id].pop(day, None)
            # 전체 불참 사유도 제거
            if available_days:
                self.absence_reasons[user_id].pop(-1, None)
            # 빈 dict이면 삭제
            if not self.absence_reasons[user_id]:
                del self.absence_reasons[user_id]
        self._save_backup()
        logger.info(
            f"[일정] {display_name} 가용일 등록: "
            f"{[WEEKDAYS[d] for d in sorted(available_days)]}"
        )

    def register_absence(
        self,
        user_id: str,
        display_name: str,
        reason: str,
        specific_days: Optional[Set[int]] = None,
    ) -> None:
        """불참 사유를 등록합니다.

        specific_days가 None이면 전체 불참(-1 키), 아니면 특정 요일별 사유.
        """
        self.admin_names[user_id] = display_name
        if user_id not in self.absence_reasons:
            self.absence_reasons[user_id] = {}

        if specific_days is None:
            # 전체 불참
            self.absence_reasons[user_id][-1] = reason
            self.availability[user_id] = set()
        else:
            for day in specific_days:
                self.absence_reasons[user_id][day] = reason
                # 해당 요일을 가용일에서 제거
                if user_id in self.availability:
                    self.availability[user_id].discard(day)

        self._save_backup()
        if specific_days is None:
            logger.info(f"[일정] {display_name} 전체 불참: {reason}")
        else:
            days_str = ', '.join(WEEKDAYS[d] for d in sorted(specific_days))
            logger.info(f"[일정] {display_name} 부분 불참({days_str}): {reason}")

    def remove_response(self, user_id: str) -> bool:
        """관리자의 응답을 삭제합니다."""
        removed = False
        if user_id in self.availability:
            del self.availability[user_id]
            removed = True
        if user_id in self.absence_reasons:
            del self.absence_reasons[user_id]
            removed = True
        if removed:
            self._save_backup()
        return removed

    # ------------------------------------------------------------------
    # 현황 조회
    # ------------------------------------------------------------------

    def get_responded_user_ids(self) -> Set[str]:
        """응답한 관리자 ID 집합을 반환합니다."""
        responded = set(self.availability.keys())
        # 전체 불참 사유만 등록한 경우도 응답으로 간주
        for uid, reasons in self.absence_reasons.items():
            if -1 in reasons:
                responded.add(uid)
        return responded

    def get_status_text(self, all_admin_ids: List[Tuple[str, str]]) -> str:
        """현황 텍스트를 생성합니다.

        Args:
            all_admin_ids: [(user_id, display_name), ...] 전체 관리자 목록
        """
        responded = self.get_responded_user_ids()
        total = len(all_admin_ids)
        resp_count = len(responded)

        lines = [f"## 📅 주간 일정 ({self.week_label})"]

        # 응답 현황
        lines.append('')
        lines.append(f'**📋 응답 현황** ({resp_count}/{total}명)')
        if not responded:
            lines.append('> 아직 응답한 관리자가 없습니다.')
        else:
            for uid in sorted(responded):
                name = self.admin_names.get(uid, '알 수 없음')
                avail = self.availability.get(uid, set())
                reasons = self.absence_reasons.get(uid, {})

                if -1 in reasons:
                    lines.append(f'> ❌ {name} — 전체 불참 ({reasons[-1]})')
                elif avail:
                    day_labels = ', '.join(WEEKDAYS[d] for d in sorted(avail))
                    absence_parts = []
                    for d in ACTIVE_DAYS:
                        if d not in avail and d in reasons:
                            absence_parts.append(f"{WEEKDAYS[d]}({reasons[d]})")
                    suffix = ''
                    if absence_parts:
                        suffix = f' | 불참: {", ".join(absence_parts)}'
                    lines.append(f'> ✅ {name} — {day_labels}{suffix}')
                else:
                    lines.append(f'> ❌ {name} — 가용일 없음')

        # 미응답 관리자
        not_responded = [
            (uid, name) for uid, name in all_admin_ids if uid not in responded
        ]
        if not_responded:
            lines.append('')
            lines.append(f'**⏳ 미응답** ({len(not_responded)}명)')
            names = ', '.join(name for _, name in not_responded)
            lines.append(f'> {names}')
        elif total > 0:
            lines.append('')
            lines.append('> ✅ 모든 관리자가 응답 완료')

        # 편성 결과
        if self.assignments:
            lines.append('')
            lines.append('**📊 편성 결과**')
            for day_idx in ACTIVE_DAYS:
                if day_idx not in self.assignments:
                    continue
                members = self.assignments[day_idx]
                if not members:
                    lines.append(f'> **{WEEKDAYS[day_idx]}** — (가용 인원 없음)')
                    continue
                member_names = [
                    self.admin_names.get(uid, '?') for uid in members
                ]
                # 실투입 완료 표시
                if day_idx in self.actual_deployments:
                    deployed = self.actual_deployments[day_idx]
                    lines.append(
                        f'> ✅ **{WEEKDAYS[day_idx]}** — '
                        f'{", ".join(member_names)} '
                        f'(투입 {len(deployed)}명)'
                    )
                else:
                    lines.append(
                        f'> **{WEEKDAYS[day_idx]}** — '
                        f'{", ".join(member_names)} '
                        f'({len(members)}명)'
                    )

        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # Load-Balanced Greedy 편성 알고리즘
    # ------------------------------------------------------------------

    def generate_assignments(self) -> Dict[int, List[str]]:
        """요일별 관리자 배정표를 생성합니다 (Load-Balanced Greedy).

        1. 가용 인원이 적은 요일부터 처리
        2. 배정 횟수가 적은 관리자 우선
        3. 동점 시 가용일이 적은 관리자 우선
        """
        # 요일별 가용 관리자 목록 구축
        day_candidates: Dict[int, List[str]] = defaultdict(list)
        for uid, days in self.availability.items():
            for day in days:
                day_candidates[day].append(uid)

        # 배정 횟수 카운터
        assign_count: Dict[str, int] = defaultdict(int)
        # 관리자별 가용일 수
        avail_count: Dict[str, int] = {
            uid: len(days) for uid, days in self.availability.items()
        }

        assignments: Dict[int, List[str]] = {}

        # 가용 인원이 적은 요일부터 정렬
        sorted_days = sorted(
            day_candidates.keys(),
            key=lambda d: len(day_candidates[d]),
        )

        for day in sorted_days:
            candidates = day_candidates[day]
            if not candidates:
                assignments[day] = []
                continue

            # 후보를 (배정횟수, 가용일수, user_id) 기준으로 정렬
            ranked = sorted(
                candidates,
                key=lambda uid: (
                    assign_count[uid],
                    avail_count.get(uid, 0),
                    uid,  # 안정 정렬용
                ),
            )

            # 최대 POOL_SIZE명 선발
            selected = ranked[:POOL_SIZE]
            assignments[day] = selected

            for uid in selected:
                assign_count[uid] += 1

        self.assignments = assignments
        self._save_backup()
        logger.info("[일정] 편성 완료")
        return assignments

    # ------------------------------------------------------------------
    # 동적 재조정
    # ------------------------------------------------------------------

    def record_actual_deployment(self, day_index: int, deployed_ids: List[str]) -> None:
        """실투입 결과를 기록하고 남은 요일의 편성을 재조정합니다."""
        self.actual_deployments[day_index] = deployed_ids
        self._readjust_remaining(day_index)
        self._save_backup()

    def _readjust_remaining(self, completed_day: int) -> None:
        """완료된 요일 이후의 편성을 투입 횟수 기반으로 재조정합니다."""
        # 실투입 횟수 계산
        deploy_count: Dict[str, int] = defaultdict(int)
        for day_idx, deployed in self.actual_deployments.items():
            for uid in deployed:
                deploy_count[uid] += 1

        # 아직 편성이 안 끝난 요일만 재조정
        remaining_days = sorted(
            d for d in self.assignments if d > completed_day
            and d not in self.actual_deployments
        )

        if not remaining_days:
            return

        # 요일별 가용 관리자 재구축
        day_candidates: Dict[int, List[str]] = defaultdict(list)
        for uid, days in self.availability.items():
            for day in days:
                if day in remaining_days:
                    day_candidates[day].append(uid)

        avail_count: Dict[str, int] = {
            uid: len(days) for uid, days in self.availability.items()
        }

        # 가용 인원 적은 요일부터
        sorted_remaining = sorted(
            remaining_days,
            key=lambda d: len(day_candidates.get(d, [])),
        )

        # 기존 편성 횟수(재조정 대상만) 초기화 후 실투입 기반으로 시작
        assign_count: Dict[str, int] = dict(deploy_count)

        for day in sorted_remaining:
            candidates = day_candidates.get(day, [])
            if not candidates:
                self.assignments[day] = []
                continue

            ranked = sorted(
                candidates,
                key=lambda uid: (
                    assign_count.get(uid, 0),
                    avail_count.get(uid, 0),
                    uid,
                ),
            )
            selected = ranked[:POOL_SIZE]
            self.assignments[day] = selected

            for uid in selected:
                assign_count[uid] = assign_count.get(uid, 0) + 1

    # ------------------------------------------------------------------
    # 백업 / 복구
    # ------------------------------------------------------------------

    def _save_backup(self) -> None:
        """현재 상태를 JSON 파일로 백업합니다."""
        try:
            os.makedirs(os.path.dirname(BACKUP_PATH), exist_ok=True)
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
            with open(BACKUP_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[일정] 백업 저장 실패: {e}", exc_info=True)

    def load_backup(self) -> bool:
        """백업 파일에서 상태를 복구합니다."""
        if not os.path.exists(BACKUP_PATH):
            return False
        try:
            with open(BACKUP_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.week_label = data.get('week_label', '')
            ws = data.get('week_start')
            if ws:
                kst = pytz.timezone('Asia/Seoul')
                self.week_start = datetime.fromisoformat(ws).replace(tzinfo=kst)
            else:
                self.week_start = None

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
            }
            self.status_message_id = data.get('status_message_id')
            self.status_channel_id = data.get('status_channel_id')

            logger.info(f"[일정] 백업 복구 완료: {self.week_label}")
            return True
        except Exception as e:
            logger.error(f"[일정] 백업 복구 실패: {e}", exc_info=True)
            return False

    def clear_backup(self) -> None:
        """백업 파일을 삭제합니다."""
        try:
            if os.path.exists(BACKUP_PATH):
                os.remove(BACKUP_PATH)
        except Exception as e:
            logger.error(f"[일정] 백업 삭제 실패: {e}", exc_info=True)
