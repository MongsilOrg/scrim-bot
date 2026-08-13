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

from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import KST, get_current_kst_time, save_json_atomic

logger = get_logger('schedule_manager')

# 요일 상수
WEEKDAYS = ['월', '화', '수', '목', '금', '토', '일']
ACTIVE_DAYS = [0, 1, 2, 3, 4, 5]  # 월~토 (일요일 제외)
POOL_SIZE = 6  # 요일별 후보 풀 크기

# 일정 관리 대상에서 제외할 사용자 ID
EXCLUDED_USER_IDS: Set[int] = {settings.TEST_ACCOUNT_CONTACT_ID}

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
        # 초기화 (상태 메시지 참조는 갱신을 위해 유지)
        self.availability.clear()
        self.absence_reasons.clear()
        self.admin_names.clear()
        self.assignments.clear()
        self.actual_deployments.clear()

        self.save_backup()
        return self.week_label

    # ------------------------------------------------------------------
    # 관리자 응답 등록
    # ------------------------------------------------------------------

    def register_schedule(
        self,
        user_id: str,
        display_name: str,
        available_days: Set[int],
        absence_reason: Optional[str] = None,
    ) -> None:
        """참가 요일과 불참 사유를 한 번에 등록합니다.

        available_days가 비어 있으면 전체 불참으로 처리합니다.
        available_days가 있으면 참가 등록합니다 (불참 사유 제거).
        """
        self.admin_names[user_id] = display_name

        if not available_days:
            # 전체 불참
            self.availability[user_id] = set()
            self.absence_reasons[user_id] = {-1: absence_reason or '사유 없음'}
        else:
            # 참가 등록
            self.availability[user_id] = available_days
            self.absence_reasons.pop(user_id, None)

        self.save_backup()

    # ------------------------------------------------------------------
    # 현황 조회
    # ------------------------------------------------------------------

    def get_responded_user_ids(self) -> Set[str]:
        responded = set(self.availability.keys())
        # 불참 사유 등록자도 응답으로 간주
        responded.update(self.absence_reasons.keys())
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
            for uid in sorted(responded, key=lambda u: self.admin_names.get(u, '')):
                name = self.admin_names.get(uid, '알 수 없음')
                avail = self.availability.get(uid, set())
                reasons = self.absence_reasons.get(uid, {})

                if -1 in reasons:
                    lines.append(f'> ❌ {name} - 불참 ({reasons[-1]})')
                elif avail:
                    day_labels = ', '.join(WEEKDAYS[d] for d in sorted(avail))
                    lines.append(f'> ✅ {name} - {day_labels}')
                else:
                    lines.append(f'> ❌ {name} - 가용일 없음')

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

        # 주간 편성
        if self.assignments:
            total_assigned = sum(len(v) for v in self.assignments.values())
            lines.append('')
            lines.append(f'**📊 주간 편성** (총 {total_assigned}건)')
            for day_idx in ACTIVE_DAYS:
                members = self.assignments.get(day_idx, [])
                deployed = self.actual_deployments.get(day_idx, [])

                # 배정자 + 배정 외 투입자를 합산
                all_uids = list(members)
                extra_deployed = [uid for uid in deployed if uid not in members]

                name_parts = []
                for uid in all_uids:
                    name = self.admin_names.get(uid, '?')
                    if uid in deployed:
                        name_parts.append(f'**{name}** ✅')
                    else:
                        name_parts.append(name)
                for uid in extra_deployed:
                    name = self.admin_names.get(uid, '?')
                    name_parts.append(f'**{name}** ✅')

                total_for_day = len(all_uids) + len(extra_deployed)
                if name_parts:
                    lines.append(
                        f'> **{WEEKDAYS[day_idx]}** ({total_for_day}명) - '
                        f'{", ".join(name_parts)}'
                    )
                else:
                    lines.append(f'> **{WEEKDAYS[day_idx]}** (0명) - (배정 없음)')

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

        # 가용 인원이 없는 요일도 포함
        for day in ACTIVE_DAYS:
            if day not in assignments:
                assignments[day] = []

        self.assignments = assignments
        self.save_backup()
        logger.info("[일정] 편성 완료")
        return assignments

    # ------------------------------------------------------------------
    # 동적 재조정
    # ------------------------------------------------------------------

    def toggle_self_deployment(self, day_index: int, user_id: str) -> bool:
        """본인의 투입 상태를 토글하고 남은 요일 편성을 재조정합니다.

        Returns:
            True면 투입 등록, False면 투입 해제
        """
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

        self._readjust_remaining()
        self.save_backup()
        return user_id in self.actual_deployments.get(day_index, [])

    def _readjust_remaining(self) -> None:
        """투입 기록이 없는 요일의 편성을 재조정합니다.

        정렬 기준 (오름차순):
          1. 투입 횟수: 실제 투입이 많을수록 후순위
          2. 배정 횟수: 나머지 요일 배정이 많을수록 후순위
          3. 가용일 수: 가용일이 적을수록 우선 (선택지가 적으니 먼저 배정)
          4. user_id: 안정 정렬
        """
        # 실투입 횟수 계산
        deploy_count: Dict[str, int] = defaultdict(int)
        for day_idx, deployed in self.actual_deployments.items():
            for uid in deployed:
                deploy_count[uid] += 1

        # 실제 투입자가 없는 요일만 재조정 (빈 리스트는 미투입 취급)
        remaining_days = sorted(
            d for d in self.assignments
            if not self.actual_deployments.get(d)
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

        # 배정 횟수는 투입 횟수와 별도로 추적
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

    # ------------------------------------------------------------------
    # 백업 / 복구
    # ------------------------------------------------------------------

    def save_backup(self) -> None:
        """현재 상태를 JSON 파일로 백업합니다."""
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
        """백업 파일에서 상태를 복구합니다."""
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
                    KST.localize(dt) if dt.tzinfo is None else dt.astimezone(KST)
                )
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
                if v  # 빈 리스트 제거
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
