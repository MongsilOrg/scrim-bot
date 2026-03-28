"""
공통 유틸리티 함수 모듈
"""
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Union

import discord
import pytz

from config.settings import settings

if TYPE_CHECKING:
    from models.team_data import TeamData


def is_admin(user: discord.Member) -> bool:
    """사용자가 관리자인지 확인합니다."""
    return any(role.id in settings.ADMIN_ROLE_IDS for role in user.roles)


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
    if current_time.hour >= 22:
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
