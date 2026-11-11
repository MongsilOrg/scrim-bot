import asyncio
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Union

import aiohttp

from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import KST, get_current_kst_time

logger = get_logger('holidays_api')

BASE_URL = "https://holidays.hyunbin.page"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

_holiday_cache: Dict[int, Dict[str, List[str]]] = {}

SEASON_URL = "https://open-api.bser.io/v2/data/Season"
SEASON_RETRY = timedelta(minutes=10)
# 시즌 목록은 하루 한 번만 받음. 목록이 그 시각을 덮지 못하면(새 시즌 행이 아직 없음) 10분 간격으로 다시 받음
_season_cache: dict = {'day': None, 'tried_at': None, 'seasons': []}


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
        logger.error(f"[공휴일] {year}년 공휴일 조회 실패 - {url}: {e}", exc_info=True)
        return _holiday_cache.get(year, {})
    except Exception as e:
        logger.error(f"[공휴일] {year}년 공휴일 처리 중 예외 - {url}: {e}", exc_info=True)
        return _holiday_cache.get(year, {})

    if not isinstance(data, dict):
        logger.error(f"[공휴일] {year}년 응답 형식이 올바르지 않습니다: {type(data)}")
        return _holiday_cache.get(year, {})

    _holiday_cache[year] = data
    logger.info(f"[공휴일] {year}년 공휴일 {len(data)}일 로드 완료")
    return data


async def get_holiday_names(
    target_date: Optional[Union[date, datetime]] = None,
) -> List[str]:
    target = _to_date(target_date)
    holidays = await fetch_holidays(target.year)
    return holidays.get(target.isoformat(), [])


def _parse_season_time(value) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except (TypeError, ValueError):
        return None


async def _fetch_seasons() -> Optional[list]:
    headers = {'x-api-key': settings.BSER_API_KEY, 'User-Agent': 'agent-local'}
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, headers=headers) as session:
            async with session.get(SEASON_URL) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        logger.warning(f"[시즌] 시즌 목록 조회 실패, 프리시즌 판정 생략: {type(e).__name__}")
        return None
    rows = data.get('data') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        logger.warning("[시즌] 시즌 목록 응답 형식이 다름, 프리시즌 판정 생략")
        return None
    seasons = []
    for row in rows:
        start = _parse_season_time(row.get('seasonStart'))
        end = _parse_season_time(row.get('seasonEnd'))
        if start and end:
            seasons.append((str(row.get('seasonName', '')), start, end))
    return seasons


def _season_at(seasons: list, at: datetime) -> Optional[str]:
    for name, start, end in seasons:
        if start <= at < end:
            return name
    return None


async def is_preseason(target_date: Optional[Union[date, datetime]] = None) -> bool:
    """그날 스크림 시작 시각이 프리시즌 기간이면 True. 조회가 안 되면 False(평일 방식)"""
    if not settings.BSER_API_KEY:
        return False
    at = datetime.combine(_to_date(target_date), time(settings.SCRIM_START_HOUR), tzinfo=KST)
    now = get_current_kst_time()
    cache = _season_cache
    stale = cache['day'] != now.date() or _season_at(cache['seasons'], at) is None
    if stale and (cache['tried_at'] is None or now - cache['tried_at'] >= SEASON_RETRY):
        cache['tried_at'] = now
        seasons = await _fetch_seasons()
        if seasons is not None:
            cache['day'] = now.date()
            cache['seasons'] = seasons
    name = _season_at(cache['seasons'], at)
    return bool(name and name.lower().startswith('pre-season'))


async def get_rest_day_info(target_date: Optional[Union[date, datetime]] = None) -> dict:
    target = _to_date(target_date)

    is_saturday = target.weekday() == 5
    is_sunday = target.weekday() == 6
    holiday_names = await get_holiday_names(target)
    is_holiday = bool(holiday_names)
    preseason = await is_preseason(target)

    labels = (
        (["프리시즌"] if preseason else [])
        + (["토요일"] if is_saturday else []) + (["일요일"] if is_sunday else []) + holiday_names
    )

    return {
        "date": target,
        "is_rest_day": is_saturday or is_sunday or is_holiday or preseason,
        "is_preseason": preseason,
        "is_saturday": is_saturday,
        "is_sunday": is_sunday,
        "is_holiday": is_holiday,
        "holiday_names": holiday_names,
        "labels": labels,
        "label": " / ".join(labels),
    }


