"""
경고 관리 모델

구글 시트 API를 통해 경고/주의를 관리합니다.
주의 2회 → 경고 1회 자동 환산 및 제한 날짜 계산을 수행합니다.
"""
import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import gspread
import pytz
from google.oauth2.service_account import Credentials

from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_current_kst_time

logger = get_logger('warning_manager')


class WarningManager:
    """
    경고 관리 클래스
    
    구글 시트 API를 통해 경고/주의를 관리합니다.
    주의 2회 → 경고 1회 자동 환산 및 제한 날짜 계산을 수행합니다.
    """
    
    # 구글 시트 컬럼 인덱스 (0-based) - 패널티 시트 (내부용, 활성 경고만)
    COL_DATE = 0
    COL_TARGET = 1
    COL_TARGET_ID = 2
    COL_TYPE = 3  # 주의/경고
    COL_REASON = 4
    COL_WARNING_DATE = 5
    COL_RESTRICTED_UNTIL = 6
    COL_ADMIN_ID = 7
    COL_NOTE = 8

    # 경고로그 시트 컬럼 인덱스 (0-based) - 외부용, 영구 보관
    LOG_COL_TARGET = 0
    LOG_COL_WARNING_DATE = 1
    LOG_COL_RESTRICTED_UNTIL = 2
    LOG_COL_REASON = 3

    def __init__(self):
        self.client: Optional[gspread.Client] = None
        self.spreadsheet: Optional[gspread.Spreadsheet] = None
        self.worksheet: Optional[gspread.Worksheet] = None
        self.warning_log_worksheet: Optional[gspread.Worksheet] = None  # 경고로그 시트 (외부용)
        self.cleanup_task: Optional[asyncio.Task] = None
        # 경고 데이터 캐시
        self._warnings_cache: Optional[List[Dict]] = None
        self._cache_timestamp: Optional[datetime] = None
        self._cache_ttl: int = 300  # 5분 캐시 TTL (초)
        self._initialize_client()
    
    def _initialize_client(self) -> None:
        """구글 시트 클라이언트 초기화"""
        try:
            credentials_path = settings.GOOGLE_SHEETS_CREDENTIALS_PATH
            
            if not credentials_path:
                logger.warning("[경고관리] 인증 정보 경로가 설정되지 않음")
                return
            
            # 파일 존재 확인
            if not os.path.exists(credentials_path):
                logger.warning(f"[경고관리] 인증 정보 파일을 찾을 수 없음 - 경로: {credentials_path}")
                return
            
            # 서비스 계정 인증
            scope = [
                'https://spreadsheets.google.com/feeds',
                'https://www.googleapis.com/auth/drive'
            ]
            creds = Credentials.from_service_account_file(
                credentials_path,
                scopes=scope
            )
            self.client = gspread.authorize(creds)
            
            # 스프레드시트 열기
            if settings.GOOGLE_SHEETS_WARNING_SPREADSHEET_ID:
                self.spreadsheet = self.client.open_by_key(
                    settings.GOOGLE_SHEETS_WARNING_SPREADSHEET_ID
                )
                logger.info(f"[경고관리] 스프레드시트 연결 성공 - ID: {settings.GOOGLE_SHEETS_WARNING_SPREADSHEET_ID}")

                # 패널티 시트 열기 (없으면 생성)
                try:
                    self.worksheet = self.spreadsheet.worksheet(
                        settings.GOOGLE_SHEETS_WARNING_WORKSHEET_NAME
                    )
                    logger.info(f"[경고관리] 패널티 시트 연결 성공 - 이름: {settings.GOOGLE_SHEETS_WARNING_WORKSHEET_NAME}")
                except gspread.WorksheetNotFound:
                    # 시트가 없으면 생성
                    self.worksheet = self.spreadsheet.add_worksheet(
                        title=settings.GOOGLE_SHEETS_WARNING_WORKSHEET_NAME,
                        rows=100,
                        cols=10
                    )
                    logger.info(f"[경고관리] 패널티 시트 생성됨 - 이름: {settings.GOOGLE_SHEETS_WARNING_WORKSHEET_NAME}")

                # 경고로그 시트 열기 (외부용 - 영구 보관, 없으면 생성)
                try:
                    self.warning_log_worksheet = self.spreadsheet.worksheet(
                        settings.GOOGLE_SHEETS_WARNING_LOG_WORKSHEET_NAME
                    )
                    logger.info(f"[경고관리] 패널티로그 시트 연결 성공 - 이름: {settings.GOOGLE_SHEETS_WARNING_LOG_WORKSHEET_NAME}")
                except gspread.WorksheetNotFound:
                    # 시트가 없으면 생성
                    self.warning_log_worksheet = self.spreadsheet.add_worksheet(
                        title=settings.GOOGLE_SHEETS_WARNING_LOG_WORKSHEET_NAME,
                        rows=100,
                        cols=10
                    )
                    logger.info(f"[경고관리] 패널티로그 시트 생성됨 - 이름: {settings.GOOGLE_SHEETS_WARNING_LOG_WORKSHEET_NAME}")

                # 헤더 확인 및 생성
                self._ensure_headers()
                self._ensure_warning_log_headers()

            else:
                logger.warning("[경고관리] 스프레드시트 ID가 설정되지 않음")
                
        except FileNotFoundError:
            logger.error(f"[경고관리] 인증 정보 파일을 찾을 수 없음 - 경로: {credentials_path}")
        except json.JSONDecodeError:
            logger.error("[경고관리] 인증 정보 파일 형식이 올바르지 않음")
        except Exception as e:
            logger.error(f"[경고관리] 클라이언트 초기화 실패: {e}")
    
    def _ensure_headers(self) -> None:
        """패널티 시트에 헤더가 없으면 생성합니다."""
        try:
            if not self.worksheet:
                logger.warning("[경고관리] 패널티 워크시트가 None입니다")
                return

            # 첫 번째 행 확인
            first_row = self.worksheet.row_values(1)
            expected_headers = ['날짜', '대상', '대상ID', '유형', '사유', '경고일', '제한해제일', '관리자ID', '비고']

            if not first_row or first_row != expected_headers:
                # 헤더가 없거나 다르면 첫 번째 행에 헤더 추가
                # 기존 데이터가 있으면 삭제 후 헤더 추가
                if first_row:
                    self.worksheet.delete_rows(1)
                self.worksheet.insert_row(expected_headers, 1)
                logger.info(f"[경고관리] 패널티 시트 헤더 생성")
        except Exception as e:
            logger.error(f"[경고관리] 패널티 시트 헤더 확인 실패: {e}")

    def _ensure_warning_log_headers(self) -> None:
        """경고로그 시트에 헤더가 없으면 생성합니다."""
        try:
            if not self.warning_log_worksheet:
                logger.warning("[경고관리] 패널티로그 워크시트가 None입니다")
                return

            # 첫 번째 행 확인
            first_row = self.warning_log_worksheet.row_values(1)
            expected_headers = ['대상', '경고일', '제한해제일', '사유']

            if not first_row or first_row != expected_headers:
                # 헤더가 없거나 다르면 첫 번째 행에 헤더 추가
                if first_row:
                    self.warning_log_worksheet.delete_rows(1)
                self.warning_log_worksheet.insert_row(expected_headers, 1)
                logger.info(f"[경고관리] 패널티로그 시트 헤더 생성")
        except Exception as e:
            logger.error(f"[경고관리] 경고로그 시트 헤더 확인 실패: {e}")

    def _add_to_warning_log(self, target: str, warning_date: str, restricted_until: str, reason: str) -> None:
        """경고로그 시트에 항목을 추가합니다. (영구 보관 - 삭제되지 않음)"""
        try:
            if not self.warning_log_worksheet:
                return

            row = [target, warning_date, restricted_until, reason]
            self.warning_log_worksheet.append_row(row)
            logger.info(f"[경고관리] 외부 로그 기록 - 대상: {target}")
        except Exception as e:
            logger.error(f"[경고관리] 경고로그 추가 실패 - 대상: {target}: {e}")

    def _build_caution_detail_reason(self, converted_cautions: List[Dict], for_external: bool = False) -> str:
        """
        주의 2회 누적으로 인한 경고 시, 두 주의의 상세 사유를 생성합니다.

        Args:
            converted_cautions: 변환된 주의 내역 리스트
            for_external: 외부용(패널티로그)인 경우 True (처리자 정보 제외)

        Returns:
            포맷팅된 상세 사유 문자열
        """
        if not converted_cautions or len(converted_cautions) < 2:
            return "주의 누적"

        lines = ["[주의 누적]"]
        for i, caution in enumerate(converted_cautions[:2], 1):
            caution_date = caution.get('날짜', 'N/A')
            caution_reason = caution.get('사유', 'N/A')
            if for_external:
                # 외부용: 처리자 정보 제외
                lines.append(f"{i}회 ({caution_date}): {caution_reason}")
            else:
                # 내부용: 처리자 정보 포함
                caution_admin = caution.get('관리자ID', 'N/A')
                lines.append(f"{i}회 ({caution_date}, {caution_admin}): {caution_reason}")

        return "\n".join(lines)

    def _calculate_restricted_until(self, warning_date: datetime) -> datetime:
        """
        경고 받은 날짜 기준으로 제한 해제 날짜를 계산합니다.
        경고 받은 날의 다음날
        """
        next_day = warning_date + timedelta(days=1)
        return next_day
    
    def _get_recent_cautions(self, target_id: str, limit: int = 10) -> List[Dict]:
        """최근 주의 기록을 가져옵니다."""
        if not self.worksheet:
            return []

        try:
            # 캐시 없이 직접 조회 (주의 데이터는 경량)
            # expected_headers를 명시하여 빈 헤더 셀로 인한 중복 오류 방지
            expected_headers = ['날짜', '대상', '대상ID', '유형', '사유', '경고일', '제한해제일', '관리자ID', '비고']
            all_records = self.worksheet.get_all_records(expected_headers=expected_headers)
            
            cautions = []
            
            for record in reversed(all_records):  # 최신순
                # 대상ID를 문자열로 변환 (구글 시트에서 숫자는 int로 반환될 수 있음)
                record_target_id = str(record.get('대상ID', '')).strip()
                record_type = str(record.get('유형', '')).strip()
                
                if record_target_id == str(target_id) and record_type == '주의':
                    cautions.append(record)
                    if len(cautions) >= limit:
                        break
            
            return cautions
        except Exception as e:
            logger.error(f"[경고관리] 주의 기록 조회 실패: {e}")
            return []
    
    def _find_caution_rows(self, target_id: str) -> List[int]:
        """주의 기록의 행 번호를 찾습니다 (헤더 제외, 1-based)."""
        if not self.worksheet:
            return []
        
        try:
            all_values = self.worksheet.get_all_values()
            caution_rows = []
            
            # 헤더 제외하고 2번째 행부터 확인 (인덱스 1부터)
            for i in range(1, len(all_values)):
                row = all_values[i]
                if len(row) > self.COL_TARGET_ID and len(row) > self.COL_TYPE:
                    # 문자열로 변환하여 비교 (구글 시트에서 숫자는 문자열로 저장됨)
                    row_target_id = str(row[self.COL_TARGET_ID]).strip() if row[self.COL_TARGET_ID] else ''
                    row_type = str(row[self.COL_TYPE]).strip() if row[self.COL_TYPE] else ''
                    
                    if row_target_id == str(target_id) and row_type == '주의':
                        caution_rows.append(i + 1)  # 1-based 행 번호
            
            return caution_rows
        except Exception as e:
            logger.error(f"[경고관리] 주의 행 찾기 실패: {e}")
            return []
    
    def _check_and_convert_cautions(self, target: str, target_id: str) -> Tuple[Optional[Dict], List[Dict]]:
        """
        주의 2회 → 경고 1회 자동 환산
        주의가 2회가 되면 경고 1회로 자동 변환하고 제한 날짜를 계산합니다.
        기존 주의 2개 행을 삭제하고 경고 1개를 추가합니다.

        Returns:
            (자동 경고 정보, 변환된 주의 내역 2개 리스트)
        """
        cautions = self._get_recent_cautions(target_id)

        # 주의가 2회 이상인 경우
        if len(cautions) >= 2:
            current_time = get_current_kst_time()

            # 17시 이전이면 전날 날짜로 기록
            if current_time.hour < 17:
                from datetime import timedelta
                warning_date = (current_time - timedelta(days=1)).date()
                # 제한 해제일 계산을 위해 전날 날짜로 datetime 생성
                warning_datetime = datetime.combine(warning_date, current_time.time())
            else:
                warning_date = current_time.date()
                warning_datetime = current_time

            restricted_until = self._calculate_restricted_until(warning_datetime).date()

            # 변환될 주의 내역 2개 저장 (삭제 전에)
            converted_cautions = cautions[:2]

            # 주의 2개 행 삭제
            caution_rows = self._find_caution_rows(target_id)
            if len(caution_rows) >= 2:
                # 내림차순 정렬하여 아래 행부터 삭제 (행 번호가 바뀌지 않도록)
                caution_rows.sort(reverse=True)
                for row_num in caution_rows[:2]:
                    try:
                        self.worksheet.delete_rows(row_num)
                    except Exception as e:
                        logger.error(f"[경고관리] 주의 행 삭제 실패 - 행: {row_num}: {e}")

            return {
                'date': warning_date.strftime('%Y-%m-%d'),
                'target': target,
                'target_id': target_id,
                'type': '경고',
                'reason': '자동',
                'warning_date': warning_date.strftime('%Y-%m-%d'),
                'restricted_until': restricted_until.strftime('%Y-%m-%d'),
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
        """
        경고 또는 주의를 추가합니다.

        Args:
            target: 대상 닉네임
            target_id: 대상 Discord ID
            warning_type: '주의' 또는 '경고'
            reason: 사유
            admin_display_name: 관리자 Discord 디스플레이 네임

        Returns:
            (성공 여부, 메시지, 자동 생성된 경고 정보, 변환된 주의 내역 리스트)
        """
        if not self.worksheet:
            return False, "구글 시트 연결이 설정되지 않았습니다.", None, []
        
        try:
            current_time = get_current_kst_time()

            # 일시 문자열 (시분초 포함)
            datetime_str = current_time.strftime('%Y-%m-%d %H:%M:%S')

            # 주의 추가인 경우
            if warning_type == '주의':
                # 주의 추가
                row = [
                    datetime_str,  # 일시 (시분초 포함)
                    target,
                    target_id,
                    '주의',
                    reason,
                    '',  # 경고일 (주의는 없음)
                    '',  # 제한해제일 (주의는 없음)
                    admin_display_name,
                    ''
                ]
                await asyncio.to_thread(self.worksheet.append_row, row)
                logger.info(f"[경고관리] 주의 부여 - 대상: {target}, 관리자: {admin_display_name}")

                # 캐시 무효화
                self._invalidate_cache()

                # 주의 2회 → 경고 1회 자동 환산 확인
                auto_warning, converted_cautions = await asyncio.to_thread(self._check_and_convert_cautions, target, target_id)
                if auto_warning:
                    # 두 주의의 상세 사유 생성 (내부용: 처리자 포함)
                    detailed_reason_internal = self._build_caution_detail_reason(converted_cautions, for_external=False)
                    # 외부용: 처리자 제외
                    detailed_reason_external = self._build_caution_detail_reason(converted_cautions, for_external=True)

                    # 자동 경고 추가 (내부 시트)
                    auto_row = [
                        datetime_str,  # 일시 (시분초 포함)
                        auto_warning['target'],
                        auto_warning['target_id'],
                        auto_warning['type'],
                        detailed_reason_internal,  # 내부용 상세 사유
                        auto_warning['warning_date'],
                        auto_warning['restricted_until'],
                        auto_warning['admin_id'],
                        auto_warning['note']
                    ]
                    await asyncio.to_thread(self.worksheet.append_row, auto_row)
                    logger.info(f"[경고관리] 주의 누적 → 경고 전환 - 대상: {target}, 제한해제: {auto_warning['restricted_until']}")

                    # 경고로그 시트에 추가 (외부용 - 영구 보관, 처리자 정보 제외)
                    await asyncio.to_thread(
                        self._add_to_warning_log,
                        target=target,
                        warning_date=auto_warning['warning_date'],
                        restricted_until=auto_warning['restricted_until'],
                        reason=detailed_reason_external  # 외부용 상세 사유 (처리자 제외)
                    )

                    # 캐시 무효화
                    self._invalidate_cache()

                    return True, f"주의가 추가되었습니다. 주의 2회로 인해 경고 1회가 자동 부여되었습니다. (제한 해제일: {auto_warning['restricted_until']})", auto_warning, converted_cautions

                return True, "주의가 추가되었습니다.", None, []
            
            # 경고 추가인 경우
            elif warning_type == '경고':
                # 17시 이전이면 전날 날짜로 경고일 기록
                if current_time.hour < 17:
                    warning_date = (current_time - timedelta(days=1)).date()
                    # 제한 해제일 계산을 위해 전날 날짜로 datetime 생성
                    warning_datetime = datetime.combine(warning_date, current_time.time())
                else:
                    warning_date = current_time.date()
                    warning_datetime = current_time

                restricted_until = self._calculate_restricted_until(warning_datetime).date()

                row = [
                    datetime_str,  # 일시 (시분초 포함)
                    target,
                    target_id,
                    '경고',
                    reason,
                    warning_date.strftime('%Y-%m-%d'),
                    restricted_until.strftime('%Y-%m-%d'),
                    admin_display_name,
                    ''
                ]
                await asyncio.to_thread(self.worksheet.append_row, row)
                logger.info(f"[경고관리] 경고 부여 - 대상: {target}, 관리자: {admin_display_name}, 제한해제: {restricted_until.strftime('%Y-%m-%d')}")

                # 경고로그 시트에 추가 (외부용 - 영구 보관, 처리자 정보 제외)
                await asyncio.to_thread(
                    self._add_to_warning_log,
                    target=target,
                    warning_date=warning_date.strftime('%Y-%m-%d'),
                    restricted_until=restricted_until.strftime('%Y-%m-%d'),
                    reason=reason  # 사유만 (처리자 정보 없음)
                )

                # 캐시 무효화
                self._invalidate_cache()

                return True, f"경고가 추가되었습니다. (제한 해제일: {restricted_until.strftime('%Y-%m-%d')})", {
                    'warning_date': warning_date.strftime('%Y-%m-%d'),
                    'restricted_until': restricted_until.strftime('%Y-%m-%d')
                }, []

            else:
                return False, "유형은 '주의' 또는 '경고'만 가능합니다.", None, []

        except Exception as e:
            logger.error(f"[경고관리] 경고 추가 실패 - 대상: {target}, 유형: {warning_type}, 오류: {e}")
            return False, f"경고 추가 중 오류가 발생했습니다: {str(e)}", None, []
    
    def _get_warnings_cache(self) -> List[Dict]:
        """경고 데이터를 캐시에서 가져오거나 새로 로드합니다."""
        current_time = get_current_kst_time()
        
        # 캐시가 유효한지 확인
        if (self._warnings_cache is not None and 
            self._cache_timestamp is not None and
            (current_time - self._cache_timestamp).total_seconds() < self._cache_ttl):
            return self._warnings_cache
        
        # 캐시가 없거나 만료된 경우 새로 로드
        try:
            if not self.worksheet:
                return []
            # expected_headers를 명시하여 빈 헤더 셀로 인한 중복 오류 방지
            expected_headers = ['날짜', '대상', '대상ID', '유형', '사유', '경고일', '제한해제일', '관리자ID', '비고']
            all_records = self.worksheet.get_all_records(expected_headers=expected_headers)
            # 경고만 필터링
            warnings = [record for record in all_records if str(record.get('유형', '')).strip() == '경고']
            
            # 캐시 업데이트
            self._warnings_cache = warnings
            self._cache_timestamp = current_time
            
            return warnings
            
        except Exception as e:
            logger.error(f"[경고관리] 경고 데이터 캐시 로드 실패: {e}")
            # 오류 발생 시 기존 캐시가 있으면 사용, 없으면 빈 리스트 반환
            if self._warnings_cache is not None:
                logger.warning("[경고관리] API 오류 발생 - 캐시된 데이터 사용")
                return self._warnings_cache
            return []
    
    def _invalidate_cache(self) -> None:
        """캐시를 무효화합니다."""
        self._warnings_cache = None
        self._cache_timestamp = None
    
    def is_restricted(self, target_id: str = None, target_name: str = None, check_date: Optional[datetime] = None) -> Tuple[bool, Optional[str]]:
        """
        대상이 현재 제한 상태인지 확인합니다.
        
        Args:
            target_id: 대상 Discord ID (선택)
            target_name: 대상 닉네임 (선택, target_id가 없을 때 사용)
            check_date: 확인할 날짜 (None이면 현재 날짜)
        
        Returns:
            (제한 여부, 제한 해제일)
        """
        if not self.worksheet:
            return False, None
        
        if not target_id and not target_name:
            return False, None
        
        try:
            if check_date is None:
                check_date = get_current_kst_time()
            
            # 캐시에서 경고 데이터 가져오기
            warnings = self._get_warnings_cache()
            
            # 최신 경고 기록 찾기
            latest_warning = None
            for record in reversed(warnings):
                # Discord ID로 확인 (우선)
                if target_id:
                    # 대상ID를 문자열로 변환 (구글 시트에서 숫자는 int로 반환될 수 있음)
                    record_target_id = str(record.get('대상ID', '')).strip()
                    
                    if record_target_id == str(target_id):
                        restricted_until_str = str(record.get('제한해제일', '')).strip()
                        if restricted_until_str:
                            try:
                                restricted_until = datetime.strptime(restricted_until_str, '%Y-%m-%d').date()
                                latest_warning = {
                                    'restricted_until': restricted_until,
                                    'target': str(record.get('대상', '')).strip()
                                }
                                break
                            except ValueError:
                                continue
                # 닉네임으로 확인 (ID가 없을 때)
                elif target_name and not target_id:
                    record_target = str(record.get('대상', '')).strip()
                    # 대소문자 구분 없이 비교
                    if record_target.lower() == target_name.lower():
                        restricted_until_str = str(record.get('제한해제일', '')).strip()
                        if restricted_until_str:
                            try:
                                restricted_until = datetime.strptime(restricted_until_str, '%Y-%m-%d').date()
                                latest_warning = {
                                    'restricted_until': restricted_until,
                                    'target': record_target
                                }
                                break
                            except ValueError:
                                continue
            
            if latest_warning:
                restricted_until = latest_warning['restricted_until']
                if check_date.date() <= restricted_until:
                    return True, restricted_until.strftime('%Y-%m-%d')
            
            return False, None
            
        except Exception as e:
            logger.error(f"[경고관리] 제한 상태 확인 실패: {e}")
            # 오류 발생 시 캐시된 데이터로 재시도
            if self._warnings_cache is not None:
                logger.warning("[경고관리] API 오류 - 캐시 데이터로 재시도")
                try:
                    warnings = self._warnings_cache
                    latest_warning = None
                    for record in reversed(warnings):
                        if target_id:
                            record_target_id = str(record.get('대상ID', '')).strip()
                            if record_target_id == str(target_id):
                                restricted_until_str = str(record.get('제한해제일', '')).strip()
                                if restricted_until_str:
                                    try:
                                        restricted_until = datetime.strptime(restricted_until_str, '%Y-%m-%d').date()
                                        if check_date.date() <= restricted_until:
                                            return True, restricted_until.strftime('%Y-%m-%d')
                                    except ValueError:
                                        continue
                        elif target_name:
                            record_target = str(record.get('대상', '')).strip()
                            if record_target.lower() == target_name.lower():
                                restricted_until_str = str(record.get('제한해제일', '')).strip()
                                if restricted_until_str:
                                    try:
                                        restricted_until = datetime.strptime(restricted_until_str, '%Y-%m-%d').date()
                                        if check_date.date() <= restricted_until:
                                            return True, restricted_until.strftime('%Y-%m-%d')
                                    except ValueError:
                                        continue
                except Exception as e2:
                    logger.error(f"[경고관리] 캐시 데이터로 재시도 실패: {e2}")
            return False, None
    
    def cleanup_expired_restrictions(self) -> int:
        """
        제한 해제일이 지난 항목을 삭제합니다.
        종료일 18시 이후부터 삭제합니다.
        패널티 시트(내부용)만 정리합니다. 경고로그(외부용)는 영구 보관되어 삭제하지 않습니다.

        Returns:
            삭제된 행 수
        """
        if not self.worksheet:
            return 0

        try:
            current_time = get_current_kst_time()
            kst = pytz.timezone('Asia/Seoul')

            # 패널티 시트만 정리 (외부용 경고로그는 영구 보관)
            deleted_count = self._cleanup_penalty_sheet(current_time, kst)

            if deleted_count > 0:
                # 캐시 무효화
                self._invalidate_cache()
                logger.info(f"[경고관리] 패널티 시트 정리 완료 - {deleted_count}건 삭제")

            return deleted_count

        except Exception as e:
            logger.error(f"[경고관리] 만료된 제한 항목 정리 실패: {e}")
            return 0

    def _cleanup_penalty_sheet(self, current_time: datetime, kst) -> int:
        """패널티 시트에서 만료된 항목을 삭제합니다."""
        try:
            all_values = self.worksheet.get_all_values()

            if len(all_values) <= 1:  # 헤더만 있거나 비어있음
                return 0

            # 삭제할 행 번호 수집 (헤더 제외, 1-based)
            rows_to_delete = []

            # 헤더 제외하고 2번째 행부터 확인 (인덱스 1부터)
            for i in range(1, len(all_values)):
                row = all_values[i]
                if len(row) > self.COL_RESTRICTED_UNTIL:
                    # 문자열로 변환하여 안전하게 처리
                    restricted_until_str = str(row[self.COL_RESTRICTED_UNTIL]).strip() if row[self.COL_RESTRICTED_UNTIL] else ''
                    if restricted_until_str:
                        try:
                            restricted_until_date = datetime.strptime(restricted_until_str, '%Y-%m-%d').date()
                            # 종료일 18시를 datetime으로 변환 (KST timezone 적용)
                            restricted_until_datetime = kst.localize(
                                datetime.combine(restricted_until_date, datetime.min.time().replace(hour=18))
                            )
                            # 현재 시간이 종료일 18시 이후인지 확인
                            if current_time > restricted_until_datetime:
                                rows_to_delete.append(i + 1)  # 1-based 행 번호
                        except ValueError:
                            continue

            # 내림차순 정렬하여 아래 행부터 삭제 (행 번호가 바뀌지 않도록)
            rows_to_delete.sort(reverse=True)

            deleted_count = 0
            for row_num in rows_to_delete:
                try:
                    self.worksheet.delete_rows(row_num)
                    deleted_count += 1
                except Exception as e:
                    logger.error(f"[경고관리] 패널티 시트 행 삭제 실패 - 행: {row_num}: {e}")

            return deleted_count

        except Exception as e:
            logger.error(f"[경고관리] 패널티 시트 정리 실패: {e}")
            return 0

    async def cleanup_loop(self) -> None:
        """주기적으로 만료된 제한 항목을 정리합니다."""
        try:
            # 봇 시작 시 즉시 정리 작업 실행
            await asyncio.to_thread(self.cleanup_expired_restrictions)
            while True:
                await asyncio.sleep(3600)  # 1시간마다 체크
                await asyncio.to_thread(self.cleanup_expired_restrictions)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[경고관리] 정리 루프 실패: {e}")
    
    def start_cleanup_task(self) -> None:
        """정리 태스크를 시작합니다."""
        if self.cleanup_task and not self.cleanup_task.done():
            return

        self.cleanup_task = asyncio.create_task(self.cleanup_loop())

