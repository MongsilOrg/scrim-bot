"""스크림 조편성 오케스트레이션 모듈

자동 조편성 스케줄링, 실행, Discord 서비스 연동, GroupRosterView 복구와
스크림 생명주기(만료 판정, 22시 다음날 전환, 일일 리셋 루프)를 담당합니다.
"""
import asyncio
from datetime import date, timedelta
from typing import TYPE_CHECKING

import discord

from bot.manager import BotManager
from commands.ui.roster_views import GroupRosterView
from utils.layout_helpers import error_view
from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_current_kst_time, get_next_scrim_date

if TYPE_CHECKING:
    from bot.client import ScrimBot
    from .team_data_manager import TeamDataManager

logger = get_logger('scrim_orchestrator')


class ScrimOrchestrator:
    """스크림 조편성 오케스트레이션을 담당하는 클래스"""

    def __init__(self, manager: "TeamDataManager"):
        self._manager = manager

    async def check_and_auto_assign(self) -> None:
        """자동 조편성 조건을 체크하고 실행합니다."""
        while True:
            try:
                await asyncio.sleep(settings.AUTO_ASSIGNMENT_CHECK_INTERVAL)

                team_data_manager = self._manager

                if not team_data_manager._should_check_auto_assign():
                    continue

                current_time = get_current_kst_time()

                # 17시가 되면 즉시 조편성 시작
                if current_time.hour >= settings.TEAM_REGISTRATION_DEADLINE_HOUR:
                    await self.start_team_assignment()
                    break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[조편성] 자동 조편성 체크 실패: {e}", exc_info=True)
                await asyncio.sleep(settings.AUTO_ASSIGNMENT_CHECK_INTERVAL)

    async def start_team_assignment(self) -> None:
        """조편성을 시작합니다."""
        try:
            team_data_manager = self._manager

            # 날짜 검증: 현재 날짜와 스크림 날짜가 일치하는지 확인
            current_time = get_current_kst_time()
            if not team_data_manager.is_scrim_date_today():
                logger.warning(
                    f"[조편성] 날짜 불일치로 중단 - scrim_date: {team_data_manager.scrim_month}/{team_data_manager.scrim_day}, "
                    f"현재 날짜: {current_time.month}/{current_time.day}"
                )
                return

            # 이미 시작된 경우 중복 실행 방지
            if team_data_manager.is_team_assignment_started:
                return

            total_teams_current = len(team_data_manager.teams)
            logger.info(f"[조편성] 조편성 시작 - {total_teams_current}팀")

            if total_teams_current < settings.TEAMS_PER_GROUP:
                logger.warning(f"[조편성] 팀 부족으로 중단 - {total_teams_current}팀 < {settings.TEAMS_PER_GROUP}팀")
                return

            # 조편성 직전 MMR 마지막 갱신 (시드 마킹도 함께 반영)
            await self._refresh_mmr_before_assignment(team_data_manager)

            team_data_manager.is_team_assignment_started = True

            await self.execute_auto_assignment()
            team_data_manager.last_auto_assignment = current_time
        except Exception as e:
            logger.error(f"[조편성] 자동 조편성 중 오류: {str(e)}", exc_info=True)
            self._rollback_assignment()

    def _rollback_assignment(self) -> None:
        """조편성 실패 롤백. 플래그만 되돌리면 조편성 감지로 종료된 MMR 루프가 죽은 채 남는다."""
        mgr = self._manager
        mgr.is_team_assignment_started = False
        task = mgr.mmr_update_task
        if task is None or task.done():
            mgr.mmr_update_task = asyncio.create_task(mgr.mmr_update_loop())
            logger.info("[조편성] 실패 롤백 - MMR 갱신 루프 재시작")

    async def _refresh_mmr_before_assignment(self, team_data_manager) -> None:
        """조편성 시작 직전 MMR을 새로 fetch하고 이미지를 한 번 갱신합니다."""
        try:
            if not team_data_manager.teams:
                return

            success, fail = await team_data_manager.update_all_team_mmr(force=True)
            logger.info(f"[조편성] 직전 MMR 갱신 - 성공: {success}팀, 실패: {fail}팀")

            # 실제 갱신 성공 시 마지막 갱신 시각 반영 (이미지의 '마지막 갱신' 표시)
            if success > 0:
                team_data_manager.mark_mmr_success()

            channel = team_data_manager.resolve_mmr_channel()
            if channel:
                await team_data_manager.update_mmr_message(channel, mmr_fail_count=fail)
        except Exception as e:
            logger.error(f"[조편성] 직전 MMR 갱신 실패 (계속 진행): {e}", exc_info=True)

    async def execute_auto_assignment(self) -> None:
        """실제 조편성을 실행합니다."""
        try:
            team_data_manager = self._manager

            if not team_data_manager.teams:
                raise ValueError("팀 데이터가 없어 조편성을 실행할 수 없습니다.")

            # 저장된 클라이언트 참조 사용 (초기화 시 설정됨)
            client = team_data_manager.client
            if not client:
                client = BotManager.get_instance().get_client()

            team_processor = BotManager.get_instance().get_team_processor()

            # 조편성 실행 (Discord 작업 제외) - 최신 인스턴스의 팀 데이터 사용
            groups, unmatched_teams = await team_processor.build_groups(team_data_manager.teams)

            # groups 저장 및 백업
            team_data_manager.groups = groups
            team_data_manager.save_backup()

            logger.info(f"[조편성] 조편성 실행 완료 - 조 수: {len(groups)}개, 매칭되지 않은 팀: {len(unmatched_teams)}개")

            if not groups:
                logger.warning("[조편성] 편성된 조가 없으므로 Discord 서비스 건너뜀")
                return

            # Discord 서비스 실행 (클라이언트가 있을 때만)
            if client:
                await self._execute_discord_services(client, groups, unmatched_teams)
            else:
                logger.warning("[조편성] 클라이언트가 없어 Discord 서비스를 건너뜁니다.")
        except Exception as e:
            error_msg = f"[조편성] 자동 조편성 실행 중 오류 발생: {e}"
            logger.error(error_msg)
            team_data_manager = self._manager
            self._rollback_assignment()

            # 오류 메시지 전송 (클라이언트가 있으면)
            try:
                client = team_data_manager.client
                if client and team_data_manager.scrim_channel_id:
                    channel = client.get_channel(team_data_manager.scrim_channel_id)
                    if channel:
                        await channel.send(view=error_view(error_msg))
            except Exception as e2:
                logger.error(f"[Discord] 오류 메시지 전송 실패: {e2}", exc_info=True)

    async def _execute_discord_services(self, client, groups, unmatched_teams):
        """Discord 서비스를 실행합니다 (공지, 역할, 채널 관리 등)."""
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
        """조편성 후 재시작 시 GroupRosterView를 복구합니다."""
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

                # groups에서 해당 조의 데이터 복원
                group_index = ord(group_letter) - ord('A')
                if group_index < 0 or group_index >= len(mgr.groups):
                    continue

                group_teams = mgr.groups[group_index]

                # 새 GroupRosterView 생성 및 재등록
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


# ──────────────────────────────────────────────
# 스크림 생명주기 (만료 판정 / 다음날 전환 / 일일 리셋)
# ──────────────────────────────────────────────

def is_scrim_expired(team_data_manager) -> bool:
    """스크림이 만료되었는지 확인합니다 (스크림 당일 22시 기준)."""
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
    """다음날 스크림으로 전환합니다. refresh_dashboard(channel)로 대시보드를 갱신합니다."""
    bot_manager = BotManager.get_instance()
    old_tdm = bot_manager.get_team_data_manager()
    old_msg_id = old_tdm.dashboard_message_id

    # 기존 MMR 메시지 삭제 (새 날에는 새 MMR 메시지 생성)
    if old_tdm.mmr_message:
        try:
            await old_tdm.mmr_message.delete()
        except Exception:
            pass
    elif old_tdm.mmr_message_id:
        try:
            old_mmr_msg = await channel.fetch_message(old_tdm.mmr_message_id)
            await old_mmr_msg.delete()
        except Exception:
            pass

    # 리셋 + 새 스크림 설정 (dashboard_message_id 보존)
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

    # 대시보드 갱신 (메시지 ID가 확정된 후 백업)
    await refresh_dashboard(channel)
    team_data_manager.save_backup()

    # 태스크 시작
    team_data_manager.auto_assignment_task = asyncio.create_task(
        team_data_manager.check_and_auto_assign()
    )
    team_data_manager.mmr_update_task = asyncio.create_task(
        team_data_manager.mmr_update_loop()
    )

    # MMR 메시지 생성 (팀이 있을 때만)
    if team_data_manager.teams:
        try:
            await team_data_manager.update_mmr_message(channel)
        except Exception as e:
            logger.error(f"[스크림] MMR 메시지 생성 실패: {e}", exc_info=True)

    logger.info(f"[스크림] 다음 스크림 전환 완료 - {date_info['month']}/{date_info['day']} ({date_info['weekday_name']})")


async def daily_reset_loop(client: "ScrimBot", refresh_dashboard) -> None:
    """매일 22시에 다음날 스크림으로 자동 전환하는 백그라운드 태스크."""
    await client.wait_until_ready()
    while not client.is_closed():
        now = get_current_kst_time()

        # 오늘 전환 시각까지 대기 (이미 지났으면 내일)
        target = now.replace(hour=settings.NEXT_SCRIM_OPEN_HOUR, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        try:
            guild = client.guilds[0] if client.guilds else None
            if not guild:
                continue
            channel = guild.get_channel(settings.SCRIM_CHANNEL_ID)
            if not channel:
                continue

            await transition_to_next_scrim(client, channel, refresh_dashboard)
        except Exception as e:
            logger.error(f"[스크림] 자동 전환 실패: {e}", exc_info=True)

        # 중복 실행 방지
        await asyncio.sleep(60)
