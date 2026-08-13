"""
공통 유틸리티 함수 모듈
"""
import json
import os
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional

import discord
import pytz

from config.settings import settings
from utils.validators import member_name_keys, normalize_nickname_for_comparison

if TYPE_CHECKING:
    from models.team_data import TeamData


def is_admin(user: discord.Member) -> bool:
    return any(role.id in settings.ADMIN_ROLE_IDS for role in user.roles)


def get_team_members(team_data: 'TeamData') -> tuple:
    """팀 데이터에서 players, staff 리스트를 추출합니다."""
    return team_data.players, team_data.staff


def normalize_player_list(players: List[str]) -> List[str]:
    """플레이어 리스트 정규화. 닉네임 비교와 같은 규칙(공백 축약, 소문자)을 쓴다."""
    normalized = []
    for player in players or []:
        norm = normalize_nickname_for_comparison(player)
        if norm:
            normalized.append(norm)
    return normalized


def save_json_atomic(path: str, data, indent: Optional[int] = None) -> None:
    """임시 파일에 쓴 뒤 교체해 부분 쓰기를 방지합니다."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(tmp_path, path)


def build_member_lookup(guild: Optional[discord.Guild]) -> Dict[str, discord.Member]:
    """길드 멤버를 정규화 닉네임(표시명/전역명/계정명)으로 찾는 매핑을 만듭니다."""
    lookup: Dict[str, discord.Member] = {}
    if not guild:
        return lookup
    for member in guild.members:
        for key in member_name_keys(member):
            lookup[key] = member
    return lookup


# KST 타임존 단일 정의. 개별 모듈에서 pytz/timedelta로 재정의하지 말 것
KST = pytz.timezone('Asia/Seoul')


def get_current_kst_time() -> datetime:
    return datetime.now(KST)


def get_start_of_day_utc(now_kst: datetime = None) -> datetime:
    """KST 자정(당일 시작) 기준 시각을 UTC로 반환합니다."""
    if now_kst is None:
        now_kst = get_current_kst_time()
    start_of_day_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_of_day_kst.astimezone(pytz.utc)


def get_group_letter(channel_id: int) -> str | None:
    for letter, ch_id in settings.GROUP_CHANNEL_IDS.items():
        if ch_id == channel_id:
            return letter
    return None


def get_group_role_mention(guild: discord.Guild, group_letter: str) -> str:
    role_name = f"{group_letter}조"
    role = discord.utils.get(guild.roles, name=role_name)
    if role:
        return f"<@&{role.id}>"
    return ""


def effective_scrim_date(current_time: datetime = None) -> date:
    """스크림 기준일. 익일 전환 규칙(NEXT_SCRIM_OPEN_HOUR 이후 = 익일)의 단일 출처."""
    if current_time is None:
        current_time = get_current_kst_time()
    if current_time.hour >= settings.NEXT_SCRIM_OPEN_HOUR:
        return (current_time + timedelta(days=1)).date()
    return current_time.date()


def get_next_scrim_date(current_time: datetime = None) -> dict:
    """다음 스크림 날짜를 계산합니다 (22시 이후는 익일)."""
    if current_time is None:
        current_time = get_current_kst_time()

    next_date = effective_scrim_date(current_time)

    weekday_names = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']

    return {
        "day": next_date.day,
        "weekday_name": weekday_names[next_date.weekday()],
        "month": next_date.month,
        "year": next_date.year,
    }
