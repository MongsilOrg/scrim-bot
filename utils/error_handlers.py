"""
에러 핸들링 유틸리티 모듈
"""
import asyncio
import functools
from typing import Any, Callable, Optional

from config.logging_config import get_logger

logger = get_logger('error_handlers')


def handle_errors(
    default_return: Any = None,
    log_level: str = 'error',
    reraise: bool = False,
    max_retries: int = 0,
    retry_delay: float = 1.0
):
    """
    에러 핸들링 데코레이터
    
    Args:
        default_return: 에러 발생 시 반환할 기본값
        log_level: 로그 레벨 ('debug', 'info', 'warning', 'error')
        reraise: 에러를 다시 발생시킬지 여부
        max_retries: 최대 재시도 횟수
        retry_delay: 재시도 간격 (초)
    """
    def decorator(func: Callable) -> Callable:
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                retries = 0
                while retries <= max_retries:
                    try:
                        return await func(*args, **kwargs)
                    except Exception as e:
                        retries += 1
                        error_msg = f"{func.__name__} 실행 중 오류 발생 (시도 {retries}/{max_retries + 1}): {e}"
                        
                        # 로그 레벨에 따른 로깅
                        if log_level == 'debug':
                            logger.debug(error_msg)
                        elif log_level == 'info':
                            logger.info(error_msg)
                        elif log_level == 'warning':
                            logger.warning(error_msg)
                        else:  # error
                            logger.error(error_msg, exc_info=True)
                        
                        # 마지막 시도가 아니면 재시도
                        if retries <= max_retries:
                            await asyncio.sleep(retry_delay)
                            continue
                        
                        # 재시도 실패 시 처리
                        if reraise:
                            raise
                        return default_return
                
                return default_return
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                retries = 0
                while retries <= max_retries:
                    try:
                        return func(*args, **kwargs)
                    except Exception as e:
                        retries += 1
                        error_msg = f"{func.__name__} 실행 중 오류 발생 (시도 {retries}/{max_retries + 1}): {e}"
                        
                        # 로그 레벨에 따른 로깅
                        if log_level == 'debug':
                            logger.debug(error_msg)
                        elif log_level == 'info':
                            logger.info(error_msg)
                        elif log_level == 'warning':
                            logger.warning(error_msg)
                        else:  # error
                            logger.error(error_msg, exc_info=True)
                        
                        # 마지막 시도가 아니면 재시도
                        if retries <= max_retries:
                            import time
                            time.sleep(retry_delay)
                            continue
                        
                        # 재시도 실패 시 처리
                        if reraise:
                            raise
                        return default_return
                
                return default_return
            return sync_wrapper
    return decorator


class ErrorContext:
    """에러 컨텍스트 매니저"""
    
    def __init__(
        self,
        default_return: Any = None,
        log_errors: bool = True,
        log_level: str = 'error'
    ):
        self.default_return = default_return
        self.log_errors = log_errors
        self.log_level = log_level
        self.error_occurred = False
        self.last_error: Optional[Exception] = None
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.error_occurred = True
            self.last_error = exc_val
            
            if self.log_errors:
                error_msg = f"컨텍스트 내에서 오류 발생: {exc_val}"
                if self.log_level == 'debug':
                    logger.debug(error_msg)
                elif self.log_level == 'info':
                    logger.info(error_msg)
                elif self.log_level == 'warning':
                    logger.warning(error_msg)
                else:  # error
                    logger.error(error_msg, exc_info=True)
            
            # 예외를 억제하고 기본값 반환
            return True
        
        return False
