import os
from typing import Set

from dotenv import load_dotenv

load_dotenv()


def _parse_int(value: str, default: int = 0) -> int:
    value = value.strip()
    if not value:
        return default
    return int(value)


def _parse_int_set(value: str) -> Set[int]:
    value = value.strip()
    if not value:
        return set()
    return set(int(id_.strip()) for id_ in value.split(',') if id_.strip())


def _parse_group_channel_ids(value: str) -> dict:
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
    DISCORD_TOKEN: str = os.getenv('DISCORD_TOKEN', '')
    GUILD_ID: int = _parse_int(os.getenv('GUILD_ID', ''))
    ADMIN_ROLE_IDS: Set[int] = _parse_int_set(os.getenv('ADMIN_ROLE_IDS', ''))
    TEST_ACCOUNT_CONTACT_ID: int = _parse_int(os.getenv('TEST_ACCOUNT_CONTACT_ID', '602522819594551306'))

    BSER_API_KEY: str = os.getenv('BSER_API_KEY', '')

    NOTICE_CHANNEL_ID: int = _parse_int(os.getenv('NOTICE_CHANNEL_ID', ''))
    BACKUP_ANALYSIS_CHANNEL_ID: int = _parse_int(os.getenv('BACKUP_ANALYSIS_CHANNEL_ID', ''))
    SCRIM_CHANNEL_ID: int = _parse_int(os.getenv('SCRIM_CHANNEL_ID', '1212383364258992128'))
    LOG_CHANNEL_ID: int = _parse_int(os.getenv('LOG_CHANNEL_ID', '1487384132035022961'))
    SCHEDULE_CHANNEL_ID: int = _parse_int(os.getenv('SCHEDULE_CHANNEL_ID', '1485653533637476512'))

    GROUP_CHANNEL_IDS: dict = _parse_group_channel_ids(os.getenv('GROUP_CHANNEL_IDS', ''))

    # {letter}가 조 문자(A, B, C, ...)로 대체되어 음성채널을 동적으로 찾는 데 쓰입니다.
    GROUP_CATEGORY_PATTERN: str = os.getenv('GROUP_CATEGORY_PATTERN', 'Group {letter}')

    TEAMS_PER_GROUP: int = 8
    EMBED_FOOTER_TEXT: str = os.getenv('EMBED_FOOTER_TEXT', 'ER Scrim | Powered by Mongsil')
    AUTO_ASSIGNMENT_CHECK_INTERVAL: int = 30

    TEAM_REGISTRATION_DEADLINE_HOUR: int = 17
    SCRIM_START_HOUR: int = 20
    NEXT_SCRIM_OPEN_HOUR: int = 22
    TOTAL_ROUNDS: int = 4

    MMR_UPDATE_INTERVAL_SECONDS: int = 300
    MMR_UPDATE_MAINTENANCE_INTERVAL_SECONDS: int = 600

    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE: str = os.getenv('LOG_FILE', 'scrimbot.log')

    ANNOUNCEMENT_MESSAGE: str = os.getenv('ANNOUNCEMENT_MESSAGE', '')
    # 공휴일 사용자 설정 대전 가이드 링크 (공지사항 하단에 표시)
    CUSTOM_GAME_GUIDE_LINK: str = (
        "[사용자 설정 대전 가이드]"
        "(https://www.notion.so/mongsildev/30125b3fe9fb8082b0e4f286d2f45512?source=copy_link)"
    )

    GOOGLE_SHEETS_CREDENTIALS_PATH: str = os.getenv(
        'GOOGLE_SHEETS_CREDENTIALS_PATH',
        'credentials/google_sheets_credentials.json'
    )
    # 시드팀, 테스트 계정, 패널티 공통 사용
    GOOGLE_SHEETS_MAIN_SPREADSHEET_ID: str = os.getenv(
        'GOOGLE_SHEETS_MAIN_SPREADSHEET_ID', ''
    )
    GOOGLE_SHEETS_WARNING_WORKSHEET_NAME: str = '패널티'
    GOOGLE_SHEETS_WARNING_LOG_WORKSHEET_NAME: str = '패널티로그'
    GOOGLE_SHEETS_SEEDS_WORKSHEET_NAME: str = '시드팀'
    GOOGLE_SHEETS_TEST_ACCOUNTS_WORKSHEET_NAME: str = '테스트'
    # 마지막 처리 날짜 기록
    MASTERS_STATE_PATH: str = os.getenv('MASTERS_STATE_PATH', 'data/masters_state.json')

    @classmethod
    def validate(cls) -> tuple[bool, list[str]]:
        errors: list[str] = []

        required_str_vars = {
            'DISCORD_TOKEN': cls.DISCORD_TOKEN,
            'BSER_API_KEY': cls.BSER_API_KEY,
            'GOOGLE_SHEETS_MAIN_SPREADSHEET_ID': cls.GOOGLE_SHEETS_MAIN_SPREADSHEET_ID,
        }

        for name, value in required_str_vars.items():
            if not value:
                errors.append(f"환경변수 '{name}'이(가) 설정되지 않았습니다")

        # 0이면 미설정
        required_int_vars = {
            'GUILD_ID': cls.GUILD_ID,
            'NOTICE_CHANNEL_ID': cls.NOTICE_CHANNEL_ID,
            'BACKUP_ANALYSIS_CHANNEL_ID': cls.BACKUP_ANALYSIS_CHANNEL_ID,
        }

        for name, value in required_int_vars.items():
            if not value:
                errors.append(f"환경변수 '{name}'이(가) 설정되지 않았습니다")

        if not cls.ADMIN_ROLE_IDS:
            errors.append("환경변수 'ADMIN_ROLE_IDS'이(가) 설정되지 않았습니다")
        if not cls.GROUP_CHANNEL_IDS:
            errors.append("환경변수 'GROUP_CHANNEL_IDS'이(가) 설정되지 않았습니다")

        if cls.GOOGLE_SHEETS_CREDENTIALS_PATH:
            if not os.path.exists(cls.GOOGLE_SHEETS_CREDENTIALS_PATH):
                errors.append(
                    f"Google Sheets 인증 파일을 찾을 수 없습니다: {cls.GOOGLE_SHEETS_CREDENTIALS_PATH}"
                )

        return (len(errors) == 0, errors)


settings = Settings()
