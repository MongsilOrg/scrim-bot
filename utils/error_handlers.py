"""
에러 핸들링 유틸리티 모듈
"""
import asyncio
import functools
from typing import Any, Callable, Optional, Tuple, Type

import aiohttp
import discord

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


def safe_execute(
    func: Callable,
    *args,
    default_return: Any = None,
    log_errors: bool = True,
    **kwargs
) -> Any:
    """
    안전한 함수 실행 (동기)
    
    Args:
        func: 실행할 함수
        *args: 함수 인자
        default_return: 에러 발생 시 반환할 기본값
        log_errors: 에러 로깅 여부
        **kwargs: 함수 키워드 인자
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        if log_errors:
            logger.error(f"{func.__name__} 실행 실패: {e}", exc_info=True)
        return default_return


async def safe_execute_async(
    func: Callable,
    *args,
    default_return: Any = None,
    log_errors: bool = True,
    **kwargs
) -> Any:
    """
    안전한 비동기 함수 실행
    
    Args:
        func: 실행할 함수
        *args: 함수 인자
        default_return: 에러 발생 시 반환할 기본값
        log_errors: 에러 로깅 여부
        **kwargs: 함수 키워드 인자
    """
    try:
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        else:
            return func(*args, **kwargs)
    except Exception as e:
        if log_errors:
            logger.error(f"{func.__name__} 실행 실패: {e}", exc_info=True)
        return default_return


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


async def retry_with_exponential_backoff(
    func: Callable,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_failure: Optional[Callable] = None
) -> Any:
    """
    지수 백오프를 사용한 재시도 로직
    
    Args:
        func: 실행할 비동기 함수
        max_retries: 최대 재시도 횟수
        initial_delay: 초기 지연 시간 (초)
        backoff_factor: 지수 백오프 배수
        exceptions: 재시도할 예외 타입들
        on_failure: 실패 시 실행할 콜백 함수
    
    Returns:
        함수 실행 결과 또는 None
    """
    delay = initial_delay
    
    for attempt in range(max_retries):
        try:
            return await func()
        except exceptions as e:
            if attempt < max_retries - 1:
                logger.warning(
                    f"{func.__name__ if hasattr(func, '__name__') else '함수'} 실행 중 오류 발생 "
                    f"(시도 {attempt + 1}/{max_retries}): {e}. {delay:.1f}초 후 재시도..."
                )
                await asyncio.sleep(delay)
                delay *= backoff_factor
                continue
            else:
                logger.error(
                    f"{func.__name__ if hasattr(func, '__name__') else '함수'} 실행 최종 실패 "
                    f"(최대 재시도 횟수 초과): {e}"
                )
                if on_failure:
                    try:
                        return await on_failure()
                    except Exception as fallback_error:
                        logger.error(f"실패 콜백 실행 실패: {fallback_error}", exc_info=True)
                return None
        except Exception as e:
            # 재시도하지 않을 예외는 즉시 전파
            logger.error(f"예상치 못한 오류: {e}", exc_info=True)
            raise
    
    return None
