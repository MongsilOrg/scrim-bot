"""
유효성 검사 유틸리티 모듈
"""
import asyncio
import re
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    import discord

from config.logging_config import get_logger

logger = get_logger('validators')


def validate_team_name(team_name: str) -> Tuple[bool, str]:
    """팀명 유효성 검사 (글자수 + 허용 문자 체크)

    - 한글, 영어, 공백만 허용 (숫자, 특수문자 불가)
    - 3~12글자
    """
    if not team_name or not team_name.strip():
        return False, "❌ 팀명을 입력해주세요."

    team_name = team_name.strip()

    # 길이 검사 (3~12글자)
    if len(team_name) < 3:
        return False, "❌ 팀명은 3~12글자여야 합니다.\n\n💡 현재 입력: {0}글자".format(len(team_name))

    if len(team_name) > 12:
        return False, "❌ 팀명은 3~12글자여야 합니다.\n\n💡 현재 입력: {0}글자".format(len(team_name))

    # 허용 문자 검사 (한글(자음/모음 포함), 영어, 공백만 허용)
    if not re.match(r'^[가-힣ㄱ-ㅎㅏ-ㅣa-zA-Z\s]+$', team_name):
        return False, "❌ 팀명에는 한글과 영어만 사용할 수 있습니다.\n\n💡 숫자, 특수문자는 사용할 수 없습니다."

    return True, ""


def validate_team_data(team_data) -> Tuple[bool, str]:
    """팀 데이터 유효성 검사"""
    try:
        if isinstance(team_data, dict) or hasattr(team_data, 'players'):
            from utils.helpers import get_team_members
            players, staff = get_team_members(team_data)
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
        if isinstance(team_data, dict) or hasattr(team_data, 'players'):
            from utils.helpers import get_team_members
            players, staff = get_team_members(team_data)
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
    """팀원 중복 검사 (대소문자 구별 없이)"""
    try:
        all_members = players + staff

        # 빈 문자열 제거
        all_members = [member.strip() for member in all_members if member.strip()]

        # 대소문자 무시 중복 검사
        seen = set()
        duplicates = []
        for member in all_members:
            normalized = normalize_nickname_for_comparison(member)
            if normalized in seen:
                duplicates.append(member)
            else:
                seen.add(normalized)

        if duplicates:
            duplicate_list = ', '.join(dict.fromkeys(duplicates))
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


async def validate_members_api(
    members: list[str],
    team_data_manager,
) -> tuple[bool, list[str], bool]:
    """게임 API를 통해 팀원 닉네임을 검증합니다.

    Args:
        members: 검증할 닉네임 목록
        team_data_manager: TeamDataManager 인스턴스 (점검 상태 참조용)

    Returns:
        (is_valid, invalid_members, is_maintenance):
        - is_valid: 모든 닉네임이 유효하면 True
        - invalid_members: 유효하지 않은 닉네임 목록
        - is_maintenance: 서버 점검 중이면 True
    """
    from services.bser_api import BSERAPIClient

    try:
        async with BSERAPIClient() as api:
            results = await asyncio.gather(
                *[api.get_user_uid(m) for m in members],
                return_exceptions=True,
            )
            invalid_members = [
                member
                for member, result in zip(members, results)
                if isinstance(result, Exception) or not result
            ]

            if invalid_members and len(invalid_members) >= len(members) / 2:
                if team_data_manager._is_maintenance:
                    return True, [], True
                try:
                    is_maintenance = await api.check_server_maintenance()
                except Exception:
                    is_maintenance = True
                if is_maintenance:
                    return True, [], True

            if invalid_members:
                return False, invalid_members, False
            return True, [], False

    except Exception as e:
        logger.error(f"[validators] API 닉네임 검증 실패: {e}", exc_info=True)
        if team_data_manager._is_maintenance:
            return True, [], True
        try:
            async with BSERAPIClient() as check:
                is_maintenance = await check.check_server_maintenance()
        except Exception:
            is_maintenance = True
        if is_maintenance:
            return True, [], True
        # API 연결 자체가 실패했고 점검도 아닌 경우: 호출자가 api_error로 처리하도록
        # 빈 invalid list + maintenance=False 로 반환하여 호출자가 구분할 수 있게 함
        # 하지만 스펙상 (False, [], False) 케이스는 없으므로 빈 list로 반환
        return False, [], False
