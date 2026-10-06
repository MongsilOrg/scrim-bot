import asyncio
import time
from typing import Optional, Tuple, TYPE_CHECKING

import discord
from discord.ui import Container, LayoutView, MediaGallery, Separator, TextDisplay

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_current_kst_time
from utils.layout_helpers import FOOTER_TEXT

from services.bser_api import UID_ERROR, UID_NOT_FOUND, BSERAPIClient
from services.image_generator import TOURNAMENT_COLOR, ImageGenerator
from services.notion_api import get_server_info

if TYPE_CHECKING:
    from .team_data_manager import TeamDataManager

logger = get_logger('mmr_updater')


class MmrUpdater:

    def __init__(self, manager: "TeamDataManager"):
        self._manager = manager
        self._render_lock = asyncio.Lock()
        self._render_waiters = 0

    async def update_mmr_message(self, channel: discord.TextChannel, mmr_fail_count: int = 0) -> None:
        self._render_waiters += 1
        try:
            await self._render_lock.acquire()
        finally:
            self._render_waiters -= 1
        try:
            if self._render_waiters > 0:
                # 대기 중인 뒤 요청이 더 새 상태로 그림
                logger.debug("[MMR메시지] 뒤 요청에 합쳐 갱신 생략")
                return
            await self._render_mmr_message(channel, mmr_fail_count)
        finally:
            self._render_lock.release()

    async def _render_mmr_message(self, channel: discord.TextChannel, mmr_fail_count: int) -> None:
        mgr = self._manager
        # get_server_info는 TTL 캐시
        info = await asyncio.to_thread(get_server_info)
        operate = info['operate']
        try:
            if mgr.is_team_assignment_started:
                logger.warning("[MMR메시지] 조편성 시작 이후이므로 갱신 불가")
                return

            try:
                team_processor = BotManager.get_instance().get_team_processor()
                await team_processor.ensure_seeds_marked(mgr.teams)
            except Exception as e:
                logger.warning(f"[MMR메시지] 시드 마킹 실패, 갱신은 계속함: {e}")

            # 렌더 스레드 도중 팀 변경에 대비한 스냅샷
            img_io = await ImageGenerator.generate_mmr_image_async(
                dict(mgr.teams), unverified_teams=set(mgr.unverified_teams), server_info=info
            )

            if not img_io:
                logger.warning("[MMR메시지] 이미지 생성 실패")
                return

            update_time = mgr._last_success_time or get_current_kst_time().strftime('%H:%M')
            if mgr.is_maintenance:
                desc = f"서버 점검 중 / 마지막 갱신: `{update_time}`"
            else:
                desc = f"총 **{len(mgr.teams)}**팀 / 마지막 갱신: `{update_time}`"
                if mmr_fail_count > 0:
                    desc += f"\n{mmr_fail_count}개 팀 MMR 갱신 실패"

            children = [
                TextDisplay(content=f"## 📊 팀 MMR 정보\n{desc}"),
                MediaGallery(discord.MediaGalleryItem(media="attachment://mmr_table.png")),
                TextDisplay(content=f"**운영 정보**\n{operate}"),
                Separator(),
                TextDisplay(content=FOOTER_TEXT),
            ]

            mmr_view = LayoutView()
            accent = discord.Color.from_str(TOURNAMENT_COLOR) if info['is_tournament'] else discord.Color.blue()
            mmr_view.add_item(Container(*children, accent_colour=accent))

            if not mgr.mmr_message and mgr.mmr_message_id:
                try:
                    mgr.mmr_message = await channel.fetch_message(mgr.mmr_message_id)
                except (discord.NotFound, discord.HTTPException):
                    mgr.mmr_message = None
                    mgr.mmr_message_id = None

            if mgr.mmr_message:
                try:
                    await mgr.mmr_message.edit(
                        view=mmr_view,
                        embed=None,
                        content=None,
                        attachments=[discord.File(img_io, filename='mmr_table.png')]
                    )
                    mgr._mmr_dirty = False
                    return
                except discord.NotFound:
                    mgr.mmr_message = None
                    mgr.mmr_message_id = None
                except discord.HTTPException as e:
                    logger.warning(f"[MMR메시지] 편집 실패 - 재시도: {e}")
                    try:
                        await mgr.mmr_message.delete()
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        logger.warning(f"[MMR메시지] 기존 메시지 삭제 실패: {e}")
                    mgr.mmr_message = None
                    mgr.mmr_message_id = None

            # 편집 시도가 버퍼를 끝까지 읽었으면 새 메시지 첨부가 빈 파일이 됨
            img_io.seek(0)
            new_message = await channel.send(
                view=mmr_view,
                file=discord.File(img_io, filename='mmr_table.png')
            )
            mgr.mmr_message = new_message
            mgr.mmr_message_id = new_message.id
            mgr._mmr_dirty = False
            mgr.save_backup()

        except Exception as e:
            logger.error(f"[MMR메시지] 업데이트 실패: {e}", exc_info=True)
            raise

    # 스킵 조건이 놓친 표시 변화의 최대 반영 지연
    RENDER_BACKSTOP_SECONDS = 1800
    # 패치 날 정기 점검이 약 4시간이라 그보다 길게 실패할 때만 경보. 점검 주기 10분 기준 5시간
    OUTAGE_ALERT_CYCLES = 30

    async def mmr_update_loop(self) -> None:
        team_data_manager = self._manager
        last_fail_count: Optional[int] = None
        last_server_info: Optional[dict] = None
        last_render_at: float = 0.0
        all_fail_cycles = 0
        try:
            # setup_scrim_dashboard와 동시 실행 시 충돌
            await asyncio.sleep(10)

            while True:
                sleep_interval = settings.MMR_UPDATE_INTERVAL_SECONDS
                final_run = False

                try:
                    current_time = get_current_kst_time()

                    if team_data_manager.is_team_assignment_started:
                        team_data_manager.mmr_update_task = None
                        return

                    final_run = (
                        team_data_manager.is_scrim_date_today()
                        and current_time.hour >= settings.TEAM_REGISTRATION_DEADLINE_HOUR
                    )

                    if team_data_manager.teams:
                        # 직전 주기가 점검이면 선수별 실패 경고를 DEBUG로, 점검 내내 같은 경고 반복 방지
                        success, fail = await self.update_all_team_mmr(quiet=team_data_manager.is_maintenance)

                        was_maintenance = team_data_manager.is_maintenance
                        if fail > 0 and success == 0:
                            team_data_manager.is_maintenance = True
                            sleep_interval = settings.MMR_UPDATE_MAINTENANCE_INTERVAL_SECONDS
                            logger.info(
                                f"[MMR갱신] 서버 점검 감지 - 실패: {fail}팀, "
                                f"갱신 주기 {settings.MMR_UPDATE_MAINTENANCE_INTERVAL_SECONDS // 60}분"
                            )
                            all_fail_cycles += 1
                            if all_fail_cycles == self.OUTAGE_ALERT_CYCLES:
                                logger.error(
                                    f"[MMR갱신] BSER 조회 전체 실패 지속 - 연속: {all_fail_cycles}회, 실패: {fail}팀"
                                )
                        else:
                            all_fail_cycles = 0
                            team_data_manager.is_maintenance = False
                            if success > 0:
                                team_data_manager.mark_mmr_success()

                        if not team_data_manager.is_maintenance and team_data_manager.unverified_teams:
                            if was_maintenance:
                                logger.info("[MMR갱신] 서버 점검 해제 감지")
                            await self.verify_unverified_teams()

                        maintenance_changed = was_maintenance != team_data_manager.is_maintenance
                        fail_changed = last_fail_count is not None and fail != last_fail_count
                        last_fail_count = fail

                        channel = team_data_manager.resolve_mmr_channel()
                        if channel:
                            info = await asyncio.to_thread(get_server_info)
                            server_changed = info != last_server_info
                            stale = (time.monotonic() - last_render_at) >= self.RENDER_BACKSTOP_SECONDS

                            if (not final_run and not team_data_manager._mmr_dirty
                                    and not maintenance_changed and not fail_changed
                                    and not server_changed and not stale):
                                logger.debug("[MMR갱신] 변경 없음 - MMR 메시지 갱신 스킵")
                            else:
                                await self.update_mmr_message(channel, mmr_fail_count=fail)
                                last_server_info = info
                                last_render_at = time.monotonic()
                except discord.NotFound:
                    team_data_manager.mmr_message = None
                except Exception as e:
                    logger.error(f"[MMR갱신] 업데이트 루프 실패: {e}", exc_info=True)

                if final_run:
                    logger.info(f"[MMR갱신] {settings.TEAM_REGISTRATION_DEADLINE_HOUR}시 최종 갱신 완료, 루프 종료")
                    team_data_manager.mmr_update_task = None
                    return

                await asyncio.sleep(sleep_interval)
        except asyncio.CancelledError:
            team_data_manager.mmr_update_task = None
        except Exception as e:
            logger.error(f"[MMR갱신] 업데이트 루프 종료: {e}", exc_info=True)
            team_data_manager.mmr_update_task = None

    TEAM_MMR_TTL_SECONDS = 600
    TEAM_FETCH_CONCURRENCY = 3

    async def update_all_team_mmr(self, force: bool = False, quiet: bool = False) -> Tuple[int, int]:
        """반환: 성공 팀 수, 실패 팀 수. TTL 스킵도 성공으로 집계. quiet는 선수별 실패 로그를 DEBUG로."""
        mgr = self._manager
        success_count = 0
        fail_count = 0
        try:
            team_processor = BotManager.get_instance().get_team_processor()

            await team_processor.ensure_test_accounts_loaded()

            current_time = get_current_kst_time()
            skipped = 0

            targets = []
            for team_name, team_data in dict(mgr.teams).items():
                if not force and team_data.mmr_updated_at:
                    elapsed = (current_time - team_data.mmr_updated_at).total_seconds()
                    if elapsed < self.TEAM_MMR_TTL_SECONDS:
                        skipped += 1
                        continue
                targets.append((team_name, team_data))
            success_count += skipped

            if targets:
                semaphore = asyncio.Semaphore(self.TEAM_FETCH_CONCURRENCY)

                async def _refresh(team_name, team_data, api_client) -> bool:
                    try:
                        async with semaphore:
                            _, _, team_mmr = await team_processor.fetch_team_mmr(
                                team_name, team_data, api_client=api_client
                            )
                        if team_mmr > 0:
                            await mgr.set_team_mmr(team_name, team_mmr)
                            return True
                    except Exception as e:
                        logger.error(f"[MMR갱신] 팀 MMR 갱신 실패 - 팀명: {team_name}: {e}", exc_info=True)
                    return False

                async with BSERAPIClient(quiet=quiet) as api_client:
                    results = await asyncio.gather(
                        *(_refresh(name, data, api_client) for name, data in targets)
                    )
                success_count += sum(results)
                fail_count += len(results) - sum(results)

            if skipped > 0:
                logger.debug(f"[MMR갱신] {skipped}개 팀은 TTL 안에 갱신돼 조회 생략")

        except Exception as e:
            logger.error(f"[MMR갱신] 전체 팀 MMR 갱신 실패: {e}", exc_info=True)

        if success_count > 0:
            mgr.save_backup()

        return success_count, fail_count

    async def verify_unverified_teams(self) -> None:
        mgr = self._manager
        if not mgr.unverified_teams:
            return

        team_processor = BotManager.get_instance().get_team_processor()
        await team_processor.ensure_test_accounts_loaded()
        teams_to_check = list(mgr.unverified_teams)
        logger.info(f"[점검해제] 미검증 팀 {len(teams_to_check)}개 재검증 시작")

        deferred = 0
        maintenance_checked = False
        async with BSERAPIClient() as api:
            for index, team_name in enumerate(teams_to_check):
                if team_name not in mgr.teams:
                    mgr.clear_unverified(team_name)
                    continue

                team_data = mgr.teams[team_name]
                try:
                    lookups = {}
                    for member in team_data.all_members:
                        if team_processor.is_test_account(member):
                            continue
                        lookups[member], _ = await api.lookup_user_uid(member)

                    if UID_ERROR in lookups.values():
                        deferred += 1
                        logger.info(f"[점검해제] 닉네임 조회 실패, 다음 주기에 재검증 - 팀: {team_name}")
                        continue

                    invalid_members = [m for m, status in lookups.items() if status == UID_NOT_FOUND]
                    # 점검 중에는 모든 닉네임이 404라 첫 404에서 점검 여부를 한 번 확인
                    if invalid_members and not maintenance_checked:
                        maintenance_checked = True
                        if await api.check_server_maintenance():
                            deferred += len(teams_to_check) - index
                            logger.info("[점검해제] 닉네임 검색이 아직 점검 중, 다음 주기에 재검증")
                            break

                    team_mmr = 0.0
                    if not invalid_members:
                        _, _, team_mmr = await team_processor.fetch_team_mmr(team_name, team_data, api_client=api)
                        if team_mmr > 0:
                            await mgr.set_team_mmr(team_name, team_mmr)

                    dm_sent = await self._send_verification_dm(team_name, team_data, invalid_members, team_mmr)
                    if invalid_members:
                        await self._notify_verification_failure(team_name, team_data, invalid_members, dm_sent)

                    mgr.clear_unverified(team_name)

                except Exception as e:
                    logger.error(f"[점검해제] 팀 재검증 실패 - {team_name}: {e}", exc_info=True)

        logger.info(
            f"[점검해제] 미검증 팀 재검증 완료 - 잔여: {len(mgr.unverified_teams)}개, 다음 주기로 미룸: {deferred}개"
        )

    async def _notify_verification_failure(self, team_name: str, team_data, invalid_members: list, dm_sent: bool) -> None:
        client = self._manager.client
        channel = client.get_channel(settings.LOG_CHANNEL_ID) if client else None
        if not channel:
            logger.warning(f"[점검해제] 로그 채널을 찾을 수 없음 - 채널 ID: {settings.LOG_CHANNEL_ID}")
            return
        applicant = f"<@{team_data.user_id}>" if team_data.user_id else "없음"
        content = (
            f"⚠️ 점검 해제 뒤 닉네임 확인 실패\n"
            f"팀: **{team_name}**\n"
            f"확인 안 된 닉네임: {', '.join(invalid_members)}\n"
            f"신청자: {applicant}, DM {'발송함' if dm_sent else '발송 못 함'}"
        )
        try:
            await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            logger.warning(f"[점검해제] 로그 채널 알림 실패 - {team_name}: {e}")

    async def _send_verification_dm(self, team_name: str, team_data, invalid_members: list, team_mmr: float = 0.0) -> bool:
        mgr = self._manager
        try:
            if not mgr.client:
                return False

            user_id = team_data.user_id
            if not user_id:
                return False

            user = mgr.client.get_user(int(user_id))
            if not user:
                try:
                    user = await mgr.client.fetch_user(int(user_id))
                except Exception:
                    return False

            players_str = ', '.join(team_data.players)
            view = LayoutView()

            if not invalid_members:
                mmr_line = (
                    f"MMR: **{team_mmr:.2f}**" if team_mmr > 0
                    else "MMR은 다음 갱신 때 반영됩니다."
                )
                content = (
                    f"## ✅ 닉네임 확인 완료\n"
                    f"**{team_name}** 팀의 닉네임을 확인했습니다.\n\n"
                    f"선수: {players_str}\n"
                    f"{mmr_line}"
                )
                accent = discord.Color.green()
            else:
                content = (
                    f"## ⚠️ 닉네임 확인 실패\n"
                    f"**{team_name}** 팀의 닉네임 중 게임에서 찾을 수 없는 닉네임이 있습니다.\n\n"
                    f"확인 안 된 닉네임: **{', '.join(invalid_members)}**\n\n"
                    f"게임 안에서 닉네임을 확인하고 "
                    f"<#{settings.SCRIM_CHANNEL_ID}> 채널의 **신청/수정** 버튼으로 "
                    f"{settings.TEAM_REGISTRATION_DEADLINE_HOUR}시 전에 고쳐주세요."
                )
                accent = discord.Color.red()

            view.add_item(Container(
                TextDisplay(content=content),
                Separator(),
                TextDisplay(content=FOOTER_TEXT),
                accent_colour=accent,
            ))

            await user.send(view=view)
            logger.info(f"[점검해제] DM 발송 - {team_name}, 결과: {'확인 완료' if not invalid_members else '확인 실패'}")
            return True

        except discord.Forbidden:
            logger.warning(f"[점검해제] DM 발송 실패, DM 차단 - {team_name}")
        except Exception as e:
            logger.error(f"[점검해제] DM 발송 실패 - {team_name}: {e}", exc_info=True)
        return False
