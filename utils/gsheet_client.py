import os
from typing import Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials

from config.logging_config import get_logger
from config.settings import settings

logger = get_logger('gsheet_client')

GSHEET_SCOPES = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive'
]


def create_gspread_client(
    caller: str = ''
) -> Tuple[Optional[gspread.Client], Optional[gspread.Spreadsheet]]:
    prefix = f"[{caller}] " if caller else ""

    try:
        credentials_path = settings.GOOGLE_SHEETS_CREDENTIALS_PATH

        if not credentials_path:
            logger.warning(f"{prefix}인증 정보 경로가 설정되지 않음")
            return None, None

        if not os.path.exists(credentials_path):
            logger.warning(f"{prefix}인증 정보 파일을 찾을 수 없음 - 경로: {credentials_path}")
            return None, None

        creds = Credentials.from_service_account_file(
            credentials_path,
            scopes=GSHEET_SCOPES
        )
        client = gspread.authorize(creds)

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
        logger.error(f"{prefix}클라이언트 초기화 실패: {e}", exc_info=True)
        return None, None
