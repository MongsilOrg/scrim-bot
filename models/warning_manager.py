import asyncio
import json
from datetime import date, datetime, timedelta
from typing import Dict, Iterator, List, Optional, Tuple

import gspread
from gspread.utils import rowcol_to_a1

from config.logging_config import get_logger
from config.settings import settings
from utils.gsheet_client import create_gspread_client
from utils.helpers import KST, get_current_kst_time, save_json_atomic
from utils.validators import normalize_nickname_for_comparison

logger = get_logger('warning_manager')

MASTERS_NOT_DEDUCTED = "마스터즈 진행일은 제한 일수에서 차감되지 않습니다."


class WarningManager:

    # 활성 경고만 남는 내부용 패널티 시트
    PENALTY_HEADERS = ['날짜', '대상', '대상ID', '유형', '사유', '경고일', '제한해제일', '관리자ID', '비고']
    COL_RESTRICTED_UNTIL = PENALTY_HEADERS.index('제한해제일')

    # 영구 보관하는 외부용 경고로그 시트
    LOG_HEADERS = ['대상', '날짜', '제한해제일', '사유', '유형', '대상ID']

    RESTRICTION_DAYS = {1: 3, 2: 7}
    RESTRICTION_DAYS_MAX = 14

    CAUTION_TO_WARNING_COUNT = 2

    # 제한해제일 당일 이 시각 이후 행 삭제
    CLEANUP_HOUR = 18

    MASTERS_STATE_FILE = settings.MASTERS_STATE_PATH

    def __init__(self):
        self.client: Optional[gspread.Client] = None
        self.spreadsheet: Optional[gspread.Spreadsheet] = None
        self.worksheet: Optional[gspread.Worksheet] = None
        self.warning_log_worksheet: Optional[gspread.Worksheet] = None
        self.cleanup_task: Optional[asyncio.Task] = None
        self._warnings_cache: Optional[List[Dict]] = None
        self._cache_timestamp: Optional[datetime] = None
        self._cache_ttl: int = 300
        self._initialize_client()
    
    def _initialize_client(self) -> None:
        self.client, self.spreadsheet = create_gspread_client(caller='경고관리')

        if not self.spreadsheet:
            return

        try:
            try:
                self.worksheet = self.spreadsheet.worksheet(
                    settings.GOOGLE_SHEETS_WARNING_WORKSHEET_NAME
                )
                logger.debug(f"[경고관리] 패널티 시트 연결 성공 - 이름: {settings.GOOGLE_SHEETS_WARNING_WORKSHEET_NAME}")
            except gspread.WorksheetNotFound:
                self.worksheet = self.spreadsheet.add_worksheet(
                    title=settings.GOOGLE_SHEETS_WARNING_WORKSHEET_NAME,
                    rows=100,
                    cols=10
                )
                logger.info(f"[경고관리] 패널티 시트 생성됨 - 이름: {settings.GOOGLE_SHEETS_WARNING_WORKSHEET_NAME}")

            try:
                self.warning_log_worksheet = self.spreadsheet.worksheet(
                    settings.GOOGLE_SHEETS_WARNING_LOG_WORKSHEET_NAME
                )
                logger.debug(f"[경고관리] 패널티로그 시트 연결 성공 - 이름: {settings.GOOGLE_SHEETS_WARNING_LOG_WORKSHEET_NAME}")
            except gspread.WorksheetNotFound:
                self.warning_log_worksheet = self.spreadsheet.add_worksheet(
                    title=settings.GOOGLE_SHEETS_WARNING_LOG_WORKSHEET_NAME,
                    rows=100,
                    cols=10
                )
                logger.info(f"[경고관리] 패널티로그 시트 생성됨 - 이름: {settings.GOOGLE_SHEETS_WARNING_LOG_WORKSHEET_NAME}")

            self._ensure_headers()
            self._ensure_warning_log_headers()

        except Exception as e:
            logger.error(f"[경고관리] 워크시트 초기화 실패: {e}", exc_info=True)
    
    def _ensure_headers(self) -> None:
        try:
            if not self.worksheet:
                logger.warning("[경고관리] 패널티 워크시트가 None입니다")
                return

            first_row = self.worksheet.row_values(1)
            expected_headers = self.PENALTY_HEADERS

            if not first_row:
                self.worksheet.insert_row(expected_headers, 1)
                logger.info("[경고관리] 패널티 시트 헤더 생성")
            elif first_row != expected_headers:
                logger.warning(f"[경고관리] 패널티 시트 헤더 불일치 - 현재: {first_row}")
        except Exception as e:
            logger.error(f"[경고관리] 패널티 시트 헤더 확인 실패: {e}", exc_info=True)

    def _ensure_warning_log_headers(self) -> None:
        try:
            if not self.warning_log_worksheet:
                logger.warning("[경고관리] 패널티로그 워크시트가 None입니다")
                return

            first_row = self.warning_log_worksheet.row_values(1)
            expected_headers = self.LOG_HEADERS

            if not first_row:
                self.warning_log_worksheet.insert_row(expected_headers, 1)
                logger.info("[경고관리] 패널티로그 시트 헤더 생성")
            elif first_row != expected_headers:
                logger.warning(f"[경고관리] 패널티로그 시트 헤더 불일치 - 현재: {first_row}")
        except Exception as e:
            logger.error(f"[경고관리] 경고로그 시트 헤더 확인 실패: {e}", exc_info=True)

    def _add_to_warning_log(self, warning_type: str, target: str, date: str, restricted_until: str, reason: str, target_id: str = '') -> None:
        """누락 시 이후 누적 회차 과소 산정."""
        if not self.warning_log_worksheet:
            return

        row = self._sheet_row(self.LOG_HEADERS, {
            '대상': target,
            '날짜': date,
            '제한해제일': restricted_until,
            '사유': reason,
            '유형': warning_type,
            '대상ID': str(target_id),
        })
        for attempt in (1, 2):
            try:
                self.warning_log_worksheet.append_row(row)
                logger.debug(f"[경고관리] 외부 로그 기록 - 대상: {target}, 유형: {warning_type}")
                return
            except Exception as e:
                if attempt == 1:
                    logger.warning(f"[경고관리] 경고로그 추가 실패 - 재시도 - 대상: {target}: {e}")
                else:
                    logger.error(f"[경고관리] 경고로그 추가 최종 실패 - 대상: {target}: {e}", exc_info=True)

    @classmethod
    def restriction_days_for(cls, warning_count: int) -> int:
        return cls.RESTRICTION_DAYS.get(warning_count, cls.RESTRICTION_DAYS_MAX)

    @staticmethod
    def _parse_sheet_date(value) -> Optional[date]:
        """빈 값과 형식 오류는 None."""
        try:
            return datetime.strptime(str(value).strip(), '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _sheet_row(headers: List[str], values: Dict[str, str]) -> List[str]:
        return [values.get(header, '') for header in headers]

    def _penalty_row(self, values: Dict[str, str]) -> List[str]:
        return self._sheet_row(self.PENALTY_HEADERS, values)

    def _iter_penalty_rows(self) -> Iterator[Tuple[int, Dict]]:
        """1-based 행 번호와 레코드 dict 순회."""
        all_values = self.worksheet.get_all_values()
        for row_num, row in enumerate(all_values[1:], start=2):
            padded = row + [''] * (len(self.PENALTY_HEADERS) - len(row))
            yield row_num, dict(zip(self.PENALTY_HEADERS, padded))

    def _delete_rows_desc(self, row_nums: List[int], label: str) -> int:
        """위에서부터 지우면 행 번호가 밀림. 반환 삭제 성공 수."""
        deleted = 0
        for row_num in sorted(row_nums, reverse=True):
            try:
                self.worksheet.delete_rows(row_num)
                deleted += 1
            except Exception as e:
                logger.error(f"[경고관리] {label} 행 삭제 실패 - 행: {row_num}: {e}", exc_info=True)
        return deleted

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

    def _count_previous_warnings(self, target_id: str = None, target_name: str = None) -> Optional[int]:
        """만료분이 지워지는 패널티 시트 대신 경고로그 기준, 집계 실패는 None."""
        if not self.warning_log_worksheet:
            return None

        try:
            records = self.warning_log_worksheet.get_all_records(expected_headers=self.LOG_HEADERS)
        except Exception as e:
            logger.error(f"[경고관리] 경고 횟수 집계 실패: {e}", exc_info=True)
            return None

        count = 0
        for record in records:
            if str(record.get('유형', '')).strip() != '경고':
                continue
            if self._matches_target(
                record.get('대상ID', ''), record.get('대상', ''),
                target_id, target_name,
            ):
                count += 1
        return count

    def _build_caution_detail_reason(self, converted_cautions: List[Dict], for_external: bool = False) -> str:
        if not converted_cautions or len(converted_cautions) < self.CAUTION_TO_WARNING_COUNT:
            return "주의 누적"

        lines = ["[주의 누적]"]
        for i, caution in enumerate(converted_cautions[:self.CAUTION_TO_WARNING_COUNT], 1):
            caution_date = caution.get('날짜', 'N/A')
            caution_reason = caution.get('사유', 'N/A')
            if for_external:
                lines.append(f"{i}회 ({caution_date}): {caution_reason}")
            else:
                caution_admin = caution.get('관리자ID', 'N/A')
                lines.append(f"{i}회 ({caution_date}, {caution_admin}): {caution_reason}")

        return "\n".join(lines)

    @staticmethod
    def _get_warning_date(current_time: datetime) -> date:
        """마감 시각 이전 경고는 전날 스크림 건."""
        if current_time.hour < settings.TEAM_REGISTRATION_DEADLINE_HOUR:
            return (current_time - timedelta(days=1)).date()
        return current_time.date()
    
    def _find_cautions(self, target_id: str, target_name: str = None) -> List[Tuple[int, Dict]]:
        if not self.worksheet:
            return []

        try:
            return [
                (row_num, record)
                for row_num, record in self._iter_penalty_rows()
                if str(record['유형']).strip() == '주의'
                and self._matches_target(
                    record['대상ID'], record['대상'], target_id, target_name
                )
            ]
        except Exception as e:
            logger.error(f"[경고관리] 주의 기록 조회 실패: {e}", exc_info=True)
            return []

    def _compute_restriction_terms(
        self, target: str, target_id: str, *, fallback_on_failure: bool
    ) -> Optional[Dict]:
        """마스터즈 진행일 연장은 process_masters_days 담당."""
        warning_date = self._get_warning_date(get_current_kst_time())
        prev_warnings = self._count_previous_warnings(target_id, target)
        if prev_warnings is None:
            if not fallback_on_failure:
                return None
            logger.error(f"[경고관리] 누적 집계 실패 - 최소 회차로 진행: {target}")
            prev_warnings = 0
        warning_count = prev_warnings + 1
        duration_days = self.restriction_days_for(warning_count)
        return {
            'warning_date': warning_date,
            'warning_count': warning_count,
            'duration_days': duration_days,
            'restricted_until': warning_date + timedelta(days=duration_days),
        }

    def _check_and_convert_cautions(self, target: str, target_id: str) -> Tuple[Optional[Dict], List[Dict]]:
        cautions = self._find_cautions(target_id, target)

        if len(cautions) >= self.CAUTION_TO_WARNING_COUNT:
            # 주의는 이미 기록된 뒤라 집계 실패해도 최소 회차로 전환
            terms = self._compute_restriction_terms(target, target_id, fallback_on_failure=True)

            # 역순은 최신 주의부터 표시하는 순서
            converted_rows = cautions[-self.CAUTION_TO_WARNING_COUNT:][::-1]
            converted_cautions = [record for _, record in converted_rows]

            self._delete_rows_desc([row_num for row_num, _ in converted_rows], "주의")

            return {
                'target': target,
                'target_id': target_id,
                'type': '경고',
                'warning_date': terms['warning_date'].strftime('%Y-%m-%d'),
                'restricted_until': terms['restricted_until'].strftime('%Y-%m-%d'),
                'warning_count': terms['warning_count'],
                'duration_days': terms['duration_days'],
                'admin_id': '시스템',
                'note': '주의 누적'
            }, converted_cautions

        return None, []
    
    async def add_warning(
        self,
        target: str,
        target_id: str,
        warning_type: str,
        reason: str,
        admin_display_name: str
    ) -> Tuple[bool, str, Optional[Dict], List[Dict]]:
        """반환: 성공 여부, 메시지, 자동 생성된 경고 정보, 변환된 주의 내역."""
        if not self.worksheet:
            return False, "구글 시트 연결이 설정되지 않았습니다.", None, []
        
        try:
            current_time = get_current_kst_time()

            datetime_str = current_time.strftime('%Y-%m-%d %H:%M:%S')

            if warning_type == '주의':
                row = self._penalty_row({
                    '날짜': datetime_str,
                    '대상': target,
                    '대상ID': target_id,
                    '유형': '주의',
                    '사유': reason,
                    '관리자ID': admin_display_name,
                })
                await asyncio.to_thread(self.worksheet.append_row, row)
                logger.info(f"[경고관리] 주의 부여 - 대상: {target}, 관리자: {admin_display_name}")

                caution_date = current_time.strftime('%Y-%m-%d')
                await asyncio.to_thread(
                    self._add_to_warning_log,
                    warning_type='주의',
                    target=target,
                    date=caution_date,
                    restricted_until='',
                    reason=reason,
                    target_id=target_id
                )

                self._invalidate_cache()

                auto_warning, converted_cautions = await asyncio.to_thread(self._check_and_convert_cautions, target, target_id)
                if auto_warning:
                    detailed_reason_internal = self._build_caution_detail_reason(converted_cautions, for_external=False)
                    detailed_reason_external = self._build_caution_detail_reason(converted_cautions, for_external=True)

                    auto_row = self._penalty_row({
                        '날짜': datetime_str,
                        '대상': auto_warning['target'],
                        '대상ID': auto_warning['target_id'],
                        '유형': auto_warning['type'],
                        '사유': detailed_reason_internal,
                        '경고일': auto_warning['warning_date'],
                        '제한해제일': auto_warning['restricted_until'],
                        '관리자ID': auto_warning['admin_id'],
                        '비고': auto_warning['note'],
                    })
                    await asyncio.to_thread(self.worksheet.append_row, auto_row)
                    logger.info(f"[경고관리] 주의 누적으로 경고 전환 - 대상: {target}, 제한해제: {auto_warning['restricted_until']}")

                    await asyncio.to_thread(
                        self._add_to_warning_log,
                        warning_type='경고',
                        target=target,
                        date=auto_warning['warning_date'],
                        restricted_until=auto_warning['restricted_until'],
                        reason=detailed_reason_external,
                        target_id=target_id
                    )

                    self._invalidate_cache()

                    return True, (
                        f"주의가 추가되었습니다. 주의 {self.CAUTION_TO_WARNING_COUNT}회로 인해 경고 1회가 자동 부여되었습니다. "
                        f"누적 {auto_warning['warning_count']}회, 제한 {auto_warning['duration_days']}일, "
                        f"{auto_warning['restricted_until']}까지 제한."
                    ), auto_warning, converted_cautions

                return True, "주의가 추가되었습니다.", None, []
            
            elif warning_type == '경고':
                terms = await asyncio.to_thread(
                    self._compute_restriction_terms, target, target_id,
                    fallback_on_failure=False,
                )
                if terms is None:
                    return False, "누적 경고 집계에 실패했습니다. 잠시 후 다시 시도해주세요.", None, []

                warning_count = terms['warning_count']
                duration_days = terms['duration_days']
                warning_date_str = terms['warning_date'].strftime('%Y-%m-%d')
                restricted_str = terms['restricted_until'].strftime('%Y-%m-%d')

                row = self._penalty_row({
                    '날짜': datetime_str,
                    '대상': target,
                    '대상ID': target_id,
                    '유형': '경고',
                    '사유': reason,
                    '경고일': warning_date_str,
                    '제한해제일': restricted_str,
                    '관리자ID': admin_display_name,
                })
                await asyncio.to_thread(self.worksheet.append_row, row)
                logger.info(
                    f"[경고관리] 경고 부여 - 대상: {target}, 관리자: {admin_display_name}, "
                    f"누적: {warning_count}회, 제한 {duration_days}일, 제한해제: {restricted_str}"
                )

                await asyncio.to_thread(
                    self._add_to_warning_log,
                    warning_type='경고',
                    target=target,
                    date=warning_date_str,
                    restricted_until=restricted_str,
                    reason=reason,
                    target_id=target_id
                )

                self._invalidate_cache()

                return True, (
                    f"경고가 추가되었습니다. 누적 {warning_count}회, 제한 {duration_days}일, "
                    f"{restricted_str}까지 제한."
                ), {
                    'warning_date': warning_date_str,
                    'restricted_until': restricted_str,
                    'warning_count': warning_count,
                    'duration_days': duration_days
                }, []

            else:
                return False, "유형은 '주의' 또는 '경고'만 가능합니다.", None, []

        except Exception as e:
            logger.error(f"[경고관리] 경고 추가 실패 - 대상: {target}, 유형: {warning_type}, 오류: {e}", exc_info=True)
            return False, f"경고 추가 중 오류가 발생했습니다: {str(e)}", None, []
    
    def _get_warnings_cache(self) -> List[Dict]:
        current_time = get_current_kst_time()

        if (self._warnings_cache is not None and
            self._cache_timestamp is not None and
            (current_time - self._cache_timestamp).total_seconds() < self._cache_ttl):
            return self._warnings_cache

        try:
            if not self.worksheet:
                return []
            # expected_headers 없으면 빈 헤더 셀에서 gspread 중복 헤더 오류
            all_records = self.worksheet.get_all_records(expected_headers=self.PENALTY_HEADERS)
            warnings = [record for record in all_records if str(record.get('유형', '')).strip() == '경고']

            self._warnings_cache = warnings
            self._cache_timestamp = current_time

            return warnings
            
        except Exception as e:
            logger.error(f"[경고관리] 경고 데이터 캐시 로드 실패: {e}", exc_info=True)
            if self._warnings_cache is not None:
                logger.warning("[경고관리] API 오류 발생 - 캐시된 데이터 사용")
                return self._warnings_cache
            return []
    
    def _invalidate_cache(self) -> None:
        self._warnings_cache = None
        self._cache_timestamp = None
    
    def _find_max_restriction(
        self, warnings: List[Dict], target_id: str = None, target_name: str = None
    ) -> Optional[Dict]:
        latest: Optional[Dict] = None
        for record in warnings:
            if not self._matches_target(
                record.get('대상ID', ''), record.get('대상', ''),
                target_id, target_name,
            ):
                continue

            restricted_until = self._parse_sheet_date(record.get('제한해제일', ''))
            if restricted_until is None:
                continue

            if latest is None or restricted_until > latest['restricted_until']:
                latest = {
                    'restricted_until': restricted_until,
                    'target': str(record.get('대상', '')).strip()
                }
        return latest

    def is_restricted(self, target_id: str = None, target_name: str = None, check_date: Optional[datetime] = None) -> Tuple[bool, Optional[str]]:
        """반환: 제한 여부, 제한 해제일."""
        if not self.worksheet:
            return False, None

        if not target_id and not target_name:
            return False, None

        try:
            if check_date is None:
                check_date = get_current_kst_time()

            warnings = self._get_warnings_cache()
            latest_warning = self._find_max_restriction(warnings, target_id, target_name)

            if latest_warning:
                restricted_until = latest_warning['restricted_until']
                if check_date.date() <= restricted_until:
                    return True, restricted_until.strftime('%Y-%m-%d')

            return False, None

        except Exception as e:
            logger.error(f"[경고관리] 제한 상태 확인 실패: {e}", exc_info=True)
            return False, None
    
    def _load_masters_state(self) -> Optional[date]:
        try:
            with open(self.MASTERS_STATE_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f).get('last_processed', '')
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.error(f"[경고관리] 마스터즈 상태 파일 읽기 실패: {e}", exc_info=True)
            return None
        return self._parse_sheet_date(raw)

    def _save_masters_state(self, last_processed: date) -> None:
        try:
            save_json_atomic(
                self.MASTERS_STATE_FILE,
                {'last_processed': last_processed.strftime('%Y-%m-%d')},
            )
        except Exception as e:
            logger.error(f"[경고관리] 마스터즈 상태 파일 저장 실패: {e}", exc_info=True)

    def _extend_active_restrictions(self, masters_day: date) -> int:
        """개별 update로 쪼개면 부분 적용 뒤 재시도 때 이중 연장."""
        if not self.worksheet:
            return 0

        updates = []
        for row_num, record in self._iter_penalty_rows():
            restricted_until = self._parse_sheet_date(record['제한해제일'])
            if restricted_until is None or restricted_until < masters_day:
                continue

            warning_date = self._parse_sheet_date(record['경고일'])
            if warning_date is not None and warning_date >= masters_day:
                continue

            new_value = (restricted_until + timedelta(days=1)).strftime('%Y-%m-%d')
            cell = rowcol_to_a1(row_num, self.COL_RESTRICTED_UNTIL + 1)
            updates.append({'range': cell, 'values': [[new_value]]})

        if updates:
            self.worksheet.batch_update(updates)
        return len(updates)

    async def process_masters_days(self) -> bool:
        """False면 다음 주기에 같은 구간 재시도."""
        if not self.worksheet:
            return False

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

        # 상태를 끝에 한 번만 저장하면 중간 실패 뒤 재시도에서 이중 연장
        day = last_processed + timedelta(days=1)
        while day <= today:
            if day in masters_days:
                try:
                    extended = await asyncio.to_thread(self._extend_active_restrictions, day)
                except Exception as e:
                    logger.error(f"[경고관리] 마스터즈 진행일({day}) 연장 실패: {e}", exc_info=True)
                    return False
                if extended:
                    self._invalidate_cache()
                    logger.info(f"[경고관리] 마스터즈 진행일({day}) 제한 연장 - {extended}건 +1일")
            self._save_masters_state(day)
            day += timedelta(days=1)
        return True

    def cleanup_expired_restrictions(self) -> int:
        if not self.worksheet:
            return 0

        try:
            current_time = get_current_kst_time()

            deleted_count = self._cleanup_penalty_sheet(current_time)

            if deleted_count > 0:
                self._invalidate_cache()
                logger.info(f"[경고관리] 패널티 시트 정리 완료 - {deleted_count}건 삭제")

            return deleted_count

        except Exception as e:
            logger.error(f"[경고관리] 만료된 제한 항목 정리 실패: {e}", exc_info=True)
            return 0

    def _cleanup_penalty_sheet(self, current_time: datetime) -> int:
        try:
            rows_to_delete = []
            for row_num, record in self._iter_penalty_rows():
                restricted_until_date = self._parse_sheet_date(record['제한해제일'])
                if restricted_until_date is None:
                    continue
                cutoff = datetime.combine(
                    restricted_until_date,
                    datetime.min.time().replace(hour=self.CLEANUP_HOUR),
                    tzinfo=KST,
                )
                if current_time > cutoff:
                    rows_to_delete.append(row_num)

            return self._delete_rows_desc(rows_to_delete, "패널티 시트")

        except Exception as e:
            logger.error(f"[경고관리] 패널티 시트 정리 실패: {e}", exc_info=True)
            return 0

    async def cleanup_loop(self) -> None:
        try:
            while True:
                try:
                    caught_up = await self.process_masters_days()
                except Exception as e:
                    logger.error(f"[경고관리] 마스터즈 처리 실패: {e}", exc_info=True)
                    caught_up = False

                # 연장 전에 만료 행을 지우면 연장 대상 소실
                if caught_up:
                    await asyncio.to_thread(self.cleanup_expired_restrictions)
                else:
                    logger.warning("[경고관리] 마스터즈 처리 미완 - 만료 정리 보류")
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[경고관리] 정리 루프 실패: {e}", exc_info=True)
    
    def start_cleanup_task(self) -> None:
        if self.cleanup_task and not self.cleanup_task.done():
            return

        self.cleanup_task = asyncio.create_task(self.cleanup_loop())

