"""
공통 유틸리티 함수 모듈
"""
import string
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Union

import discord
import pytz

from config.logging_config import get_logger

if TYPE_CHECKING:
    from models.team_data import TeamData

logger = get_logger('helpers')


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


def format_team_info(team_name: str, team_data: Union[Dict, 'TeamData'], avg_mmr: float) -> List[str]:
    """팀 정보 포맷팅"""
    try:
        if isinstance(team_data, dict):
            players = extract_players_only(team_data)
            staff = team_data.get("staff", [])
            # 스태프도 정규화
            clean_staff = normalize_player_list(staff)
            
            roster_line = f"▫️ 로스터: {', '.join(players)}"
            if clean_staff:
                roster_line += f" // {', '.join(clean_staff)}"
        else:
            players = extract_players_only(team_data)
            roster_line = f"▫️ 로스터: {', '.join(players)}"
        return [roster_line]
        
    except Exception as e:
        logger.error(f"[헬퍼] 팀 정보 포맷팅 실패: {e}", exc_info=True)
        return [f"▫️ 로스터: 오류 발생"]


def get_current_kst_time() -> datetime:
    """현재 KST 시간 반환"""
    kst = pytz.timezone('Asia/Seoul')
    return datetime.now(kst)


def safe_get_channel(client, channel_id: int) -> Optional[discord.TextChannel]:
    """안전하게 채널 가져오기"""
    try:
        return client.get_channel(channel_id)
    except Exception as e:
        logger.error(f"[헬퍼] 채널 가져오기 실패 - 채널 ID: {channel_id}: {e}", exc_info=True)
        return None


def safe_get_guild(client, guild_id: int) -> Optional[discord.Guild]:
    """안전하게 길드 가져오기"""
    try:
        return client.get_guild(guild_id)
    except Exception as e:
        logger.error(f"[헬퍼] 길드 가져오기 실패 - 길드 ID: {guild_id}: {e}", exc_info=True)
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
    
    # 일요일(6)이라면 월요일(0)로 조정
    if next_weekday == 6:  # 일요일
        from datetime import timedelta
        next_date = next_date + timedelta(days=1)
        next_weekday = 0  # 월요일
    
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
