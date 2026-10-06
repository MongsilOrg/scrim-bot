import asyncio
from datetime import date, timedelta
from typing import TYPE_CHECKING

import discord

from bot.manager import BotManager
from commands.ui.roster_views import GroupRosterView
from utils.layout_helpers import custom_view, error_view
from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_current_kst_time, get_next_scrim_date
from utils.scrim_history import record_assignment

if TYPE_CHECKING:
    from bot.client import ScrimBot
    from .team_data_manager import TeamDataManager

logger = get_logger('scrim_orchestrator')


class ScrimOrchestrator:

    def __init__(self, manager: "TeamDataManager"):
        self._manager = manager

    # 호스트 절전 등으로 긴 sleep이 밀릴 때의 최대 지연
    MAX_ASSIGN_WAIT_SECONDS = 3600

    async def check_and_auto_assign(self) -> None:
        while True:
            try:
                team_data_manager = self._manager
                current_time = get_current_kst_time()

                if (team_data_manager._should_check_auto_assign()
                        and current_time.hour >= settings.TEAM_REGISTRATION_DEADLINE_HOUR):
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

    async def start_team_assignment(self) -> None:
        try:
            team_data_manager = self._manager

            current_time = get_current_kst_time()
            if not team_data_manager.is_scrim_date_today():
                logger.warning(
                    f"[조편성] 날짜 불일치로 중단 - scrim_date: {team_data_manager.scrim_month}/{team_data_manager.scrim_day}, "
                    f"현재 날짜: {current_time.month}/{current_time.day}"
                )
                return

            if team_data_manager.is_team_assignment_started:
                return

            last = team_data_manager.last_auto_assignment
            if last and last.date() == current_time.date():
                logger.info("[조편성] 오늘 조편성 처리 완료 상태 - 건너뜀")
                return

            total_teams_current = len(team_data_manager.teams)
            logger.info(f"[조편성] 조편성 시작 - {total_teams_current}팀")

            if total_teams_current < settings.TEAMS_PER_GROUP:
                logger.warning(f"[조편성] 팀 부족으로 중단 - {total_teams_current}팀 < {settings.TEAMS_PER_GROUP}팀")
                # 재시작 때 취소 공지 재전송 방지
                team_data_manager.last_auto_assignment = current_time
                team_data_manager.save_backup()
                if total_teams_current:
                    await self._send_cancellation_notice(team_data_manager, total_teams_current)
                return

            await self._refresh_mmr_before_assignment(team_data_manager)

            team_data_manager.is_team_assignment_started = True

            await self.execute_auto_assignment()
            team_data_manager.last_auto_assignment = current_time
        except Exception as e:
            logger.error(f"[조편성] 자동 조편성 중 오류: {str(e)}", exc_info=True)
            self._rollback_assignment()

    async def _send_cancellation_notice(self, team_data_manager, team_count: int) -> None:
        try:
            client = team_data_manager.client or BotManager.get_instance().get_client()
            channel = client.get_channel(team_data_manager.scrim_channel_id) if client and team_data_manager.scrim_channel_id else None
            if not channel:
                logger.warning("[조편성] 스크림 채널을 찾을 수 없어 취소 공지 생략")
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
            logger.error(f"[조편성] 직전 MMR 갱신 실패 (계속 진행): {e}", exc_info=True)

    async def execute_auto_assignment(self) -> None:
        try:
            team_data_manager = self._manager

            if not team_data_manager.teams:
                raise ValueError("팀 데이터가 없어 조편성을 실행할 수 없습니다.")

            client = team_data_manager.client
            if not client:
                client = BotManager.get_instance().get_client()

            team_processor = BotManager.get_instance().get_team_processor()

            groups, unmatched_teams = await team_processor.build_groups(team_data_manager.teams)

            team_data_manager.groups = groups
            team_data_manager.save_backup()
            # 공지와 역할 처리를 늦추지 않게 따로 돌림
            team_data_manager.spawn_task(record_assignment(
                groups, unmatched_teams, registered=len(team_data_manager.teams),
                guild=client.get_guild(settings.GUILD_ID) if client else None,
                is_test=team_processor.is_test_account,
            ))

            logger.info(f"[조편성] 조편성 실행 완료 - 조 수: {len(groups)}개, 매칭되지 않은 팀: {len(unmatched_teams)}개")

            if not groups:
                logger.warning("[조편성] 편성된 조가 없으므로 Discord 서비스 건너뜀")
                return

            if client:
                await self._execute_discord_services(client, groups, unmatched_teams)
            else:
                logger.warning("[조편성] 클라이언트가 없어 Discord 서비스를 건너뜁니다.")
        except Exception as e:
            logger.error(f"[조편성] 자동 조편성 실행 중 오류 발생: {e}", exc_info=True)
            team_data_manager = self._manager
            self._rollback_assignment()

            try:
                client = team_data_manager.client
                if client and team_data_manager.scrim_channel_id:
                    channel = client.get_channel(team_data_manager.scrim_channel_id)
                    if channel:
                        await channel.send(view=error_view(
                            "자동 조편성 중 오류가 발생했습니다.\n관리자에게 문의해주세요."
                        ))
            except Exception as e2:
                logger.error(f"[Discord] 오류 메시지 전송 실패: {e2}", exc_info=True)

    async def _execute_discord_services(self, client, groups, unmatched_teams):
        try:
            team_processor = BotManager.get_instance().get_team_processor()

            guild = client.get_guild(settings.GUILD_ID)
            if not guild:
                logger.warning(f"[Discord] 서버를 찾을 수 없음 - 서버 ID: {settings.GUILD_ID}")
                return

            await team_processor.discord_service.send_global_announcement(guild, groups, unmatched_teams)

            await team_processor.discord_service.send_notices(guild, groups, unmatched_teams)

        except Exception as e:
            logger.error(f"[Discord] 서비스 실행 실패: {e}", exc_info=True)

    async def restore_group_roster_views(self, client) -> None:
        mgr = self._manager
        if not mgr.groups or not mgr.group_message_ids:
            logger.info("[복구] groups 또는 group_message_ids가 없어 복구 건너뜀")
            return

        guild = client.get_guild(settings.GUILD_ID)
        if not guild:
            logger.warning(f"[복구] 서버를 찾을 수 없음 - 서버 ID: {settings.GUILD_ID}")
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
                    logger.warning(f"[복구] {group_letter}조 메시지를 찾을 수 없음 (id={message_id})")
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
