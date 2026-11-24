import asyncio
import json
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from config.logging_config import get_logger
from config.settings import settings
from services import labs_ops
from utils.helpers import effective_scrim_date, get_current_kst_time, save_json_atomic
from utils.validators import normalize_nickname_for_comparison

logger = get_logger('warning_manager')

MASTERS_NOT_DEDUCTED = "마스터즈 진행일은 제한 일수에서 차감되지 않습니다."


def _parse_day(value) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or '').strip())
    except ValueError:
        return None


class WarningManager:
    """제재 기록과 기간 계산은 Labs 담당. 봇은 labs_ops가 받아 둔 사본으로 제한 여부만 판정"""

    # Labs의 CAUTIONS_PER_WARNING과 같은 값, 안내 문구에만 사용
    CAUTION_TO_WARNING_COUNT = 2

    MASTERS_STATE_FILE = settings.MASTERS_STATE_PATH
    MASTERS_CHECK_SECONDS = 3600
    MY_SANCTION_HISTORY_LIMIT = 5

    def __init__(self):
        self.masters_task: Optional[asyncio.Task] = None

    @staticmethod
    def _matches_target(record_id: str, record_name: str, target_id: Optional[str], target_name: Optional[str]) -> bool:
        record_id = str(record_id).strip() if record_id else ''
        target_id = str(target_id).strip() if target_id else ''

        if record_id and target_id:
            return record_id == target_id

        if not target_name:
            return False
        return (
            normalize_nickname_for_comparison(str(record_name))
            == normalize_nickname_for_comparison(target_name)
        )

    @staticmethod
    def _restricted_until(row: Dict) -> Optional[date]:
        """해제하지 않은 경고의 제한해제일."""
        if row.get('kind') != '경고' or row.get('released'):
            return None
        return _parse_day(row.get('until'))

    @staticmethod
    def _get_warning_date(current_time: datetime) -> date:
        """마감 시각 이전 경고는 전날 스크림 건."""
        if current_time.hour < settings.TEAM_REGISTRATION_DEADLINE_HOUR:
            return (current_time - timedelta(days=1)).date()
        return current_time.date()

    async def add_warning(
        self,
        target: str,
        target_id: str,
        warning_type: str,
        reason: str,
        admin_display_name: str
    ) -> Tuple[bool, str, Optional[Dict], List[Dict]]:
        """반환: 성공 여부, 메시지, 자동 생성된 경고 정보, 변환된 주의 내역."""
        if warning_type not in ('주의', '경고'):
            return False, "유형은 '주의' 또는 '경고'만 가능합니다.", None, []

        current_time = get_current_kst_time()
        result = await labs_ops.grant(
            target=target,
            target_id=target_id,
            kind=warning_type,
            reason=reason,
            admin=admin_display_name,
            today=current_time.date().isoformat(),
            warning_day=self._get_warning_date(current_time).isoformat(),
        )
        if result is None:
            logger.error(f"[경고관리] 제재 기록 실패 - 대상: {target}, 유형: {warning_type}")
            return False, "제재를 기록하지 못했습니다.\n잠시 후 다시 시도해주세요.", None, []

        warning = result.get('warning') if isinstance(result.get('warning'), dict) else None
        logger.info(
            f"[경고관리] {warning_type} 부여 - 대상: {target}, 관리자: {admin_display_name}"
            + (f", 누적: {warning.get('warning_count')}회, 제한해제: {warning.get('restricted_until')}" if warning else "")
        )

        if warning is None:
            return True, f"{warning_type}가 추가되었습니다.", None, []

        terms = (
            f"누적 {warning.get('warning_count')}회, 제한 {warning.get('duration_days')}일입니다. "
            f"{warning.get('restricted_until')}까지 제한됩니다."
        )
        if warning_type == '경고':
            return True, f"경고가 추가되었습니다. {terms}", warning, []
        return True, (
            f"주의가 추가되었습니다. 주의 {self.CAUTION_TO_WARNING_COUNT}회로 인해 경고 1회가 자동 부여되었습니다. {terms}"
        ), warning, [c for c in result.get('converted') or [] if isinstance(c, dict)]

    def is_restricted(self, target_id: str = None, target_name: str = None, check_date: Optional[datetime] = None) -> Tuple[bool, Optional[str]]:
        """반환: 제한 여부, 제한 해제일. 사본을 한 번도 받지 못했으면 제한하지 않음"""
        rows = labs_ops.sanctions()
        if not rows or not (target_id or target_name):
            return False, None

        latest = max(
            (
                until for row in rows
                if (until := self._restricted_until(row))
                and self._matches_target(row.get('target_id'), row.get('target_name'), target_id, target_name)
            ),
            default=None,
        )
        day = (check_date or get_current_kst_time()).date()
        if latest and day <= latest:
            return True, latest.isoformat()
        return False, None

    async def get_member_sanctions(self, target_id: str) -> Optional[Dict]:
        """대상ID가 같은 행만 집계, ID가 빈 기록은 이름이 같아도 제외. None은 조회 실패."""
        target_id = str(target_id or '').strip()
        rows = labs_ops.sanctions()
        if not target_id or rows is None:
            return None

        mine = [r for r in rows if str(r.get('target_id') or '').strip() == target_id]
        latest = max((until for r in mine if (until := self._restricted_until(r))), default=None)
        if latest and effective_scrim_date(get_current_kst_time()) > latest:
            latest = None

        return {
            'cautions': sum(1 for r in mine if r.get('kind') == '주의' and r.get('merged_into') is None),
            'warnings': sum(1 for r in mine if r.get('kind') == '경고'),
            'restricted_until': latest,
            'history': [
                {
                    'date': str(r.get('day') or ''),
                    'type': str(r.get('kind') or ''),
                    'reason': ' '.join(str(r.get('reason') or '').split()),
                }
                for r in reversed(mine[-self.MY_SANCTION_HISTORY_LIMIT:])
            ],
            'has_records': bool(mine),
        }

    def _load_masters_state(self) -> Optional[date]:
        try:
            with open(self.MASTERS_STATE_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f).get('last_processed', '')
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.error(f"[경고관리] 마스터즈 상태 파일 읽기 실패: {e}", exc_info=True)
            return None
        return _parse_day(raw)

    def _save_masters_state(self, last_processed: date) -> None:
        try:
            save_json_atomic(
                self.MASTERS_STATE_FILE,
                {'last_processed': last_processed.strftime('%Y-%m-%d')},
            )
        except Exception as e:
            logger.error(f"[경고관리] 마스터즈 상태 파일 저장 실패: {e}", exc_info=True)

    async def process_masters_days(self) -> bool:
        """False면 다음 주기에 같은 구간 재시도."""
        today = get_current_kst_time().date()
        last_processed = self._load_masters_state()
        if last_processed is None:
            last_processed = today - timedelta(days=1)
        if last_processed >= today:
            return True

        from services.notion_api import get_masters_dates

        try:
            masters_days = await asyncio.to_thread(
                get_masters_dates, last_processed + timedelta(days=1), today
            )
        except Exception as e:
            logger.error(f"[경고관리] 마스터즈 일정 조회 실패: {e}", exc_info=True)
            return False

        day = last_processed + timedelta(days=1)
        while day <= today:
            if day in masters_days:
                extended = await labs_ops.extend_masters(day.isoformat())
                if extended is None:
                    logger.error(f"[경고관리] 마스터즈 진행일 연장 실패 - 날짜: {day}")
                    return False
                if extended:
                    logger.info(f"[경고관리] 마스터즈 진행일 제한 연장 - 날짜: {day}, {extended}건 1일씩")
            self._save_masters_state(day)
            day += timedelta(days=1)
        return True

    async def masters_loop(self) -> None:
        try:
            while True:
                try:
                    await self.process_masters_days()
                except Exception as e:
                    logger.error(f"[경고관리] 마스터즈 처리 실패: {e}", exc_info=True)
                await asyncio.sleep(self.MASTERS_CHECK_SECONDS)
        except asyncio.CancelledError:
            pass

    def start_masters_task(self) -> None:
        if self.masters_task and not self.masters_task.done():
            return

        self.masters_task = asyncio.create_task(self.masters_loop())
