import os
import re
import requests

from datetime import datetime, timezone, timedelta


def _is_number(tag: str) -> bool:
    return re.match(r"^[0-9]+(\.[0-9]+)?$", tag) is not None


# 1. 설정 정보
NOTION_TOKEN: str = os.getenv('NOTION_TOKEN', '')
NOTION_DATABASE_ID: str = os.getenv('NOTION_DATABASE_ID', '')
UNMANAGED_TOURNAMENT_TAGS = {"KEL"}

def get_date_data(data):
    props = data.get("properties", {})

    date_prop = props.get("날짜", {}).get("date")
            
    start_str = date_prop.get("start")
    end_str = date_prop.get("end") or start_str

    start_date = datetime.strptime(start_str[:10], "%Y-%m-%d").date()
    end_date = datetime.strptime(end_str[:10], "%Y-%m-%d").date()

    return start_date, end_date, props

def get_notion_data():
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    
    # 이거 안쓰면 안감
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    # 날짜에있는 데이터만 가져오기 없으면 안가져옴
    payload = {
        "filter": {
            "property": "날짜",
            "date": { "is_not_empty": True }
        }
    }
    
    res = requests.post(url, headers=headers, json=payload)
    results = res.json().get("results", [])
    return results

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
        tag_names = [tag["name"] for tag in props.get("태그", {}).get("multi_select", [])]

        if any(name in UNMANAGED_TOURNAMENT_TAGS for name in tag_names):
            if start_date <= today <= end_date:
                tournament_today = True
            continue

        numbers = [float(name) for name in tag_names if _is_number(name)]
        is_tournament_row = len(numbers) != len(tag_names)

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
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    today = now.date() + timedelta(days=1) if now.hour >= 22 else now.date()
    tomorrow = (now + timedelta(days=1)).date()

    return _decide(get_notion_data(), today, tomorrow)


def get_server_info() -> dict:
    """서버 타입 및 송출 정보를 딕셔너리로 반환합니다.

    Returns:
        dict: {
            'is_tournament': bool,
            'broadcast': bool,
            'server_emoji': str,
            'server_type': str,
            'broadcast_emoji': str,
            'broadcast_status': str,
            'operate': str,  # 운영 정보 요약 문자열
        }
    """
    [is_tournament, broadcast] = check_notion_for_tags()
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

