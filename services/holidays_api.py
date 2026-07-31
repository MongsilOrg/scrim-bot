import asyncio
from datetime import date, datetime
from typing import Dict, List, Optional, Union

import aiohttp

from config.logging_config import get_logger
from utils.helpers import get_current_kst_time

logger = get_logger('holidays_api')

BASE_URL = "https://holidays.hyunbin.page"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

_holiday_cache: Dict[int, Dict[str, List[str]]] = {}


def _to_date(value: Optional[Union[date, datetime]]) -> date:
    if value is None:
        return get_current_kst_time().date()
    if isinstance(value, datetime):
        return value.date()
    return value


async def fetch_holidays(
    year: Optional[int] = None,
    *,
    force_refresh: bool = False,
) -> Dict[str, List[str]]:
    if year is None:
        year = get_current_kst_time().year

    if not force_refresh and year in _holiday_cache:
        return _holiday_cache[year]

    url = f"{BASE_URL}/{year}.json"
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error(f"[공휴일] {year}년 공휴일 조회 실패 - {url}: {e}")
        return _holiday_cache.get(year, {})
    except Exception as e:
        # 200이지만 JSON이 아닌 응답 등 그 외 예외도 흡수 (호출부 흐름을 막지 않도록)
        logger.error(f"[공휴일] {year}년 공휴일 처리 중 예외 - {url}: {e}", exc_info=True)
        return _holiday_cache.get(year, {})

    if not isinstance(data, dict):
        logger.error(f"[공휴일] {year}년 응답 형식이 올바르지 않습니다: {type(data)}")
        return _holiday_cache.get(year, {})

    _holiday_cache[year] = data
    logger.info(f"[공휴일] {year}년 공휴일 {len(data)}일 로드 완료")
    return data


async def is_holiday(target_date: Optional[Union[date, datetime]] = None) -> bool:
    target = _to_date(target_date)
    holidays = await fetch_holidays(target.year)
    return target.isoformat() in holidays


async def get_holiday_names(
    target_date: Optional[Union[date, datetime]] = None,
) -> List[str]:
    target = _to_date(target_date)
    holidays = await fetch_holidays(target.year)
    return holidays.get(target.isoformat(), [])


