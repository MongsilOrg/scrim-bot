import asyncio
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple, Union

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
# 정규 시즌 뒤에는 늘 1주 프리시즌이 왔음. 다음 행이 늦게 생겨도 이 기간은 프리시즌으로 봄
PRESEASON_GAP = timedelta(days=7)

def _rank_switch_at(start: datetime) -> datetime:
    # 새 정규 시즌은 시작 뒤 첫 월요일 스크림 신청(일요일 밤에 열림) 전, 그 일요일 12시부터 랭크 MMR 기준으로 씀
    sunday = (start + timedelta(days=(6 - start.weekday()) % 7)).replace(hour=12, minute=0, second=0, microsecond=0)
    return sunday if sunday > start else sunday + timedelta(days=7)
# 시즌 목록은 하루 한 번만 받음. 목록이 그 시각을 덮지 못하면(새 시즌 행이 아직 없음) 10분 간격으로 다시 받음
_season_cache: dict = {'day': None, 'tried_at': None, 'seasons': []}
_season_lock = asyncio.Lock()


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
    return _parse_seasons(rows)


def _parse_seasons(rows: list) -> list:
    seasons = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        start = _parse_season_time(row.get('seasonStart'))
        if start:
            seasons.append((str(row.get('seasonName', '')), start, _parse_season_time(row.get('seasonEnd')),
                            row.get('seasonID')))
    seasons.sort(key=lambda s: s[1])
    # 끝 시각이 비어 온 행은 다음 행의 시작까지, 마지막 행이면 끝 없이 다룸
    return [(name, start, end or (seasons[i + 1][1] if i + 1 < len(seasons) else None), season_id)
            for i, (name, start, end, season_id) in enumerate(seasons)]


def _season_match(seasons: list, at: datetime) -> Optional[tuple]:
    for season in seasons:
        start, end = season[1], season[2]
        if start <= at and (end is None or at < end):
            return season
    return None


def _season_at(seasons: list, at: datetime) -> Optional[str]:
    match = _season_match(seasons, at)
    if match:
        return match[0]
    prev = max((s for s in seasons if s[2] and s[2] <= at), key=lambda s: s[2], default=None)
    if prev and prev[0].lower().startswith('season') and at < prev[2] + PRESEASON_GAP:
        return "Pre-Season"
    return None


async def _load_seasons(at: datetime) -> list:
    cache = _season_cache
    # 조회 중에 들어온 호출은 빈 캐시 대신 조회 결과를 기다림
    async with _season_lock:
        now = get_current_kst_time()
        match = _season_match(cache['seasons'], at)
        # 끝 시각이 없는 구간은 다음 시즌 행이 늦게 생길 수 있어 확정으로 보지 않음
        stale = cache['day'] != now.date() or match is None or match[2] is None
        if stale and (cache['tried_at'] is None or now - cache['tried_at'] >= SEASON_RETRY):
            cache['tried_at'] = now
            seasons = await _fetch_seasons()
            if seasons is not None:
                cache['day'] = now.date()
                cache['seasons'] = seasons
    return cache['seasons']


async def is_preseason(target_date: Optional[Union[date, datetime]] = None) -> bool:
    """그날 스크림 시작 시각이 프리시즌 기간이면 True. 조회가 안 되면 False(평일 방식)"""
    if not settings.BSER_API_KEY:
        return False
    at = datetime.combine(_to_date(target_date), time(settings.SCRIM_START_HOUR), tzinfo=KST)
    name = _season_at(await _load_seasons(at), at)
    return bool(name and name.lower().startswith('pre-season'))


async def rank_season_ids() -> Optional[Tuple[int, Optional[int]]]:
    """(랭크 MMR에 쓸 정규 시즌 ID, 그 직전 정규 시즌 ID). 시즌 목록을 받은 적이 없으면 None"""
    if not settings.BSER_API_KEY:
        return None
    now = get_current_kst_time()
    seasons = await _load_seasons(now)
    ids = [s[3] for s in seasons
           if len(s) > 3 and isinstance(s[3], int) and s[0].lower().startswith('season')
           and _rank_switch_at(s[1]) <= now]
    if not ids:
        return None
    return ids[-1], (ids[-2] if len(ids) > 1 else None)


async def regular_season_start() -> Optional[datetime]:
    """지금 진행 중이거나 가장 최근에 시작한 정규 시즌의 시작 시각. 시즌 목록을 받은 적이 없으면 None"""
    if not settings.BSER_API_KEY:
        return None
    now = get_current_kst_time()
    starts = [s[1] for s in await _load_seasons(now) if s[0].lower().startswith('season') and s[1] <= now]
    return starts[-1] if starts else None


async def get_rest_day_info(target_date: Optional[Union[date, datetime]] = None) -> dict:
    target = _to_date(target_date)

    is_saturday = target.weekday() == 5
    is_sunday = target.weekday() == 6
    holiday_names = await get_holiday_names(target)
    is_holiday = bool(holiday_names)
    try:
        preseason = await is_preseason(target)
    except Exception as e:
        # 프리시즌 판정이 깨져도 주말과 공휴일 판정은 유지
        logger.warning(f"[시즌] 프리시즌 판정 실패, 평일 방식 사용: {type(e).__name__}")
        preseason = False

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


