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
    """비숫자 태그가 하나라도 있으면 대회 행이다."""
    return any(not _is_number(name) for name in tag_names)


def _tag_names(props) -> List[str]:
    return [tag["name"] for tag in props.get("태그", {}).get("multi_select", [])]


# 1. 설정 정보
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
    """Notion DB 쿼리. 페이지당 최대 100건이라 커서로 전부 순회한다."""
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
    # 날짜가 있는 행만 조회
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
    """대회 행(비숫자 태그 포함)의 날짜만 집계하며 KEL 등 미관리 대회는 제외합니다.

    조회 실패 시 빈 집합 대신 예외를 던져 호출부가 재시도하게 합니다.
    """
    payload = {
        "filter": {
            "and": [
                # Notion date 필터는 시작일 기준. 다일 행 대비 여유 30일
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


# get_server_info TTL 캐시 (5분 주기 MMR 루프가 사이클마다 Notion을 때리지 않도록)
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
    """서버 타입 및 송출 정보 dict.

    조회 실패 시 만료된 캐시라도 있으면 그것을, 없으면 Live 기본값을 반환하므로
    호출부는 예외 처리 없이 써도 된다. 실패 시 캐시를 갱신하지 않아 다음 호출에 재시도한다.
    """
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

