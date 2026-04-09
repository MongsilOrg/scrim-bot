"""
구글 시트 클라이언트 공통 초기화 유틸리티

gspread 클라이언트와 스프레드시트 객체를 생성하는 공통 함수를 제공합니다.
"""
import os
from typing import Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials

from config.logging_config import get_logger
from config.settings import settings

logger = get_logger('gsheet_client')

# 구글 시트 API 스코프
GSHEET_SCOPES = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive'
]


def create_gspread_client(
    caller: str = ''
) -> Tuple[Optional[gspread.Client], Optional[gspread.Spreadsheet]]:
    """
    구글 시트 클라이언트와 메인 스프레드시트를 초기화합니다.

    Args:
        caller: 호출자 식별 문자열 (로그용, 예: '경고관리', '구글시트')

    Returns:
        (gspread.Client, gspread.Spreadsheet) 튜플.
        초기화 실패 시 해당 항목은 None.
    """
    prefix = f"[{caller}] " if caller else ""

    try:
        credentials_path = settings.GOOGLE_SHEETS_CREDENTIALS_PATH

        if not credentials_path:
            logger.warning(f"{prefix}인증 정보 경로가 설정되지 않음")
            return None, None

        # 파일 존재 확인
        if not os.path.exists(credentials_path):
            logger.warning(f"{prefix}인증 정보 파일을 찾을 수 없음 - 경로: {credentials_path}")
            return None, None

        # 서비스 계정 인증
        creds = Credentials.from_service_account_file(
            credentials_path,
            scopes=GSHEET_SCOPES
        )
        client = gspread.authorize(creds)

        # 스프레드시트 열기
        spreadsheet = None
        if settings.GOOGLE_SHEETS_MAIN_SPREADSHEET_ID:
            spreadsheet = client.open_by_key(
                settings.GOOGLE_SHEETS_MAIN_SPREADSHEET_ID
            )
            logger.debug(f"{prefix}스프레드시트 연결 성공 - ID: {settings.GOOGLE_SHEETS_MAIN_SPREADSHEET_ID}")
        else:
            logger.warning(f"{prefix}스프레드시트 ID가 설정되지 않음")

        return client, spreadsheet

    except Exception as e:
        logger.error(f"{prefix}클라이언트 초기화 실패: {e}")
        return None, None
