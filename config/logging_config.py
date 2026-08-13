"""
로깅 설정 모듈
일관된 로깅 시스템을 제공합니다.
"""
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

from config.settings import settings


class KSTFormatter(logging.Formatter):
    """KST 시간대를 사용하는 커스텀 포맷터"""

    def formatTime(self, record, datefmt=None):
        # helpers→validators→logging_config 순환 때문에 top-level import 불가
        from utils.helpers import KST
        dt = datetime.fromtimestamp(record.created, tz=KST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S')


class ScrimbotLogger:
    """스크림봇 전용 로거 클래스"""

    _initialized = False

    @classmethod
    def setup_logging(cls, log_level: str = settings.LOG_LEVEL, log_file: str = settings.LOG_FILE) -> None:
        if cls._initialized:
            return

        level = getattr(logging, log_level.upper(), logging.INFO)
        
        # 로그 포맷 설정 (KST 시간대 사용)
        formatter = KSTFormatter(
            '%(asctime)s | %(name)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 로그 파일 경로 준비
        if log_file:
            log_dir = os.path.dirname(log_file)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
        
        # 파일 핸들러 설정 (10MB당 로테이션, 최대 5개 백업)
        file_handler = RotatingFileHandler(
            log_file, encoding='utf-8',
            maxBytes=10 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)

        root_logger = logging.getLogger()
        root_logger.setLevel(level)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)

        # 외부 라이브러리 로거 레벨 제한 (불필요한 로그 억제)
        for lib_logger_name in ('discord', 'discord.http', 'discord.gateway', 'aiohttp', 'asyncio'):
            logging.getLogger(lib_logger_name).setLevel(logging.ERROR)

        cls._initialized = True

    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        if not cls._initialized:
            cls.setup_logging()
        return logging.getLogger(f'scrimbot.{name}')


def get_logger(name: str) -> logging.Logger:
    return ScrimbotLogger.get_logger(name)
