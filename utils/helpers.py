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
    return team_data.players, team_data.staff


def normalize_player_list(players: List[str]) -> List[str]:
    normalized = []
    for player in players or []:
        norm = normalize_nickname_for_comparison(player)
        if norm:
            normalized.append(norm)
    return normalized


def save_json_atomic(path: str, data, indent: Optional[int] = None) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(tmp_path, path)


def build_member_lookup(guild: Optional[discord.Guild]) -> Dict[str, discord.Member]:
    lookup: Dict[str, discord.Member] = {}
    if not guild:
        return lookup
    for member in guild.members:
        for key in member_name_keys(member):
            lookup[key] = member
    return lookup


KST = pytz.timezone('Asia/Seoul')


def get_current_kst_time() -> datetime:
    return datetime.now(KST)


def get_start_of_day_utc(now_kst: datetime = None) -> datetime:
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
    if current_time is None:
        current_time = get_current_kst_time()
    if current_time.hour >= settings.NEXT_SCRIM_OPEN_HOUR:
        return (current_time + timedelta(days=1)).date()
    return current_time.date()


def get_next_scrim_date(current_time: datetime = None) -> dict:
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
