import asyncio
import re
from typing import TYPE_CHECKING, List, Set, Tuple

if TYPE_CHECKING:
    import discord

from config.logging_config import get_logger
from config.settings import settings
from services.bser_api import BSERAPIClient

logger = get_logger('validators')

# 영문 경계가 없으면 Fastest, Latest, Contest도 걸림
TEST_NICKNAME_PATTERN = re.compile(r'(?:^|[^a-z])test(?:[^a-z]|$)')


def validate_team_name(team_name: str) -> Tuple[bool, str]:
    if not team_name or not team_name.strip():
        return False, "❌ 팀명을 입력해주세요."

    team_name = team_name.strip()

    if len(team_name) < 3:
        return False, "❌ 팀명은 3~12글자여야 합니다.\n\n💡 현재 입력: {0}글자".format(len(team_name))

    if len(team_name) > 12:
        return False, "❌ 팀명은 3~12글자여야 합니다.\n\n💡 현재 입력: {0}글자".format(len(team_name))

    if not re.match(r'^[가-힣ㄱ-ㅎㅏ-ㅣa-zA-Z\s]+$', team_name):
        return False, "❌ 팀명에는 한글과 영어만 사용할 수 있습니다.\n\n💡 숫자, 특수문자는 사용할 수 없습니다."

    return True, ""


def validate_team_data(team_data) -> Tuple[bool, str]:
    try:
        players, staff = team_data.players, team_data.staff

        if len(players) < 3:
            return False, f"❌ 플레이어는 최소 3명이 필요합니다.\n\n💡 현재 입력된 플레이어 수: {len(players)}명"
        
        if len(players) > 4:
            return False, f"❌ 플레이어는 최대 4명까지만 등록할 수 있습니다.\n\n💡 현재 입력된 플레이어 수: {len(players)}명"

        if len(staff) > 3:
            return False, f"❌ 스태프는 최대 3명까지만 등록할 수 있습니다.\n\n💡 현재 입력된 스태프 수: {len(staff)}명"

        for player in players:
            if not player or not player.strip():
                return False, "❌ 플레이어 닉네임을 입력해주세요.\n\n💡 빈 줄이나 공백만 입력할 수 없습니다."

        for staff_member in staff:
            if not staff_member or not staff_member.strip():
                return False, "❌ 스태프 닉네임을 입력해주세요.\n\n💡 빈 줄이나 공백만 입력할 수 없습니다."
        
        return True, ""
        
    except Exception as e:
        logger.error(f"[유효성검사] 팀 데이터 유효성 검사 실패: {e}", exc_info=True)
        return False, "❌ 팀 정보 확인 중 문제가 발생했습니다.\n💡 잠시 후 다시 시도해주세요."


def validate_discord_user_in_team(team_data, member: 'discord.Member') -> bool:
    try:
        team_member_keys = {
            normalize_nickname_for_comparison(name)
            for name in team_data.players + team_data.staff
        }
        return bool(member_name_keys(member) & team_member_keys)

    except Exception as e:
        logger.error(f"[유효성검사] 사용자 팀 포함 검사 실패: {e}", exc_info=True)
        return False


def normalize_nickname_for_comparison(name: str) -> str:
    if not name:
        return ""

    normalized = re.sub(r'\s+', ' ', name.strip()).lower()
    return normalized


def normalize_team_name(name: str) -> str:
    return normalize_nickname_for_comparison(name)


def member_name_keys(member: 'discord.Member') -> Set[str]:
    keys = {normalize_nickname_for_comparison(member.display_name)}
    if member.global_name:
        keys.add(normalize_nickname_for_comparison(member.global_name))
    keys.add(normalize_nickname_for_comparison(member.name))
    return keys


def check_duplicate_members(players: List[str], staff: List[str]) -> Tuple[bool, str]:
    try:
        all_members = players + staff

        all_members = [member.strip() for member in all_members if member.strip()]

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
        return False, "❌ 팀원 중복 확인 중 문제가 발생했습니다.\n💡 잠시 후 다시 시도해주세요."


def validate_members_in_guild(
    guild: 'discord.Guild',
    members: List[str]
) -> Tuple[bool, List[str]]:
    try:
        guild_member_names: set = set()
        for discord_member in guild.members:
            guild_member_names |= member_name_keys(discord_member)

        not_found: List[str] = []
        for member_name in members:
            normalized = normalize_nickname_for_comparison(member_name)
            if normalized not in guild_member_names:
                not_found.append(member_name)

        return (len(not_found) == 0), not_found

    except Exception as e:
        logger.error(f"[유효성검사] 디스코드 서버 멤버 검증 실패: {e}", exc_info=True)
        # 일부러 fail-open
        return True, []


async def validate_members_api(
    members: list[str],
    *,
    maintenance_hint: bool,
) -> tuple[bool, list[str], bool]:
    """반환 (통과 여부, 확인 안 된 닉네임, 점검 여부)."""
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

            # 점검 중 404는 닉네임 캐시 밖 멤버에게만 발생
            if invalid_members and maintenance_hint:
                return True, [], True

            if invalid_members and len(invalid_members) >= len(members) / 2:
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
        logger.error(f"[유효성검사] API 닉네임 검증 실패: {e}", exc_info=True)
        if maintenance_hint:
            return True, [], True
        try:
            async with BSERAPIClient() as check:
                is_maintenance = await check.check_server_maintenance()
        except Exception:
            is_maintenance = True
        if is_maintenance:
            return True, [], True
        # 빈 목록은 API 불통 신호, compose_nickname_error가 fallback으로 처리
        return False, [], False


def split_test_nicknames(nicknames: List[str]) -> Tuple[List[str], List[str]]:
    """반환 (일반, 테스트 계정)."""
    normal: List[str] = []
    test_like: List[str] = []
    for nickname in nicknames:
        if TEST_NICKNAME_PATTERN.search(nickname.lower()):
            test_like.append(nickname)
        else:
            normal.append(nickname)
    return normal, test_like


def build_test_account_notice(nicknames: List[str]) -> str:
    if not nicknames:
        return ""
    return (
        f"❌ 테스트 계정은 확인이 필요합니다: **{', '.join(nicknames)}**\n"
        f"💡 <@{settings.TEST_ACCOUNT_CONTACT_ID}>에게 문의해주세요."
    )


GUILD_NICKNAME_ERROR = "❌ 디스코드 서버에서 확인되지 않는 닉네임: **{names}**\n💡 디스코드 서버 닉네임과 동일하게 입력해주세요."
GAME_NICKNAME_ERROR = "❌ 게임 내에서 확인되지 않는 닉네임: **{names}**\n💡 게임 내 닉네임을 정확히 입력해주세요."
API_UNAVAILABLE_NOTICE = "❌ 게임 서버 응답이 없어 닉네임을 확인할 수 없습니다.\n💡 잠시 후 다시 시도해주세요."


def build_team_mmr_line(team_mmr: float, players: List[str], is_test_account) -> str:
    if team_mmr > 0:
        return f"📊 팀 평균 MMR: **{team_mmr:.2f}**"
    if players and all(is_test_account(player) for player in players):
        return (
            "📊 팀 평균 MMR: 미등록\n"
            f"💡 테스트 계정 MMR이 등록되지 않았습니다. <@{settings.TEST_ACCOUNT_CONTACT_ID}>에게 문의해주세요."
        )
    return "📊 팀 평균 MMR: 확인 중\n💡 잠시 후 자동으로 갱신됩니다."


def compose_nickname_error(nicknames: List[str], template: str, fallback: str = "") -> str:
    normal, test_like = split_test_nicknames(nicknames)
    parts = []
    if normal:
        parts.append(template.format(names=', '.join(normal)))
    if test_like:
        parts.append(build_test_account_notice(test_like))
    return '\n\n'.join(parts) or fallback
