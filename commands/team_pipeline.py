"""
팀 등록/수정 파이프라인

TeamModal(등록)과 TeamEditModal(수정)이 공유하는 흐름을 담당합니다:
입력 검증 → 제한 검사 → 닉네임 검증 → MMR 조회 → 저장 → 캐시/백업 → 후속 갱신.
UI 콜백에는 입력 수집과 임시 메시지 생성만 남기고, 결과 표시를 포함한
나머지 단계는 모두 이 모듈이 수행합니다.
"""
from typing import TYPE_CHECKING, List, Optional, Set, Tuple

import discord

from bot.manager import BotManager
from commands.ui.warning_modals import REASON_TYPE, send_sanction_dm
from config.logging_config import get_logger
from config.settings import settings
from models.team_data import TeamData
from models.user_team_cache import UserTeamCache
from utils.helpers import build_member_lookup, get_current_kst_time
from utils.layout_helpers import send_error_message, update_temp_message
from utils.validators import (
    API_UNAVAILABLE_NOTICE,
    GAME_NICKNAME_ERROR,
    GUILD_NICKNAME_ERROR,
    build_team_mmr_line,
    build_test_account_notice,
    check_duplicate_members,
    compose_nickname_error,
    normalize_nickname_for_comparison,
    split_test_nicknames,
    validate_discord_user_in_team,
    validate_members_api,
    validate_members_in_guild,
    validate_team_data,
    validate_team_name,
)

if TYPE_CHECKING:
    from models.team_data_manager import TeamDataManager
    from models.team_processor import TeamProcessor

logger = get_logger('team_pipeline')

MAINTENANCE_SKIP_NOTICE = (
    "🔧 서버 점검으로 닉네임 확인을 건너뛰었습니다.\n"
    "점검 종료 후 자동으로 확인되며, 결과는 DM으로 안내드립니다.\n"
    "💡 닉네임 오타가 없는지 다시 한번 확인해주세요."
)


# ──────────────────────────────────────────────
# 공통 단계
# ──────────────────────────────────────────────

async def _handle_pipeline_exception(
    interaction: discord.Interaction,
    exc: Exception,
    *,
    tag: str,
    action: str,
    team_name: str,
    generic_message: str,
    generic_log: str,
) -> None:
    """등록/수정 파이프라인 공통 예외 처리 (interaction 만료 / Discord 오류 / 그 외)."""
    if isinstance(exc, discord.NotFound):
        logger.warning(f"[{tag}] interaction 만료 - 팀명: {team_name}")
    elif isinstance(exc, discord.HTTPException):
        logger.error(f"[{tag}] Discord API 오류: {exc.status} {exc.text}", exc_info=exc)
        await send_error_message(interaction, f"❌ {action} 중 Discord 오류가 발생했습니다.\n\n💡 잠시 후 다시 시도해주세요.")
    else:
        logger.error(f"[{tag}] {generic_log}: {exc}", exc_info=exc)
        await send_error_message(interaction, generic_message)


async def _validate_inputs(team_data: TeamData, temp_message: discord.Message) -> bool:
    """팀명/멤버 중복/팀 구성 검증. 실패 시 temp_message에 사유를 표시합니다."""
    is_name_valid, name_error = validate_team_name(team_data.name)
    if not is_name_valid:
        await update_temp_message(temp_message, name_error, discord.Color.red())
        return False

    is_duplicate_valid, duplicate_error = check_duplicate_members(team_data.players, team_data.staff)
    if not is_duplicate_valid:
        await update_temp_message(temp_message, duplicate_error, discord.Color.red())
        return False

    is_valid, error_message = validate_team_data(team_data)
    if not is_valid:
        await update_temp_message(temp_message, error_message, discord.Color.red())
        return False

    return True


async def _validate_team_rules(
    team_data_manager: "TeamDataManager",
    team_processor: "TeamProcessor",
    team_data: TeamData,
    temp_message: discord.Message,
    *,
    is_edit: bool,
    original_team_name: Optional[str] = None,
) -> Tuple[bool, bool]:
    """등록/수정 공통 제한 검사와 닉네임 검증.

    순서: 조편성/시간 제한 → 경고 제한(등록만) → 봇 팀 중복 → 길드 존재 → 게임 API.
    수정 경로는 팀명이 바뀐 경우에만 봇 팀 중복을 검사합니다.

    Returns:
        (통과 여부, 서버 점검 여부)
    """
    team_name = team_data.name
    fail_tag = "팀수정실패" if is_edit else "팀신청실패"
    current_time = get_current_kst_time()
    local_error: Optional[str] = None

    is_allowed, err = team_data_manager.check_team_time_rules(current_time, is_edit=is_edit)
    if not is_allowed:
        local_error = err

    if local_error is None and not is_edit:
        is_allowed, err = await team_data_manager.check_member_restrictions(current_time, new_team=team_data)
        if not is_allowed:
            local_error = err

    team_members = team_data.all_members
    real_members = [m for m in team_members if not team_processor.is_test_account(m)]

    if local_error is None and (not is_edit or team_name != original_team_name):
        is_bot_valid, bot_error = team_data_manager.check_duplicate_with_bot_teams(
            team_name, team_members, exclude_team=original_team_name
        )
        if not is_bot_valid:
            local_error = bot_error

    if local_error is None and real_members:
        client = BotManager.get_instance().get_client()
        guild = client.get_guild(settings.GUILD_ID) if client else None
        if guild:
            is_guild_valid, not_found = validate_members_in_guild(guild, real_members)
            if not is_guild_valid:
                if is_edit:
                    logger.info(f"[{fail_tag}] {team_name} | 단계: 디스코드검증 | 대상: [{', '.join(not_found)}]")
                local_error = compose_nickname_error(not_found, GUILD_NICKNAME_ERROR)

    if local_error is not None:
        if not is_edit:
            logger.info(f"[{fail_tag}] {team_name} | 단계: 로컬검증 | 사유: {local_error}")
        await update_temp_message(temp_message, local_error, discord.Color.red())
        return False, False

    is_maintenance = False
    if real_members:
        is_valid, api_invalid, is_maintenance = await validate_members_api(
            real_members, maintenance_hint=team_data_manager.is_maintenance
        )
        if not is_valid and not is_maintenance:
            logger.info(f"[{fail_tag}] {team_name} | 단계: API검증 | 대상: [{', '.join(api_invalid) or '(응답 없음)'}]")
            msg = compose_nickname_error(api_invalid, GAME_NICKNAME_ERROR, API_UNAVAILABLE_NOTICE)
            await update_temp_message(temp_message, msg, discord.Color.red())
            return False, False

    return True, is_maintenance


async def _fetch_team_mmr_or(team_processor: "TeamProcessor", team_data: TeamData, fallback_mmr: float) -> float:
    try:
        _, _, fetched_mmr = await team_processor.fetch_team_mmr(team_data.name, team_data)
        if fetched_mmr > 0:
            return fetched_mmr
    except Exception as e:
        logger.error(f"[팀파이프라인] 팀 MMR 계산 실패 - 팀명: {team_data.name}: {e}", exc_info=True)
    return fallback_mmr


def _save_user_cache(user_id: str, team_data: TeamData) -> None:
    """다음 신청 프리필용 사용자 캐시를 저장합니다."""
    try:
        UserTeamCache().set(user_id, {
            "team_name": team_data.name,
            "players": team_data.players,
            "staff": team_data.staff,
        })
    except Exception as e:
        logger.warning(f"[팀파이프라인] 캐시 저장 실패: {e}")


def schedule_mmr_refresh(team_data_manager: "TeamDataManager", channel) -> None:
    """백그라운드 MMR 갱신 + 대시보드 메시지 업데이트를 예약합니다 (fire-and-forget)."""
    team_data_manager.spawn_task(_update_mmr_background(team_data_manager, channel))


async def _update_mmr_background(team_data_manager: "TeamDataManager", channel) -> None:
    try:
        await team_data_manager.update_all_team_mmr()
    except Exception as e:
        logger.error(f"[팀파이프라인] 팀 MMR 갱신 실패: {e}", exc_info=True)

    # MMR 메시지 업데이트 (실패 시 다음 갱신 루프에서 재시도)
    try:
        await team_data_manager.update_mmr_message(channel)
    except Exception as e:
        logger.error(f"[팀파이프라인] MMR 메시지 업데이트 실패: {e}", exc_info=True)


# ──────────────────────────────────────────────
# 등록 파이프라인
# ──────────────────────────────────────────────

async def process_team_registration(
    interaction: discord.Interaction,
    team_data: TeamData,
    temp_message: discord.Message,
    *,
    submitter: discord.Member,
) -> None:
    """팀 등록 파이프라인: 검증 → MMR 조회 → 저장 → 캐시 → 결과 표시 → 백그라운드 갱신."""
    team_name = team_data.name
    try:
        team_data_manager = BotManager.get_instance().get_team_data_manager()
        team_processor = BotManager.get_instance().get_team_processor()

        # 1. 입력 검증 (팀명/멤버 중복/팀 구성)
        if not await _validate_inputs(team_data, temp_message):
            return

        # 2. 신청자 확인 (테스트 계정이 포함된 경우 디스코드 닉네임 확인 생략)
        await team_processor.ensure_test_accounts_loaded()
        all_members = team_data.all_members
        has_test_account = any(team_processor.is_test_account(member) for member in all_members)
        if not has_test_account:
            submitter_name = submitter.display_name
            if not validate_discord_user_in_team(team_data, submitter):
                error_msg = (f"❌ 본인의 디스코드 닉네임이 팀원 목록에 포함되어 있지 않습니다.\n\n"
                             f"📌 **참가팀의 팀원만 신청할 수 있습니다.**\n\n"
                             f"**현재 디스코드 닉네임**: {submitter_name}\n"
                             f"**입력된 팀원**: {', '.join(all_members) if all_members else '정보 없음'}\n\n"
                             f"💡 플레이어 또는 스태프 목록에 본인의 디스코드 닉네임을 포함해주세요.")
                # 시트에 없는 테스트 계정이 섞였을 수 있으므로 원래 사유에 덧붙인다
                notice = build_test_account_notice(split_test_nicknames(all_members)[1])
                if notice:
                    error_msg = f"{error_msg}\n\n{notice}"
                logger.info(f"[팀신청실패] {team_name} | 단계: 신청자확인 | 신청자: {submitter_name} | 팀원: [{', '.join(all_members)}]")
                await update_temp_message(temp_message, error_msg, discord.Color.red())
                return

        # 3. 제한 검사 + 닉네임 검증
        passed, is_maintenance = await _validate_team_rules(
            team_data_manager, team_processor, team_data, temp_message, is_edit=False
        )
        if not passed:
            return

        # 4. MMR 계산 (실패 시 0 유지, 백그라운드 갱신에서 재시도)
        team_mmr = await _fetch_team_mmr_or(team_processor, team_data, 0.0)
        team_data.mmr = team_mmr

        # 5. 저장 (user_id는 add_team에서 자동 설정)
        success, failure_reason = await team_data_manager.add_team(team_name, team_data, interaction.user)
        if not success:
            # 실패 사유가 있으면 그대로 표시, 없으면 기본 메시지
            error_message = failure_reason if failure_reason else (
                "❌ 팀 등록에 실패했습니다.\n\n"
                "💡 신청 시간 제한을 확인해주세요."
            )
            logger.info(f"[팀신청실패] {team_name} | 단계: 저장 | 사유: {failure_reason or '(사유 없음)'}")
            await update_temp_message(temp_message, error_message, discord.Color.red())
            return

        # 6. 캐시 저장 + 로깅
        _save_user_cache(str(interaction.user.id), team_data)

        players_str = ', '.join(team_data.players) if team_data.players else '(없음)'
        staff_str = ', '.join(team_data.staff) if team_data.staff else '(없음)'
        team_data_manager.log_action(
            "신청", interaction.user, team_name,
            detail=f"선수: {players_str} / 스태프: {staff_str}",
        )
        logger.info(f"[팀신청] {team_name} | MMR: {team_mmr:.2f} | 선수: [{players_str}] | 스태프: [{staff_str}]")

        # 7. unverified 처리 + 결과 메시지
        if is_maintenance:
            team_data_manager.mark_unverified(team_name)
            success_msg = (
                f"**{team_name}** 팀이 등록되었습니다.\n\n"
                f"🎮 선수: {players_str}\n"
                f"🛠️ 스태프: {staff_str}\n\n"
                f"{MAINTENANCE_SKIP_NOTICE}"
            )
        else:
            success_msg = (
                f"**{team_name}** 팀이 성공적으로 등록되었습니다!\n\n"
                f"🎮 선수: {players_str}\n"
                f"🛠️ 스태프: {staff_str}\n"
                f"{build_team_mmr_line(team_mmr, team_data.players, team_processor.is_test_account)}"
            )
        await update_temp_message(temp_message, success_msg, discord.Color.green())

        # 8. 백그라운드에서 MMR 갱신 및 메시지 업데이트
        schedule_mmr_refresh(team_data_manager, interaction.channel)

    except Exception as e:
        await _handle_pipeline_exception(
            interaction, e,
            tag="팀등록", action="팀 등록", team_name=team_name,
            generic_message="❌ 팀 등록 중 오류가 발생했습니다.\n\n💡 다시 시도해도 문제가 지속되면 관리자에게 문의해주세요.",
            generic_log="팀 등록 실패",
        )


# ──────────────────────────────────────────────
# 수정 파이프라인
# ──────────────────────────────────────────────

async def process_team_edit(
    interaction: discord.Interaction,
    *,
    group_letter: Optional[str] = None,
    original_team_name: str,
    original_team_data: TeamData,
    new_team_data: TeamData,
    temp_message: discord.Message,
    is_roster_change: bool,
    apply_warning: bool = False,
    warning_reason: str = "대타",
) -> None:
    """팀 수정 파이프라인: 검증 → MMR 조회 → 교체 저장 → 캐시 → 조별 갱신 → 결과 표시.

    is_roster_change=True(관리자 로스터 변경)면 모든 검증을 건너뛰고,
    group_letter가 가리키는 조의 데이터/역할/음성채널/공지 갱신과 주의 부여를 수행합니다.
    조별 팀 목록은 team_data_manager.groups를 단일 소스로 사용합니다.
    """
    new_team_name = new_team_data.name
    try:
        team_data_manager = BotManager.get_instance().get_team_data_manager()
        team_processor = BotManager.get_instance().get_team_processor()

        # 1. 검증 (관리자 로스터 변경은 모든 검증을 건너뜀)
        if not is_roster_change:
            if not await _validate_inputs(new_team_data, temp_message):
                return

        # 로스터 변경도 뒤이어 MMR 을 재계산하므로 경로와 무관하게 갱신한다
        await team_processor.ensure_test_accounts_loaded()

        is_maintenance = False
        if not is_roster_change:
            passed, is_maintenance = await _validate_team_rules(
                team_data_manager, team_processor, new_team_data, temp_message,
                is_edit=True, original_team_name=original_team_name,
            )
            if not passed:
                return

        # 2. MMR 계산 (실패 시 기존 MMR 유지)
        new_team_mmr = await _fetch_team_mmr_or(team_processor, new_team_data, original_team_data.mmr)

        # 3. 데이터 저장 (관리자 수정 시 신청자가 바뀌지 않도록 user_id 보존)
        new_team_data.user_id = original_team_data.user_id or str(interaction.user.id)
        new_team_data.created_at = interaction.created_at
        replaced, replace_reason = await team_data_manager.replace_team(original_team_name, new_team_data, new_team_mmr)
        if not replaced:
            # 모달이 열린 사이 팀이 취소/개명된 경우. 저장 없이 중단해 유령 팀 부활을 막는다
            await update_temp_message(temp_message, f"❌ {replace_reason}", discord.Color.red())
            return

        _save_user_cache(str(interaction.user.id), new_team_data)

        # 4. unverified 처리
        _apply_unverified_transition(
            team_data_manager,
            is_maintenance=is_maintenance,
            old_players=original_team_data.players,
            new_players=new_team_data.players,
            old_name=original_team_name,
            new_name=new_team_name,
        )

        # 5. diff 계산 + 로깅
        added, removed = _log_edit_diff(
            interaction, team_data_manager, original_team_name, original_team_data, new_team_data, new_team_mmr
        )

        # 6. 조별 데이터 업데이트 (로스터 변경 시)
        if is_roster_change and group_letter:
            await _update_changed_team(group_letter, team_data_manager, original_team_name, new_team_name, new_team_mmr)

        # 7. 결과 메시지
        await _send_edit_result(
            temp_message, team_processor, original_team_name, new_team_data, new_team_mmr,
            added, removed, is_maintenance,
        )

        # 8. 후처리 (주의 부여, 공지/MMR 메시지 갱신)
        if is_roster_change and apply_warning:
            await _apply_roster_warnings(
                interaction, original_team_name, original_team_data.players, warning_reason, temp_message
            )
        if is_roster_change:
            if group_letter:
                await _update_group_announcement(group_letter)
        else:
            await _update_mmr_message_for_individual_team(team_data_manager)

    except Exception as e:
        await _handle_pipeline_exception(
            interaction, e,
            tag="팀수정", action="팀 수정", team_name=new_team_name,
            generic_message="팀 정보 수정 중 오류가 발생했습니다.",
            generic_log="팀 정보 수정 실패",
        )


def _apply_unverified_transition(
    team_data_manager: "TeamDataManager",
    *,
    is_maintenance: bool,
    old_players: List[str],
    new_players: List[str],
    old_name: str,
    new_name: str,
) -> None:
    """점검 중 로스터가 바뀐 팀만 미검증으로 표시하고, 평시와 개명 잔여 마커는 정리합니다."""
    if is_maintenance:
        old_norm = {normalize_nickname_for_comparison(p) for p in old_players}
        new_norm = {normalize_nickname_for_comparison(p) for p in new_players}
        if old_norm != new_norm:
            team_data_manager.mark_unverified(new_name)
    else:
        team_data_manager.clear_unverified(new_name)
    if new_name != old_name:
        team_data_manager.clear_unverified(old_name)


def _log_edit_diff(
    interaction: discord.Interaction,
    team_data_manager: "TeamDataManager",
    original_team_name: str,
    original_team_data: TeamData,
    new_team_data: TeamData,
    new_team_mmr: float,
) -> Tuple[Set[str], Set[str]]:
    """변경사항 diff 계산 + 로그 기록. Returns (added, removed).

    비교는 정규화 키 기준(_apply_unverified_transition 과 동일)이라
    대소문자/공백만 고친 수정은 변경으로 집계되지 않고, 표시는 원문을 유지합니다.
    """
    old_by_key = {
        normalize_nickname_for_comparison(name): name
        for name in original_team_data.players + original_team_data.staff
    }
    new_by_key = {
        normalize_nickname_for_comparison(name): name
        for name in new_team_data.players + new_team_data.staff
    }
    added = {new_by_key[key] for key in new_by_key.keys() - old_by_key.keys()}
    removed = {old_by_key[key] for key in old_by_key.keys() - new_by_key.keys()}

    new_team_name = new_team_data.name
    if (original_team_name != new_team_name) or added or removed:
        players_str = ', '.join(new_team_data.players)
        staff_str = ', '.join(new_team_data.staff) if new_team_data.staff else '(없음)'
        parts = []
        if original_team_name != new_team_name:
            parts.append(f"{original_team_name} → {new_team_name}")
        if removed:
            parts.append(f"{', '.join(sorted(removed))} → {', '.join(sorted(added))}" if added else f"-{', '.join(sorted(removed))}")
        elif added:
            parts.append(f"+{', '.join(sorted(added))}")
        detail = ' / '.join(parts) + f" / 선수: {players_str} / 스태프: {staff_str}"
        team_data_manager.log_action("수정", interaction.user, new_team_name, detail=detail)

    original_players_str = ', '.join(original_team_data.players) if original_team_data.players else '(없음)'
    original_staff_str = ', '.join(original_team_data.staff) if original_team_data.staff else '(없음)'
    new_players_str = ', '.join(new_team_data.players) if new_team_data.players else '(없음)'
    new_staff_str = ', '.join(new_team_data.staff) if new_team_data.staff else '(없음)'
    logger.info(
        f"[팀수정] {original_team_name} → {new_team_name} | MMR: {new_team_mmr:.2f} | "
        f"선수: [{original_players_str}] → [{new_players_str}] | "
        f"스태프: [{original_staff_str}] → [{new_staff_str}]"
    )
    return added, removed


async def _send_edit_result(
    temp_message: discord.Message,
    team_processor: "TeamProcessor",
    original_team_name: str,
    new_team_data: TeamData,
    new_team_mmr: float,
    added: Set[str],
    removed: Set[str],
    is_maintenance: bool,
) -> None:
    """수정 결과 메시지 전송."""
    new_team_name = new_team_data.name
    if is_maintenance:
        await update_temp_message(
            temp_message,
            f"**{original_team_name}** → **{new_team_name}**\n\n{MAINTENANCE_SKIP_NOTICE}",
            discord.Color.green()
        )
        return

    diff_parts = []
    if original_team_name != new_team_name:
        diff_parts.append(f"팀명: {original_team_name} → {new_team_name}")
    if removed:
        diff_parts.append(f"제외: {', '.join(sorted(removed))}")
    if added:
        diff_parts.append(f"추가: {', '.join(sorted(added))}")
    diff_summary = '\n'.join(diff_parts) if diff_parts else "변경 없음"
    mmr_line = build_team_mmr_line(new_team_mmr, new_team_data.players, team_processor.is_test_account)
    await update_temp_message(
        temp_message,
        f"**{new_team_name}** 팀이 수정되었습니다.\n\n{diff_summary}\n{mmr_line}",
        discord.Color.green()
    )


# ──────────────────────────────────────────────
# 로스터 변경 후처리 (GroupRosterView 경로 전용)
# ──────────────────────────────────────────────

def _get_group_teams(team_data_manager: "TeamDataManager", group_letter: str) -> Optional[list]:
    group_index = ord(group_letter) - ord('A')
    if team_data_manager.groups and 0 <= group_index < len(team_data_manager.groups):
        return team_data_manager.groups[group_index]
    return None


async def _update_changed_team(
    group_letter: str,
    team_data_manager: "TeamDataManager",
    original_team_name: str,
    new_team_name: str,
    new_team_mmr: float,
) -> None:
    """변경된 팀의 데이터만 업데이트 (팀 번호/순서 유지)"""
    try:
        current_teams = _get_group_teams(team_data_manager, group_letter)
        if current_teams is None:
            logger.warning(f"[팀수정] 조 데이터를 찾을 수 없음: {group_letter}조")
            return

        # 변경된 팀의 인덱스 찾기
        changed_index = None
        for i, (team_name, team_data, mmr) in enumerate(current_teams):
            if team_name == original_team_name:
                changed_index = i
                break

        if changed_index is None:
            logger.warning(f"[팀수정] 변경된 팀을 찾을 수 없음: {original_team_name}")
            return

        # 해당 팀만 새 데이터로 교체 (순서 유지)
        updated_team_data = team_data_manager.teams[new_team_name]
        group_teams = list(current_teams)
        group_teams[changed_index] = (new_team_name, updated_team_data, new_team_mmr)

        # groups 갱신 (조별 공지가 새 GroupRosterView를 부착하므로 여기가 단일 소스)
        team_data_manager.groups[ord(group_letter) - ord('A')] = group_teams
        team_data_manager.save_backup()

        client = BotManager.get_instance().get_client()
        guild = client.get_guild(settings.GUILD_ID) if client else None
        if not guild:
            logger.warning("[팀수정] 서버 정보를 찾을 수 없음")
            return

        team_processor = BotManager.get_instance().get_team_processor()
        await team_processor.discord_service.update_group_roles(guild, group_letter, group_teams)

        # 변경된 팀의 음성채널 이름만 변경
        await team_processor.discord_service.rename_group_voice_channel(
            guild, group_letter, changed_index, new_team_name
        )

    except Exception as e:
        logger.error(f"[팀수정] 팀 데이터 업데이트 실패: {e}", exc_info=True)


async def _update_group_announcement(group_letter: str) -> None:
    try:
        client = BotManager.get_instance().get_client()
        guild = client.get_guild(settings.GUILD_ID)

        if not guild:
            logger.warning("[팀수정] 서버 정보를 찾을 수 없음")
            return

        channel_id = settings.GROUP_CHANNEL_IDS.get(group_letter)

        if not channel_id:
            logger.warning(f"[팀수정] 조별 채널 ID가 설정되지 않음 - 조: {group_letter}조")
            return

        channel = guild.get_channel(channel_id)
        if not channel:
            logger.warning(f"[팀수정] 조별 채널을 찾을 수 없음 - 조: {group_letter}조")
            return

        team_data_manager = BotManager.get_instance().get_team_data_manager()
        updated_group_teams = _get_group_teams(team_data_manager, group_letter)
        if updated_group_teams is None:
            logger.warning(f"[팀수정] 조 데이터를 찾을 수 없음: {group_letter}조")
            return

        # 기존 조별 공지 메시지 찾기 및 수정
        team_processor = BotManager.get_instance().get_team_processor()
        await team_processor.discord_service.update_single_group_announcement(
            channel, group_letter, list(updated_group_teams)
        )

    except Exception as e:
        logger.error(f"[팀수정] 조별 공지 업데이트 실패: {e}", exc_info=True)


async def _apply_roster_warnings(
    interaction: discord.Interaction,
    original_team_name: str,
    original_players: List[str],
    reason: str,
    temp_message: discord.Message,
) -> None:
    """로스터 변경 시 빠지는 팀 선수에게 주의를 부여합니다."""
    try:
        admin_name = interaction.user.display_name or interaction.user.name
        warning_manager = BotManager.get_instance().get_warning_manager()

        # 길드 멤버 매핑 (닉네임 → Member)
        client = BotManager.get_instance().get_client()
        guild = client.get_guild(settings.GUILD_ID) if client else None
        member_map = build_member_lookup(guild)

        success_count = 0
        fail_names = []

        for player in original_players:
            discord_member = member_map.get(normalize_nickname_for_comparison(player))
            target_id = str(discord_member.id) if discord_member else ""
            target_name = discord_member.display_name if discord_member else player

            success, message, auto_warning, converted_cautions = await warning_manager.add_warning(
                target=target_name,
                target_id=target_id,
                warning_type=REASON_TYPE["대타"],
                reason=reason,
                admin_display_name=admin_name,
            )

            if success:
                success_count += 1
                if discord_member:
                    await send_sanction_dm(
                        discord_member, REASON_TYPE["대타"], reason,
                        auto_warning=auto_warning,
                        converted_cautions=converted_cautions,
                    )
            else:
                fail_names.append(target_name)
                logger.error(f"[로스터주의] 주의 부여 실패 - 대상: {target_name}, 메시지: {message}")

        # 결과 메시지 추가
        result_parts = [f"주의 {success_count}명 부여 완료"]
        if fail_names:
            result_parts.append(f"실패: {', '.join(fail_names)}")
        result_text = " | ".join(result_parts)

        try:
            await update_temp_message(
                temp_message,
                f"**{original_team_name}** → 로스터 변경 완료\n⚡ {result_text}",
                discord.Color.green()
            )
        except Exception:
            pass

        logger.info(f"[로스터주의] {original_team_name} - {result_text} (사유: {reason})")

    except Exception as e:
        logger.error(f"[로스터주의] 주의 부여 처리 실패: {e}", exc_info=True)


async def _update_mmr_message_for_individual_team(team_data_manager: "TeamDataManager") -> None:
    try:
        # MMR 메시지가 있는 경우 해당 채널로 업데이트
        if team_data_manager.mmr_message and team_data_manager.mmr_message.channel:
            await team_data_manager.update_mmr_message(team_data_manager.mmr_message.channel)
        else:
            logger.warning("[팀수정] MMR 메시지 또는 채널 정보가 없어 업데이트 건너뜀")
    except Exception as e:
        logger.error(f"[팀수정] 개별 팀 수정 후 MMR 메시지 업데이트 실패: {e}", exc_info=True)
