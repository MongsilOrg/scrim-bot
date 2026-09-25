import logging
import os
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from config.settings import settings


class KSTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        # utils.helpers, validators, logging_config 순환 import
        from utils.helpers import KST
        dt = datetime.fromtimestamp(record.created, tz=KST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S')


class ScrimbotLogger:
    _initialized = False

    @classmethod
    def setup_logging(cls, log_level: str = settings.LOG_LEVEL, log_file: str = settings.LOG_FILE) -> None:
        if cls._initialized:
            return

        level = getattr(logging, log_level.upper(), logging.INFO)
        
        formatter = KSTFormatter(
            '%(asctime)s | %(levelname)-7s | %(name)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        if log_file:
            log_dir = os.path.dirname(log_file)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)

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

        for lib_logger_name in ('discord', 'discord.http', 'discord.gateway', 'aiohttp', 'asyncio'):
            logging.getLogger(lib_logger_name).setLevel(logging.ERROR)

        cls._initialized = True

    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        if not cls._initialized:
            cls.setup_logging()
        return logging.getLogger(f'scrim-bot.{name}')


def get_logger(name: str) -> logging.Logger:
    return ScrimbotLogger.get_logger(name)


_log_once_at: dict[str, float] = {}


def log_once(key: str, ttl: float = 1800) -> bool:
    now = time.monotonic()
    last = _log_once_at.get(key)
    if last is not None and now - last < ttl:
        return False
    if len(_log_once_at) > 2000:
        for k in [k for k, t in _log_once_at.items() if now - t >= ttl]:
            del _log_once_at[k]
    _log_once_at[key] = now
    return True
