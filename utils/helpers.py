"""
공통 유틸리티 함수 모듈
"""
from datetime import date, datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Union

import discord
import pytz

from config.settings import settings

if TYPE_CHECKING:
    from models.team_data import TeamData


def is_admin(user: discord.Member) -> bool:
    """사용자가 관리자인지 확인합니다."""
    return any(role.id in settings.ADMIN_ROLE_IDS for role in user.roles)


def get_team_members(team_data) -> tuple:
    """팀 데이터에서 players, staff 리스트를 추출합니다."""
    if isinstance(team_data, dict):
        return team_data.get('players', []), team_data.get('staff', [])
    return getattr(team_data, 'players', []), getattr(team_data, 'staff', [])


def get_all_members(team_data) -> List[str]:
    """팀 데이터에서 모든 멤버(players + staff)를 추출합니다."""
    if isinstance(team_data, dict):
        return team_data.get('players', []) + team_data.get('staff', [])
    return getattr(team_data, 'all_members', [])


def extract_players_only(team_data: Union[Dict, 'TeamData', List]) -> List[str]:
    """팀 데이터에서 플레이어만 추출"""
    if isinstance(team_data, dict):
        return team_data.get('players', [])
    elif hasattr(team_data, 'players'):
        return team_data.players
    else:
        # TeamData 객체가 아닌 경우 빈 리스트 반환
        return []


def normalize_player_list(players: List[str]) -> List[str]:
    """플레이어 리스트 정규화 (대소문자, 공백 처리, 빈 값 제거)"""
    if not players:
        return []
    
    normalized = []
    for player in players:
        if player and player.strip():  # 빈 문자열이나 None이 아닌 경우만
            normalized.append(player.strip().lower())
    return normalized


def get_current_kst_time() -> datetime:
    """현재 KST 시간 반환"""
    kst = pytz.timezone('Asia/Seoul')
    return datetime.now(kst)


def get_start_of_day_utc(now_kst: datetime = None) -> datetime:
    """KST 자정(당일 시작) 기준 시각을 UTC로 반환합니다."""
    if now_kst is None:
        now_kst = get_current_kst_time()
    start_of_day_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_of_day_kst.astimezone(pytz.utc)


def get_group_letter(channel_id: int) -> str | None:
    """채널 ID로 조 문자를 반환합니다."""
    for letter, ch_id in settings.GROUP_CHANNEL_IDS.items():
        if ch_id == channel_id:
            return letter
    return None


def get_next_scrim_date(current_time: datetime = None) -> dict:
    """다음 스크림 날짜를 자동으로 계산합니다"""
    if current_time is None:
        current_time = get_current_kst_time()
    
    # 현재 요일 (0=월요일, 6=일요일)
    current_weekday = current_time.weekday()
    
    # 22시 이후라면 익일부터 계산
    if current_time.hour >= settings.NEXT_SCRIM_OPEN_HOUR:
        # 익일 계산
        from datetime import timedelta
        next_date = current_time + timedelta(days=1)
        next_weekday = next_date.weekday()
    else:
        # 당일부터 계산
        next_date = current_time
        next_weekday = current_weekday
    
    # 요일명 매핑
    weekday_names = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']
    
    return {
        "date": next_date,
        "day": next_date.day,
        "weekday": next_weekday,
        "weekday_name": weekday_names[next_weekday],
        "month": next_date.month,
        "year": next_date.year,
        "current_time": current_time
    }


async def get_rest_day_info(target_date: Optional[Union[date, datetime]] = None) -> dict:
    """오늘(또는 지정일)이 휴무일(일요일/공휴일)인지 표시하여 반환합니다."""
    from services.holidays_api import get_holiday_names

    if target_date is None:
        target_date = get_current_kst_time().date()
    elif isinstance(target_date, datetime):
        target_date = target_date.date()

    is_sunday = target_date.weekday() == 6
    holiday_names = await get_holiday_names(target_date)
    is_holiday = bool(holiday_names)

    # [임시] 2026-08-16(포함)까지는 요일/공휴일과 무관하게 모든 스크림을 자율로 상시 안내한다.
    # 기간 종료 후 이 temp_force 관련 3줄을 삭제하면 원래 동작(일요일/공휴일만)으로 원복된다.
    temp_force_rest = target_date <= date(2026, 8, 16)

    labels = (["일요일"] if is_sunday else []) + holiday_names
    if temp_force_rest and not labels:
        labels = ["자율"]

    return {
        "date": target_date,
        "is_rest_day": is_sunday or is_holiday or temp_force_rest,
        "is_sunday": is_sunday,
        "is_holiday": is_holiday,
        "holiday_names": holiday_names,
        "labels": labels,
        "label": " / ".join(labels),
    }
