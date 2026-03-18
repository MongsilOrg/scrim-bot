"""
스크림봇 설정 모듈
환경변수 및 기본 설정을 관리합니다.
"""
import os
from typing import Set

from dotenv import load_dotenv

# 환경변수 로드
load_dotenv()


def _parse_int(value: str, default: int = 0) -> int:
    """문자열을 int로 변환합니다. 빈 문자열이면 기본값을 반환합니다."""
    value = value.strip()
    if not value:
        return default
    return int(value)


def _parse_int_set(value: str) -> Set[int]:
    """쉼표로 구분된 문자열을 int Set으로 변환합니다."""
    value = value.strip()
    if not value:
        return set()
    return set(int(id_.strip()) for id_ in value.split(',') if id_.strip())


def _parse_group_channel_ids(value: str) -> dict:
    """'A:123,B:456' 형식의 문자열을 dict로 변환합니다."""
    value = value.strip()
    if not value:
        return {}
    result = {}
    for pair in value.split(','):
        pair = pair.strip()
        if ':' in pair:
            key, val = pair.split(':', 1)
            key = key.strip()
            val = val.strip()
            if key and val:
                result[key] = int(val)
    return result


class Settings:
    """스크림봇 설정 클래스"""

    # Discord 설정
    DISCORD_TOKEN: str = os.getenv('DISCORD_TOKEN', '')
    GUILD_ID: int = _parse_int(os.getenv('GUILD_ID', ''))
    ADMIN_ROLE_IDS: Set[int] = _parse_int_set(os.getenv('ADMIN_ROLE_IDS', ''))

    # BSER API 설정
    BSER_API_KEY: str = os.getenv('BSER_API_KEY', '')

    # 채널 ID 설정
    NOTICE_CHANNEL_ID: int = _parse_int(os.getenv('NOTICE_CHANNEL_ID', ''))
    BACKUP_ANALYSIS_CHANNEL_ID: int = _parse_int(os.getenv('BACKUP_ANALYSIS_CHANNEL_ID', ''))

    # 조별 채널 ID
    GROUP_CHANNEL_IDS: dict = _parse_group_channel_ids(os.getenv('GROUP_CHANNEL_IDS', ''))

    # 조별 카테고리 이름 패턴 (동적으로 음성채널을 찾기 위해 사용)
    # {letter}가 조 문자(A, B, C, ...)로 대체됩니다.
    GROUP_CATEGORY_PATTERN: str = os.getenv('GROUP_CATEGORY_PATTERN', 'Group {letter}')

    # 상수 설정
    TEAMS_PER_GROUP: int = 8
    THUMBNAIL_URL: str = os.getenv('THUMBNAIL_URL', 'https://mongsil.dev/w/src/Scrim.jpg')
    EMBED_FOOTER_TEXT: str = os.getenv('EMBED_FOOTER_TEXT', 'ER Scrim | Powered by Mongsil')
    AUTO_ASSIGNMENT_CHECK_INTERVAL: int = 30  # seconds

    # 로깅 설정
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE: str = os.getenv('LOG_FILE', 'scrimbot.log')

    # 공지사항 설정
    ANNOUNCEMENT_MESSAGE: str = os.getenv('ANNOUNCEMENT_MESSAGE', '')

    # 구글 시트 설정
    GOOGLE_SHEETS_CREDENTIALS_PATH: str = os.getenv(
        'GOOGLE_SHEETS_CREDENTIALS_PATH',
        'credentials/google_sheets_credentials.json'
    )
    # 메인 스프레드시트 ID (시드팀, 테스트 계정, 패널티 공통)
    GOOGLE_SHEETS_MAIN_SPREADSHEET_ID: str = os.getenv(
        'GOOGLE_SHEETS_MAIN_SPREADSHEET_ID', ''
    )
    GOOGLE_SHEETS_WARNING_WORKSHEET_NAME: str = '패널티'
    # 경고로그 시트 설정 (외부 공개용 - 영구 보관)
    GOOGLE_SHEETS_WARNING_LOG_WORKSHEET_NAME: str = '패널티로그'
    # 시드팀 시트 설정
    GOOGLE_SHEETS_SEEDS_WORKSHEET_NAME: str = '시드팀'
    # 테스트 계정 시트 설정
    GOOGLE_SHEETS_TEST_ACCOUNTS_WORKSHEET_NAME: str = '테스트'

    @classmethod
    def validate(cls) -> tuple[bool, list[str]]:
        """필수 설정값 검증. (성공 여부, 누락 항목 리스트)를 반환합니다."""
        errors: list[str] = []

        # 문자열 필수 항목
        required_str_vars = {
            'DISCORD_TOKEN': cls.DISCORD_TOKEN,
            'BSER_API_KEY': cls.BSER_API_KEY,
            'GOOGLE_SHEETS_MAIN_SPREADSHEET_ID': cls.GOOGLE_SHEETS_MAIN_SPREADSHEET_ID,
        }

        for name, value in required_str_vars.items():
            if not value:
                errors.append(f"환경변수 '{name}'이(가) 설정되지 않았습니다")

        # int 필수 항목 (0이면 미설정)
        required_int_vars = {
            'GUILD_ID': cls.GUILD_ID,
            'NOTICE_CHANNEL_ID': cls.NOTICE_CHANNEL_ID,
            'BACKUP_ANALYSIS_CHANNEL_ID': cls.BACKUP_ANALYSIS_CHANNEL_ID,
        }

        for name, value in required_int_vars.items():
            if not value:
                errors.append(f"환경변수 '{name}'이(가) 설정되지 않았습니다")

        # Set/dict 필수 항목
        if not cls.ADMIN_ROLE_IDS:
            errors.append("환경변수 'ADMIN_ROLE_IDS'이(가) 설정되지 않았습니다")
        if not cls.GROUP_CHANNEL_IDS:
            errors.append("환경변수 'GROUP_CHANNEL_IDS'이(가) 설정되지 않았습니다")

        # 인증 파일 존재 여부 확인
        if cls.GOOGLE_SHEETS_CREDENTIALS_PATH:
            if not os.path.exists(cls.GOOGLE_SHEETS_CREDENTIALS_PATH):
                errors.append(
                    f"Google Sheets 인증 파일을 찾을 수 없습니다: {cls.GOOGLE_SHEETS_CREDENTIALS_PATH}"
                )

        return (len(errors) == 0, errors)


# 전역 설정 인스턴스
settings = Settings()
