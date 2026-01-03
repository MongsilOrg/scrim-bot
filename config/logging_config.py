"""
로깅 설정 모듈
일관된 로깅 시스템을 제공합니다.
"""
import logging
import os
from datetime import datetime
from typing import Optional

import pytz


class KSTFormatter(logging.Formatter):
    """KST 시간대를 사용하는 커스텀 포맷터"""
    
    def __init__(self, fmt=None, datefmt=None):
        super().__init__(fmt, datefmt)
        self.kst = pytz.timezone('Asia/Seoul')
    
    def formatTime(self, record, datefmt=None):
        """KST 시간으로 포맷팅"""
        dt = datetime.fromtimestamp(record.created, tz=self.kst)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S')


class ScrimbotLogger:
    """스크림봇 전용 로거 클래스"""
    
    _loggers = {}
    _initialized = False
    
    @classmethod
    def setup_logging(cls, log_level: str = "INFO", log_file: str = "scrimbot.log") -> None:
        """로깅 시스템 초기화"""
        if cls._initialized:
            return
            
        # 로그 레벨 설정
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
        
        # 파일 핸들러 설정
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        
        # 콘솔 핸들러 설정
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        
        # 루트 로거 설정
        root_logger = logging.getLogger()
        root_logger.setLevel(level)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        
        cls._initialized = True
    
    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        """모듈별 로거 반환"""
        if not cls._initialized:
            cls.setup_logging()
        
        if name not in cls._loggers:
            logger = logging.getLogger(f'scrimbot.{name}')
            cls._loggers[name] = logger
        
        return cls._loggers[name]


def get_logger(name: str) -> logging.Logger:
    """편의 함수: 모듈별 로거 반환"""
    return ScrimbotLogger.get_logger(name)


# 로깅 레벨 상수
class LogLevel:
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
