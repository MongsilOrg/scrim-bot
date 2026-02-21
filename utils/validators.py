"""
유효성 검사 유틸리티 모듈
"""
import re
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    import discord

from config.logging_config import get_logger

logger = get_logger('validators')


def validate_team_name(team_name: str) -> Tuple[bool, str]:
    """팀명 유효성 검사 (글자수 + 허용 문자 체크)

    - 한글, 영어, 공백만 허용 (숫자, 특수문자 불가)
    - 3~8글자
    """
    if not team_name or not team_name.strip():
        return False, "❌ 팀명을 입력해주세요."

    team_name = team_name.strip()

    # 허용 문자 검사 (한글, 영어, 공백만 허용)
    if not re.match(r'^[가-힣a-zA-Z\s]+$', team_name):
        return False, "❌ 팀명에는 한글과 영어만 사용할 수 있습니다.\n\n💡 숫자, 특수문자는 사용할 수 없습니다."

    # 최소 길이 검사 (3글자 이상)
    if len(team_name) < 3:
        return False, "❌ 팀명은 최소 3글자 이상이어야 합니다."

    # 최대 길이 검사 (8글자 이하)
    if len(team_name) > 8:
        return False, "❌ 팀명은 최대 8글자까지만 가능합니다."

    return True, ""


def validate_team_data(team_data) -> Tuple[bool, str]:
    """팀 데이터 유효성 검사"""
    try:
        if isinstance(team_data, dict):
            players = team_data.get('players', [])
            staff = team_data.get('staff', [])
        else:
            players = team_data
            staff = []
        
        # 플레이어 수 검사 (최소 3명, 최대 4명)
        if len(players) < 3:
            return False, f"❌ 플레이어는 최소 3명이 필요합니다.\n\n💡 현재 입력된 플레이어 수: {len(players)}명"
        
        if len(players) > 4:
            return False, f"❌ 플레이어는 최대 4명까지만 등록할 수 있습니다.\n\n💡 현재 입력된 플레이어 수: {len(players)}명"
        
        # 스태프 수 검사 (최대 3명)
        if len(staff) > 3:
            return False, f"❌ 스태프는 최대 3명까지만 등록할 수 있습니다.\n\n💡 현재 입력된 스태프 수: {len(staff)}명"
        
        # 플레이어명 기본 검사 (빈 문자열만 체크)
        for player in players:
            if not player or not player.strip():
                return False, "❌ 플레이어 닉네임을 입력해주세요.\n\n💡 빈 줄이나 공백만 입력할 수 없습니다."
        
        # 스태프명 기본 검사 (빈 문자열만 체크)
        for staff_member in staff:
            if not staff_member or not staff_member.strip():
                return False, "❌ 스태프 닉네임을 입력해주세요.\n\n💡 빈 줄이나 공백만 입력할 수 없습니다."
        
        return True, ""
        
    except Exception as e:
        logger.error(f"[유효성검사] 팀 데이터 유효성 검사 실패: {e}", exc_info=True)
        return False, "⚠️ 팀 정보 확인 중 문제가 발생했습니다.\n\n잠시 후 다시 시도해주세요."


def validate_discord_user_in_team(team_data, user_name: str) -> bool:
    """디스코드 사용자가 팀에 포함되어 있는지 검사 (대소문자 구별 없이)"""
    try:
        if isinstance(team_data, dict):
            players = team_data.get('players', [])
            staff = team_data.get('staff', [])
        else:
            players = team_data
            staff = []
        
        all_members = players + staff
        
        # 대소문자 구별 없이 비교
        normalized_user_name = normalize_nickname_for_comparison(user_name)
        for member in all_members:
            if normalize_nickname_for_comparison(member) == normalized_user_name:
                return True
        
        return False
        
    except Exception as e:
        logger.error(f"[유효성검사] 사용자 팀 포함 검사 실패: {e}", exc_info=True)
        return False


def normalize_nickname_for_comparison(name: str) -> str:
    """닉네임 비교를 위한 정규화 (대소문자 구별 없이)"""
    if not name:
        return ""
    
    # 앞뒤 공백 제거 및 중간 공백 정규화 후 소문자로 변환
    normalized = re.sub(r'\s+', ' ', name.strip()).lower()
    return normalized


def normalize_team_name(name: str) -> str:
    """팀명 정규화 (대소문자 구별 없이)"""
    if not name:
        return ""
    
    # 앞뒤 공백 제거, 중간 공백 정규화, 소문자 변환
    normalized = re.sub(r'\s+', ' ', name.strip()).lower()
    return normalized


def check_duplicate_members(players: List[str], staff: List[str]) -> Tuple[bool, str]:
    """팀원 중복 검사"""
    try:
        all_members = players + staff
        
        # 빈 문자열 제거
        all_members = [member.strip() for member in all_members if member.strip()]
        
        # 중복 검사
        if len(all_members) != len(set(all_members)):
            duplicates = []
            seen = set()
            for member in all_members:
                if member in seen:
                    duplicates.append(member)
                else:
                    seen.add(member)
            duplicate_list = ', '.join(set(duplicates))
            return False, f"❌ 중복된 팀원이 있습니다.\n\n**중복된 닉네임**: {duplicate_list}\n\n💡 같은 닉네임을 여러 번 입력할 수 없습니다."
        
        return True, ""
        
    except Exception as e:
        logger.error(f"[유효성검사] 팀원 중복 검사 실패: {e}", exc_info=True)
        return False, "⚠️ 팀원 중복 확인 중 문제가 발생했습니다.\n\n잠시 후 다시 시도해주세요."


def validate_members_in_guild(
    guild: 'discord.Guild',
    members: List[str]
) -> Tuple[bool, List[str]]:
    """팀원들이 디스코드 서버에 존재하는지 검증합니다.

    guild.members를 한 번 순회하여 이름 set을 구축(O(M))한 뒤,
    팀원 목록을 대조(O(N))하는 O(M+N) 방식으로 동작합니다.

    Args:
        guild: Discord 서버(길드) 객체
        members: 검증할 팀원 닉네임 목록

    Returns:
        (is_valid, not_found_members):
        - is_valid: 모든 팀원이 서버에 존재하면 True
        - not_found_members: 서버에서 찾을 수 없는 팀원 닉네임 목록
    """
    try:
        # 길드 멤버의 모든 닉네임 변형을 set으로 구축 (O(M))
        guild_member_names: set = set()
        for discord_member in guild.members:
            guild_member_names.add(
                normalize_nickname_for_comparison(discord_member.display_name)
            )
            if discord_member.global_name:
                guild_member_names.add(
                    normalize_nickname_for_comparison(discord_member.global_name)
                )
            guild_member_names.add(
                normalize_nickname_for_comparison(discord_member.name)
            )

        # 팀원 검증 (O(N))
        not_found: List[str] = []
        for member_name in members:
            normalized = normalize_nickname_for_comparison(member_name)
            if normalized not in guild_member_names:
                not_found.append(member_name)

        return (len(not_found) == 0), not_found

    except Exception as e:
        logger.error(f"[유효성검사] 디스코드 서버 멤버 검증 실패: {e}", exc_info=True)
        # 검증 실패 시 통과시킴 (서버 장애로 인한 등록 차단 방지)
        return True, []
