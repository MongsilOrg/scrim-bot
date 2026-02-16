"""
스크림봇 설정 모듈
환경변수 및 기본 설정을 관리합니다.
"""
import os
from typing import Set

import pytz
from dotenv import load_dotenv

# 환경변수 로드
load_dotenv()


class Settings:
    """스크림봇 설정 클래스"""
    
    # Discord 설정
    DISCORD_TOKEN: str = os.getenv('DISCORD_TOKEN', '')
    GUILD_ID: int = int(os.getenv('GUILD_ID', '1035508677689475092'))
    ADMIN_ROLE_IDS: Set[int] = set(
        int(id_) for id_ in os.getenv('ADMIN_ROLE_IDS', '1035511183073099777,1178295996862713916').split(',')
    )
    
    # BSER API 설정
    BSER_API_KEY: str = os.getenv('BSER_API_KEY', '')
    
    # 채널 ID 설정
    NOTICE_CHANNEL_ID: int = int(os.getenv('NOTICE_CHANNEL_ID', '1173422674626748417'))
    TEAM_ASSIGNMENT_CHANNEL_ID: int = int(os.getenv('TEAM_ASSIGNMENT_CHANNEL_ID', '1212383364258992128'))
    AUTO_ASSIGNMENT_START_CHANNEL_ID: int = int(os.getenv('AUTO_ASSIGNMENT_START_CHANNEL_ID', '1390999095962767380'))
    TEAM_LIST_CHANNEL_ID: int = int(os.getenv('TEAM_LIST_CHANNEL_ID', '1390999095962767380'))
    BACKUP_ANALYSIS_CHANNEL_ID: int = int(os.getenv('BACKUP_ANALYSIS_CHANNEL_ID', '1400785133489098842'))

    # 조별 채널 ID
    _group_channel_defaults = 'A:1337238342730776668,B:1337238366667669595,C:1337238442605543455,D:1337238460905553951,E:1337238477879906397,F:1337238497408585779'
    GROUP_CHANNEL_IDS = {
        pair.split(':')[0]: int(pair.split(':')[1])
        for pair in os.getenv('GROUP_CHANNEL_IDS', _group_channel_defaults).split(',')
    }
    
    # 조별 카테고리 이름 패턴 (동적으로 음성채널을 찾기 위해 사용)
    GROUP_CATEGORY_PATTERNS = {
        'A': 'Group A',
        'B': 'Group B', 
        'C': 'Group C',
        'D': 'Group D',
        'E': 'Group E',
        'F': 'Group F',
        'G': 'Group G',
        'H': 'Group H'
    }
    
    # 상수 설정
    TEAMS_PER_GROUP: int = 8
    THUMBNAIL_URL: str = "https://mongsil.dev/w/src/Scrim.jpg"
    KST_TIMEZONE = pytz.timezone('Asia/Seoul')
    AUTO_ASSIGNMENT_CHECK_INTERVAL: int = 30  # seconds
    
    # 로깅 설정
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE: str = os.getenv('LOG_FILE', 'scrimbot.log')
    
    # 공지사항 설정
    ANNOUNCEMENT_MESSAGE: str = "['25 H2 Cal](https://www.notion.so/211ec9fb976380edb7f7d4c58a6da80a?v=211ec9fb976380458a04000c79c39aa3)"
    
    # 구글 시트 설정
    GOOGLE_SHEETS_CREDENTIALS_PATH: str = os.getenv(
        'GOOGLE_SHEETS_CREDENTIALS_PATH',
        'credentials/google_sheets_credentials.json'
    )
    # 메인 스프레드시트 ID (시드팀, 테스트 계정, 패널티 공통)
    GOOGLE_SHEETS_MAIN_SPREADSHEET_ID: str = os.getenv(
        'GOOGLE_SHEETS_MAIN_SPREADSHEET_ID',
        'REDACTED-SHEET-ID'
    )
    # 패널티 시트 설정 (레거시 호환성을 위해 유지)
    GOOGLE_SHEETS_WARNING_SPREADSHEET_ID: str = os.getenv(
        'GOOGLE_SHEETS_WARNING_SPREADSHEET_ID',
        'REDACTED-SHEET-ID'
    )
    GOOGLE_SHEETS_WARNING_WORKSHEET_NAME: str = '패널티'
    # 경고로그 시트 설정 (외부 공개용 - 영구 보관)
    GOOGLE_SHEETS_WARNING_LOG_WORKSHEET_NAME: str = '패널티로그'
    # 시드팀 시트 설정
    GOOGLE_SHEETS_SEEDS_WORKSHEET_NAME: str = '시드팀'
    # 테스트 계정 시트 설정
    GOOGLE_SHEETS_TEST_ACCOUNTS_WORKSHEET_NAME: str = '테스트'
    
    @classmethod
    def validate(cls) -> bool:
        """필수 설정값 검증"""
        required_settings = [
            cls.DISCORD_TOKEN,
            cls.BSER_API_KEY,
            cls.GUILD_ID,
            cls.GOOGLE_SHEETS_MAIN_SPREADSHEET_ID,
            cls.GOOGLE_SHEETS_WARNING_SPREADSHEET_ID,
        ]
        
        for setting in required_settings:
            if not setting:
                return False
        
        # 인증 파일 존재 여부 확인
        if cls.GOOGLE_SHEETS_CREDENTIALS_PATH:
            if not os.path.exists(cls.GOOGLE_SHEETS_CREDENTIALS_PATH):
                return False

        return True


# 전역 설정 인스턴스
settings = Settings()
