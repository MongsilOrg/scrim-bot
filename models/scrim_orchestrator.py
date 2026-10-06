import asyncio
from datetime import date, timedelta
from typing import TYPE_CHECKING, List, Optional, Tuple

import discord

from bot.manager import BotManager
from commands.ui.roster_views import GroupRosterView
from utils.layout_helpers import custom_view, warning_view
from config.logging_config import get_logger
from config.settings import settings
from services.holidays_api import get_rest_day_info
from services.notion_api import get_server_info
from utils.helpers import get_current_kst_time, get_next_scrim_date, is_assignment_window
from utils.scrim_history import record_assignment

if TYPE_CHECKING:
    from bot.client import ScrimBot
    from .team_data_manager import TeamDataManager

logger = get_logger('scrim_orchestrator')

STAGE_GLOBAL_NOTICE = 'global_notice'
STAGE_ROLES = 'roles'
STAGE_GROUP_NOTICES = 'group_notices'
STAGE_VOICE = 'voice'

DELAY_NOTICE = "조편성이 늦어지고 있습니다. 운영진이 확인하고 있습니다."


class ScrimOrchestrator:

    def __init__(self, manager: "TeamDataManager"):
        self._manager = manager
        self._assign_lock = asyncio.Lock()

    # 호스트 절전 등으로 긴 sleep이 밀릴 때의 최대 지연
    MAX_ASSIGN_WAIT_SECONDS = 3600

    async def check_and_auto_assign(self) -> None:
        while True:
            try:
                team_data_manager = self._manager
                current_time = get_current_kst_time()

                if (team_data_manager._should_check_auto_assign()
                        and is_assignment_window(current_time)):
                    await self.start_team_assignment()
                    break

                target = current_time.replace(
                    hour=settings.TEAM_REGISTRATION_DEADLINE_HOUR, minute=0, second=0, microsecond=0
                )
                if current_time >= target:
                    target += timedelta(days=1)
                wait_seconds = (target - current_time).total_seconds()
                await asyncio.sleep(min(wait_seconds, self.MAX_ASSIGN_WAIT_SECONDS) + 1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[조편성] 자동 조편성 체크 실패: {e}", exc_info=True)
                await asyncio.sleep(settings.AUTO_ASSIGNMENT_CHECK_INTERVAL)

    def is_assignment_done_today(self, current_time=None) -> bool:
        last = self._manager.last_auto_assignment
        if current_time is None:
            current_time = get_current_kst_time()
        return bool(last and last.date() == current_time.date())

    def _today_progress(self) -> Optional[dict]:
        progress = self._manager.assignment_progress
        today = get_current_kst_time().date().isoformat()
        if isinstance(progress, dict) and progress.get('date') == today:
            return progress
        return None

    def _mark_stage(self, stage: str) -> None:
        progress = self._today_progress()
        if progress is None:
            return
        done = progress.setdefault('done', [])
        if stage not in done:
            done.append(stage)
        self._manager.save_backup()

    def rerun_team_assignment(self) -> Tuple[bool, str]:
        mgr = self._manager
        now = get_current_kst_time()
        if self._assign_lock.locked():
            return False, "조편성이 진행 중입니다. 끝난 뒤 다시 확인해주세요."
        if not (mgr.is_scrim_date_today(now) and is_assignment_window(now)):
            return False, (
                f"조편성은 스크림 당일 {settings.TEAM_REGISTRATION_DEADLINE_HOUR}시부터 "
                f"{settings.NEXT_SCRIM_OPEN_HOUR}시 전까지만 다시 실행할 수 있습니다."
            )
        if self.is_assignment_done_today(now):
            return False, "오늘 조편성은 이미 끝났습니다."
        mgr.spawn_task(self.start_team_assignment())
        logger.info("[조편성] 운영진 요청으로 조편성 다시 실행")
        return True, "조편성을 다시 실행합니다."

    async def start_team_assignment(self) -> None:
        if self._assign_lock.locked():
            logger.info("[조편성] 이미 진행 중이라 건너뜀")
            return
        async with self._assign_lock:
            try:
                await self._run_assignment()
            except Exception as e:
                logger.error(f"[조편성] 자동 조편성 중 오류: {e}", exc_info=True)
                # 조가 저장된 뒤에는 공지가 나갔을 수 있어 되돌리지 않고 이어하기에 맡김
                if self._manager.groups is None:
                    self._rollback_assignment()
                await self._report_assignment_failure("조편성 처리 중 오류가 발생했습니다.")

    async def _run_assignment(self) -> None:
        team_data_manager = self._manager

        current_time = get_current_kst_time()
        if not team_data_manager.is_scrim_date_today(current_time):
            logger.error(
                f"[조편성] 날짜 불일치로 중단 - scrim_date: {team_data_manager.scrim_month}/{team_data_manager.scrim_day}, "
                f"현재 날짜: {current_time.month}/{current_time.day}"
            )
            return

        if not is_assignment_window(current_time):
            logger.error(f"[조편성] 조편성 시간이 아니라 중단 - 현재 {current_time:%H:%M}")
            return

        if self.is_assignment_done_today(current_time):
            logger.info("[조편성] 오늘 조편성 처리 완료 상태 - 건너뜀")
            return

        if team_data_manager.is_team_assignment_started and team_data_manager.groups is not None:
            if self._today_progress() is None:
                # 단계 기록 도입 전 백업은 조 저장 뒤 Discord 처리까지 끝난 상태
                logger.warning("[조편성] 단계 기록이 없는 조편성 상태 - 완료로 처리")
                team_data_manager.last_auto_assignment = current_time
                team_data_manager.save_backup()
                return
            logger.info("[조편성] 중단된 조편성 이어서 진행")
            await self._run_discord_stages()
            return

        total_teams_current = len(team_data_manager.teams)
        logger.info(f"[조편성] 조편성 시작 - {total_teams_current}팀")

        if total_teams_current < settings.TEAMS_PER_GROUP:
            await self._cancel_for_lack_of_teams(current_time, total_teams_current)
            return

        await self._refresh_mmr_before_assignment(team_data_manager)

        team_data_manager.is_team_assignment_started = True

        # MMR 갱신 중에는 취소가 열려 있어 팀 수가 줄 수 있음
        total_teams_current = len(team_data_manager.teams)
        if total_teams_current < settings.TEAMS_PER_GROUP:
            team_data_manager.is_team_assignment_started = False
            await self._cancel_for_lack_of_teams(current_time, total_teams_current)
            return

        await self.execute_auto_assignment()

    async def _cancel_for_lack_of_teams(self, current_time, team_count: int) -> None:
        team_data_manager = self._manager
        logger.warning(f"[조편성] 팀 부족으로 중단 - {team_count}팀 < {settings.TEAMS_PER_GROUP}팀")
        # 재시작 때 취소 공지 재전송 방지
        team_data_manager.last_auto_assignment = current_time
        team_data_manager.save_backup()
        if team_count:
            await self._send_cancellation_notice(team_data_manager, team_count)

    async def _send_cancellation_notice(self, team_data_manager, team_count: int) -> None:
        try:
            client = team_data_manager.client or BotManager.get_instance().get_client()
            channel = client.get_channel(team_data_manager.scrim_channel_id) if client and team_data_manager.scrim_channel_id else None
            if not channel:
                logger.error("[조편성] 스크림 채널을 찾을 수 없어 취소 공지 생략")
                return

            date_str = f"{team_data_manager.scrim_month}/{team_data_manager.scrim_day}"
            mentions = " ".join(
                f"<@{team.user_id}>" for team in team_data_manager.teams.values() if team.user_id
            )
            description = (
                f"등록된 팀이 {team_count}팀으로 최소 {settings.TEAMS_PER_GROUP}팀에 미달하여 "
                f"오늘 스크림은 진행하지 않습니다.\n"
                f"다음 스크림 신청은 {settings.NEXT_SCRIM_OPEN_HOUR}시에 열립니다."
            )
            if mentions:
                description = f"{mentions}\n{description}"
            view = custom_view(f"📢 {date_str} 스크림 취소 안내", description, discord.Color.orange())
            await channel.send(view=view, allowed_mentions=discord.AllowedMentions(users=True))
            logger.info(f"[조편성] 팀 부족 취소 공지 전송 - {team_count}팀")
        except Exception as e:
            logger.error(f"[조편성] 취소 공지 전송 실패: {e}", exc_info=True)

    def _rollback_assignment(self) -> None:
        """조편성 감지로 이미 종료된 MMR 루프도 재시작 대상."""
        mgr = self._manager
        mgr.is_team_assignment_started = False
        task = mgr.mmr_update_task
        if task is None or task.done():
            mgr.mmr_update_task = asyncio.create_task(mgr.mmr_update_loop())
            logger.info("[조편성] 실패 롤백 - MMR 갱신 루프 재시작")

    async def _report_assignment_failure(self, reason: str) -> None:
        mgr = self._manager
        client = mgr.client or BotManager.get_instance().get_client()
        if not client:
            return

        try:
            log_channel = client.get_channel(settings.LOG_CHANNEL_ID)
            if log_channel:
                mentions = " ".join(f"<@&{role_id}>" for role_id in sorted(settings.ADMIN_ROLE_IDS))
                text = (
                    f"⚠️ **조편성 확인 필요**\n{reason}\n"
                    "원인을 확인해주세요. 봇을 재시작하면 남은 단계부터 이어서 진행합니다."
                )
                if mentions:
                    text = f"{mentions}\n{text}"
                await log_channel.send(text, allowed_mentions=discord.AllowedMentions(roles=True))
            else:
                logger.error(f"[조편성] 로그 채널을 찾을 수 없어 운영진 알림 생략 - 채널 ID: {settings.LOG_CHANNEL_ID}")
        except Exception as e:
            logger.error(f"[조편성] 운영진 알림 전송 실패: {e}", exc_info=True)

        progress = self._today_progress()
        if progress and STAGE_GLOBAL_NOTICE in progress.get('done', []):
            return
        try:
            channel = client.get_channel(mgr.scrim_channel_id) if mgr.scrim_channel_id else None
            if channel:
                await channel.send(view=warning_view(DELAY_NOTICE, title="⏳ 조편성 지연"))
        except Exception as e:
            logger.error(f"[조편성] 지연 안내 전송 실패: {e}", exc_info=True)

    async def _refresh_mmr_before_assignment(self, team_data_manager) -> None:
        try:
            if not team_data_manager.teams:
                return

            success, fail = await team_data_manager.update_all_team_mmr(force=True)
            logger.info(f"[조편성] 직전 MMR 갱신 - 성공: {success}팀, 실패: {fail}팀")

            if success > 0:
                team_data_manager.mark_mmr_success()

            channel = team_data_manager.resolve_mmr_channel()
            if channel:
                await team_data_manager.update_mmr_message(channel, mmr_fail_count=fail)
        except Exception as e:
            logger.error(f"[조편성] 직전 MMR 갱신 실패, 계속 진행: {e}", exc_info=True)

    async def execute_auto_assignment(self) -> None:
        team_data_manager = self._manager

        if not team_data_manager.teams:
            raise ValueError("팀 데이터가 없어 조편성을 실행할 수 없습니다.")

        client = team_data_manager.client or BotManager.get_instance().get_client()
        team_processor = BotManager.get_instance().get_team_processor()

        try:
            groups, unmatched_teams = await team_processor.build_groups(team_data_manager.teams)
        except Exception as e:
            logger.error(f"[조편성] 조편성 계산 실패: {e}", exc_info=True)
            self._rollback_assignment()
            await self._report_assignment_failure("조편성 계산에 실패했습니다.")
            return

        logger.info(f"[조편성] 조편성 실행 완료 - 조 수: {len(groups)}개, 매칭되지 않은 팀: {len(unmatched_teams)}개")

        if not groups:
            team_data_manager.is_team_assignment_started = False
            await self._cancel_for_lack_of_teams(get_current_kst_time(), len(team_data_manager.teams))
            return

        team_data_manager.groups = groups
        team_data_manager.assignment_progress = {
            'date': get_current_kst_time().date().isoformat(),
            'done': [],
            'unmatched': [team_name for team_name, _, _ in unmatched_teams],
        }
        team_data_manager.save_backup()
        # 공지와 역할 처리를 늦추지 않게 따로 돌림
        team_data_manager.spawn_task(record_assignment(
            groups, unmatched_teams, registered=len(team_data_manager.teams),
            guild=client.get_guild(settings.GUILD_ID) if client else None,
            is_test=team_processor.is_test_account,
        ))

        await self._run_discord_stages()

    async def _run_discord_stages(self) -> None:
        """전체 공지는 다시 보내면 중복이라 단계마다 끝난 것을 기록하고 남은 단계만 실행."""
        mgr = self._manager
        progress = self._today_progress() or {}
        done = progress.get('done', [])
        groups = mgr.groups or []
        unmatched_teams = [
            (name, mgr.teams[name], mgr.teams[name].mmr)
            for name in progress.get('unmatched', []) if name in mgr.teams
        ]

        client = mgr.client or BotManager.get_instance().get_client()
        guild = client.get_guild(settings.GUILD_ID) if client else None
        if not guild:
            logger.error(f"[Discord] 서버를 찾을 수 없음 - 서버 ID: {settings.GUILD_ID}")
            await self._report_assignment_failure("서버를 찾을 수 없어 공지와 역할 처리를 하지 못했습니다.")
            return

        service = BotManager.get_instance().get_team_processor().discord_service

        if STAGE_GLOBAL_NOTICE not in done:
            await service.send_global_announcement(guild, groups, unmatched_teams)
            self._mark_stage(STAGE_GLOBAL_NOTICE)

        # 역할 재배정이 공지보다 늦으면 멘션과 채널 권한이 이전 조 멤버에게 감
        if STAGE_ROLES not in done:
            await service.handle_discord_roles(guild, groups)
            self._mark_stage(STAGE_ROLES)

        missing: List[str] = []
        if STAGE_GROUP_NOTICES not in done:
            missing = await self._send_group_notices(guild, service, groups)
            if not missing:
                self._mark_stage(STAGE_GROUP_NOTICES)

        if STAGE_VOICE not in done:
            await service.rename_voice_channels(guild, groups)
            self._mark_stage(STAGE_VOICE)

        if missing:
            letters = ", ".join(f"{letter}조" for letter in missing)
            logger.error(f"[조편성] 조별 공지 미전송 - {letters}")
            await self._report_assignment_failure(f"{letters} 공지를 보내지 못했습니다.")
            return

        mgr.last_auto_assignment = get_current_kst_time()
        mgr.save_backup()
        logger.info("[조편성] 공지와 역할 처리 완료")

    async def _send_group_notices(self, guild, service, groups) -> List[str]:
        """조별 공지는 채널을 비운 뒤 보내 다시 보내도 중복이 남지 않음. 반환은 못 보낸 조."""
        mgr = self._manager
        targets = []
        for group_letter, channel_id in settings.GROUP_CHANNEL_IDS.items():
            group_index = ord(group_letter) - ord('A')
            group = groups[group_index] if 0 <= group_index < len(groups) else None
            if group and group_letter in mgr.group_message_ids:
                continue
            targets.append((group_letter, channel_id, group))

        pending = [letter for letter, _, group in targets if group]

        try:
            is_rest_day = (await get_rest_day_info())["is_rest_day"]
        except Exception as e:
            logger.error(f"[Discord] 휴무일 정보 조회 실패, 자율 진행 안내 생략: {e}", exc_info=True)
            is_rest_day = False

        try:
            info = await asyncio.to_thread(get_server_info)
        except Exception as e:
            logger.error(f"[Discord] 서버 정보 조회 실패, 조별 공지 보류: {e}", exc_info=True)
            return pending

        for group_letter, channel_id, group in targets:
            try:
                channel = guild.get_channel(channel_id)
                if not channel:
                    logger.error(f"[Discord] 조별 채널을 찾을 수 없음 - 조: {group_letter}조, 채널 ID: {channel_id}")
                    continue

                await service.clear_channel_messages(channel)
                if group:
                    message = service.create_group_announcement_message(group_letter, group, info)
                    await service.send_group_announcement_with_image(channel, message, group, is_rest_day=is_rest_day)
            except Exception as e:
                logger.error(f"[Discord] 조별 공지 전송 실패 - 조: {group_letter}조: {e}", exc_info=True)

        return [letter for letter in pending if letter not in mgr.group_message_ids]

    async def restore_group_roster_views(self, client) -> None:
        mgr = self._manager
        if not mgr.groups or not mgr.group_message_ids:
            logger.info("[복구] groups 또는 group_message_ids가 없어 복구 건너뜀")
            return

        guild = client.get_guild(settings.GUILD_ID)
        if not guild:
            logger.error(f"[복구] 서버를 찾을 수 없음 - 서버 ID: {settings.GUILD_ID}")
            return

        restored = 0
        for group_letter, message_id in mgr.group_message_ids.items():
            try:
                channel_id = settings.GROUP_CHANNEL_IDS.get(group_letter)
                if not channel_id:
                    continue

                channel = guild.get_channel(channel_id)
                if not channel:
                    logger.warning(f"[복구] {group_letter}조 채널을 찾을 수 없음")
                    continue

                try:
                    message = await channel.fetch_message(message_id)
                except discord.NotFound:
                    logger.warning(f"[복구] {group_letter}조 메시지를 찾을 수 없음 - 메시지 ID: {message_id}")
                    continue

                group_index = ord(group_letter) - ord('A')
                if group_index < 0 or group_index >= len(mgr.groups):
                    continue

                group_teams = mgr.groups[group_index]

                saved_text = mgr.group_message_texts.get(group_letter, "")
                roster_view = GroupRosterView(
                    group_letter, group_teams,
                    message_text=saved_text, has_image=True,
                )
                await message.edit(view=roster_view)
                restored += 1
                logger.info(f"[복구] {group_letter}조 GroupRosterView 재등록 완료")

            except Exception as e:
                logger.error(f"[복구] {group_letter}조 복구 실패: {e}", exc_info=True)

        logger.info(f"[복구] GroupRosterView 복구 완료 - {restored}개 조")


def is_scrim_expired(team_data_manager) -> bool:
    if not team_data_manager.scrim_day or not team_data_manager.scrim_month:
        return True

    try:
        now = get_current_kst_time()
        today = now.date()
        scrim_date = date(now.year, team_data_manager.scrim_month, team_data_manager.scrim_day)

        if (scrim_date - today).days > 180:
            scrim_date = date(now.year - 1, team_data_manager.scrim_month, team_data_manager.scrim_day)

        if scrim_date < today:
            return True
        if scrim_date == today and now.hour >= settings.NEXT_SCRIM_OPEN_HOUR:
            return True
        return False
    except ValueError:
        return True


async def transition_to_next_scrim(client: "ScrimBot", channel: discord.TextChannel, refresh_dashboard) -> None:
    bot_manager = BotManager.get_instance()
    old_tdm = bot_manager.get_team_data_manager()
    old_msg_id = old_tdm.dashboard_message_id

    if old_tdm.mmr_message:
        try:
            await old_tdm.mmr_message.delete()
        except discord.NotFound:
            pass
        except Exception as e:
            logger.warning(f"[스크림] 이전 MMR 메시지 삭제 실패: {e}")
    elif old_tdm.mmr_message_id:
        try:
            old_mmr_msg = await channel.fetch_message(old_tdm.mmr_message_id)
            await old_mmr_msg.delete()
        except discord.NotFound:
            pass
        except Exception as e:
            logger.warning(f"[스크림] 이전 MMR 메시지 삭제 실패: {e}")

    team_data_manager = await bot_manager.reset_team_data_manager(client)
    team_data_manager.assignment_progress = {}
    if old_msg_id:
        team_data_manager.dashboard_message_id = old_msg_id
    team_data_manager.scrim_channel_id = settings.SCRIM_CHANNEL_ID

    date_info = get_next_scrim_date()
    await team_data_manager.initialize_new_scrim(
        scrim_day=date_info['day'],
        scrim_month=date_info['month'],
        scrim_channel_id=settings.SCRIM_CHANNEL_ID,
    )

    team_data_manager.start_background_tasks()

    try:
        await refresh_dashboard(channel)
    except Exception as e:
        logger.error(f"[스크림] 전환 중 대시보드 갱신 실패: {e}", exc_info=True)
    # 대시보드 메시지 ID 확정 후 백업
    team_data_manager.save_backup()

    if team_data_manager.teams:
        try:
            await team_data_manager.update_mmr_message(channel)
        except Exception as e:
            logger.error(f"[스크림] MMR 메시지 생성 실패: {e}", exc_info=True)

    logger.info(f"[스크림] 다음 스크림 전환 완료 - {date_info['month']}/{date_info['day']} {date_info['weekday_name']}")


# 전환 실패 때 재시도 간격
RESET_RETRY_SECONDS = 300


async def _run_scheduled_transition(client: "ScrimBot", refresh_dashboard, log) -> bool:
    guild = client.get_guild(settings.GUILD_ID)
    if not guild:
        log(f"[스크림] 자동 전환 건너뜀 - 서버를 찾을 수 없음, 서버 ID: {settings.GUILD_ID}")
        return False
    channel = guild.get_channel(settings.SCRIM_CHANNEL_ID)
    if not channel:
        log(f"[스크림] 자동 전환 건너뜀 - 채널 없음: {settings.SCRIM_CHANNEL_ID}")
        return False
    try:
        await transition_to_next_scrim(client, channel, refresh_dashboard)
    except Exception as e:
        log(f"[스크림] 자동 전환 실패: {e}", exc_info=True)
        return False
    return True


# 시각 대신 만료 여부로 판단, 22시 직전 재시작이나 절전으로 지난 전환도 처리
async def daily_reset_loop(client: "ScrimBot", refresh_dashboard) -> None:
    await client.wait_until_ready()
    failing = False
    while not client.is_closed():
        team_data_manager = BotManager.get_instance().get_team_data_manager()
        if is_scrim_expired(team_data_manager):
            # Sentry 중복 방지로 연속 실패는 첫 번째만 ERROR
            log = logger.warning if failing else logger.error
            failing = not await _run_scheduled_transition(client, refresh_dashboard, log)
        else:
            failing = False

        if failing:
            wait_seconds = RESET_RETRY_SECONDS
        else:
            now = get_current_kst_time()
            target = now.replace(hour=settings.NEXT_SCRIM_OPEN_HOUR, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = min((target - now).total_seconds(), ScrimOrchestrator.MAX_ASSIGN_WAIT_SECONDS) + 1
        await asyncio.sleep(wait_seconds)
