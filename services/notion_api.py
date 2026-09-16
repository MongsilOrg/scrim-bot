import os
import re
import time
import requests

from datetime import date, datetime, timedelta
from typing import List, Optional, Set

from config.logging_config import get_logger
from utils.helpers import effective_scrim_date, get_current_kst_time

logger = get_logger('notion_api')


def _is_number(tag: str) -> bool:
    return re.match(r"^[0-9]+(\.[0-9]+)?$", tag) is not None


def _is_tournament_row(tag_names) -> bool:
    return any(not _is_number(name) for name in tag_names)


def _tag_names(props) -> List[str]:
    return [tag["name"] for tag in props.get("태그", {}).get("multi_select", [])]


NOTION_TOKEN: str = os.getenv('NOTION_TOKEN', '')
NOTION_DATABASE_ID: str = os.getenv('NOTION_DATABASE_ID', '')
UNMANAGED_TOURNAMENT_TAGS = {"KEL"}

NOTION_QUERY_URL = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

def get_date_data(data):
    props = data.get("properties", {})

    date_prop = props.get("날짜", {}).get("date")
            
    start_str = date_prop.get("start")
    end_str = date_prop.get("end") or start_str

    start_date = datetime.strptime(start_str[:10], "%Y-%m-%d").date()
    end_date = datetime.strptime(end_str[:10], "%Y-%m-%d").date()

    return start_date, end_date, props

def _query_database(payload: dict) -> list:
    """Notion 쿼리 응답은 페이지당 최대 100건."""
    results = []
    start_cursor = None
    while True:
        body = {**payload, "start_cursor": start_cursor} if start_cursor else payload
        res = requests.post(NOTION_QUERY_URL, headers=NOTION_HEADERS, json=body, timeout=10)
        res.raise_for_status()
        data = res.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")
    return results


def get_notion_data():
    payload = {
        "filter": {
            "property": "날짜",
            "date": { "is_not_empty": True }
        }
    }
    return _query_database(payload)

def _decide(results, today, tomorrow):
    count = 0
    broadcast = True
    tournament_today = False
    live_versions = []
    next_masters_start = None
    next_masters_versions = []

    for result in results:
        if "properties" not in result:
            continue

        start_date, end_date, props = get_date_data(result)
        tag_names = _tag_names(props)

        if any(name in UNMANAGED_TOURNAMENT_TAGS for name in tag_names):
            if start_date <= today <= end_date:
                tournament_today = True
            continue

        numbers = [float(name) for name in tag_names if _is_number(name)]
        is_tournament_row = _is_tournament_row(tag_names)

        if is_tournament_row:
            if start_date <= today <= end_date:
                tournament_today = True
            elif start_date > today:
                if next_masters_start is None or start_date < next_masters_start:
                    next_masters_start = start_date
                    next_masters_versions = list(numbers)
                elif start_date == next_masters_start:
                    next_masters_versions.extend(numbers)
        elif start_date <= today <= end_date:
            live_versions.extend(numbers)

        if start_date <= tomorrow <= end_date:
            count += len(tag_names)
            if count > 1:
                broadcast = False

    if tournament_today:
        return [False, broadcast]

    if live_versions and next_masters_versions:
        if min(next_masters_versions) < max(live_versions):
            return [True, broadcast]

    return [False, broadcast]


def check_notion_for_tags():
    now = get_current_kst_time()
    today = effective_scrim_date(now)
    tomorrow = (now + timedelta(days=1)).date()

    return _decide(get_notion_data(), today, tomorrow)


def get_masters_dates(range_start: date, range_end: date) -> Set[date]:
    """조회 실패는 빈 집합 대신 예외, 재시도는 호출부 몫."""
    payload = {
        "filter": {
            "and": [
                # Notion date 필터는 시작일 기준이라 여러 날에 걸친 행 대비 30일 여유
                { "property": "날짜", "date": { "is_not_empty": True } },
                { "property": "날짜", "date": { "on_or_after": (range_start - timedelta(days=30)).isoformat() } },
                { "property": "날짜", "date": { "on_or_before": range_end.isoformat() } },
            ]
        }
    }

    results = _query_database(payload)

    days: Set[date] = set()
    for result in results:
        if "properties" not in result:
            continue

        start_date, end_date, props = get_date_data(result)
        tag_names = _tag_names(props)

        if not tag_names:
            continue
        if any(name in UNMANAGED_TOURNAMENT_TAGS for name in tag_names):
            continue
        if not _is_tournament_row(tag_names):
            continue

        d = max(start_date, range_start)
        last = min(end_date, range_end)
        while d <= last:
            days.add(d)
            d += timedelta(days=1)

    return days


_SERVER_INFO_TTL_SECONDS = 300
_server_info_cache: Optional[dict] = None
_server_info_cached_at: float = 0.0


def _build_server_info(is_tournament: bool, broadcast: bool) -> dict:
    server_emoji = "🟠" if is_tournament else "🟢"
    server_type = "Tournament" if is_tournament else "Live"
    broadcast_emoji = "📡" if broadcast else "🚫"
    broadcast_status = "송출 가능" if broadcast else "송출 불가"
    operate = f"{server_emoji} {server_type} 서버 | {broadcast_emoji} {broadcast_status}"

    return {
        'is_tournament': is_tournament,
        'broadcast': broadcast,
        'server_emoji': server_emoji,
        'server_type': server_type,
        'broadcast_emoji': broadcast_emoji,
        'broadcast_status': broadcast_status,
        'operate': operate,
    }


def get_server_info() -> dict:
    """예외 없음, 조회 실패 시 만료 캐시나 Live 기본값 반환."""
    global _server_info_cache, _server_info_cached_at

    now = time.monotonic()
    if _server_info_cache is not None and (now - _server_info_cached_at) < _SERVER_INFO_TTL_SECONDS:
        return _server_info_cache

    try:
        [is_tournament, broadcast] = check_notion_for_tags()
    except Exception as e:
        if _server_info_cache is not None:
            logger.warning(f"[노션] 서버 정보 조회 실패, 만료된 캐시 사용: {e}")
            return _server_info_cache
        logger.error(f"[노션] 서버 정보 조회 실패, Live 기본값 사용: {e}", exc_info=True)
        return _build_server_info(False, True)

    info = _build_server_info(is_tournament, broadcast)
    _server_info_cache = info
    _server_info_cached_at = now
    return info

