"""MMR 갱신 및 닉네임 검증 모듈

주기적 MMR 업데이트 루프, MMR 메시지 관리, 점검 해제 후 닉네임 재검증을 담당합니다.
"""
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

from services.bser_api import BSERAPIClient
from services.image_generator import TOURNAMENT_COLOR, ImageGenerator
from services.notion_api import get_server_info

if TYPE_CHECKING:
    from .team_data_manager import TeamDataManager

logger = get_logger('mmr_updater')


class MmrUpdater:
    """MMR 갱신 및 닉네임 검증을 담당하는 클래스"""

    def __init__(self, manager: "TeamDataManager"):
        self._manager = manager

    async def update_mmr_message(self, channel: discord.TextChannel, mmr_fail_count: int = 0) -> None:
        mgr = self._manager
        # TTL 캐시 미스 시에만 실제 Notion 호출
        info = await asyncio.to_thread(get_server_info)
        operate = info['operate']
        try:
            # 조편성 시작 이후인지 확인
            if mgr.is_team_assignment_started:
                logger.warning("[MMR메시지] 조편성 시작 이후이므로 갱신 불가")
                return

            # 시드팀 마킹 (이미지에 시드 여부 표시)
            try:
                team_processor = BotManager.get_instance().get_team_processor()
                await team_processor.ensure_seeds_marked(mgr.teams)
            except Exception as e:
                logger.warning(f"[MMR메시지] 시드 마킹 실패 (계속 진행): {e}")

            # 이미지 생성 (조회해둔 서버 정보 전달). 렌더 스레드 중 팀 변경으로
            # dict가 바뀌지 않도록 스냅샷을 넘긴다
            img_io = await ImageGenerator.generate_mmr_image_async(
                dict(mgr.teams), unverified_teams=set(mgr.unverified_teams), server_info=info
            )

            if not img_io:
                logger.error("[MMR메시지] 이미지 생성 실패", exc_info=True)
                return

            # MMR LayoutView 생성 (이미지 → 운영 정보 순서)
            update_time = mgr._last_success_time or get_current_kst_time().strftime('%H:%M')
            if mgr.is_maintenance:
                desc = f"🔧 서버 점검 중 / 마지막 갱신: `{update_time}`"
            else:
                desc = f"총 **{len(mgr.teams)}**팀 / 마지막 갱신: `{update_time}`"
                if mmr_fail_count > 0:
                    desc += f"\n⚠️ {mmr_fail_count}개 팀 MMR 갱신 실패"

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

            # 기존 메시지 편집 시도 (메모리 참조 또는 백업 ID)
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
                    # 편집 실패 시 기존 메시지 삭제 후 새로 생성
                    try:
                        await mgr.mmr_message.delete()
                    except Exception:
                        pass
                    mgr.mmr_message = None
                    mgr.mmr_message_id = None

            # 새로 생성
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

    # 스킵 조건이 놓친 표시 변화가 있어도 이 주기 안에는 화면에 반영된다
    RENDER_BACKSTOP_SECONDS = 1800

    async def mmr_update_loop(self) -> None:
        team_data_manager = self._manager
        last_fail_count: Optional[int] = None  # 마지막 사이클의 실패 팀 수 (실패 경고 표시 변화 감지용)
        last_server_info: Optional[dict] = None  # 마지막 렌더 시점의 서버 정보 (Live/Tournament 전환 감지용)
        last_render_at: float = 0.0
        try:
            # 첫 실행은 대기 후 시작 (setup_scrim_dashboard와 충돌 방지)
            await asyncio.sleep(10)

            while True:
                sleep_interval = settings.MMR_UPDATE_INTERVAL_SECONDS
                final_run = False

                try:
                    current_time = get_current_kst_time()

                    # 조편성 시작 후에는 즉시 중단
                    if team_data_manager.is_team_assignment_started:
                        team_data_manager.mmr_update_task = None
                        return

                    # 스크림 당일 마감 시각 이후면 마지막 갱신 1회 후 종료
                    final_run = (
                        team_data_manager.is_scrim_date_today()
                        and current_time.hour >= settings.TEAM_REGISTRATION_DEADLINE_HOUR
                    )

                    # 팀이 있는 경우 MMR 갱신
                    if team_data_manager.teams:
                        success, fail = await self.update_all_team_mmr()

                        # 점검 감지: 전체 실패 시 점검으로 판정
                        was_maintenance = team_data_manager.is_maintenance
                        if fail > 0 and success == 0:
                            team_data_manager.is_maintenance = True
                            sleep_interval = settings.MMR_UPDATE_MAINTENANCE_INTERVAL_SECONDS
                            logger.info(
                                f"[MMR갱신] 서버 점검 감지 - 실패: {fail}팀, "
                                f"갱신 주기 {settings.MMR_UPDATE_MAINTENANCE_INTERVAL_SECONDS // 60}분"
                            )
                        else:
                            team_data_manager.is_maintenance = False
                            # 실제 갱신 성공 시에만 마지막 갱신 시각 업데이트
                            if success > 0:
                                team_data_manager.mark_mmr_success()

                        # 정상 상태에서 잔여 미검증 팀 재검증 (점검 해제, 봇 재시작, 이전 검증 실패 등)
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

                            # MMR/팀 구성/점검 상태/실패 수/서버 정보 모두 무변화면 재렌더+재업로드 스킵
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
            # 태스크 참조만 정리 (재시작은 외부에서 관리)
            team_data_manager.mmr_update_task = None

    # 팀별 MMR 재조회 최소 간격 (초)
    TEAM_MMR_TTL_SECONDS = 600

    async def update_all_team_mmr(self, force: bool = False) -> Tuple[int, int]:
        """모든 팀의 MMR을 갱신합니다 (TTL 이내 갱신된 팀은 스킵).

        Args:
            force: True면 TTL 캐시를 무시하고 모든 팀을 실제로 재조회합니다
                (조편성 직전 마지막 갱신 등에 사용).

        Returns:
            Tuple[int, int]: (성공 팀 수, 실패 팀 수)
        """
        mgr = self._manager
        success_count = 0
        fail_count = 0
        try:
            team_processor = BotManager.get_instance().get_team_processor()

            # 시트에 새로 추가된 테스트 계정도 인식되도록 MMR 조회 직전 재로드
            await team_processor.ensure_test_accounts_loaded()

            current_time = get_current_kst_time()
            skipped = 0

            # 딕셔너리 순회 중 변경을 방지하기 위해 복사본 사용
            teams_copy = dict(mgr.teams)
            for team_name, team_data in teams_copy.items():
                try:
                    # TTL 이내 갱신된 팀은 스킵 (force면 무시하고 실제 재조회)
                    if not force and team_data.mmr_updated_at:
                        elapsed = (current_time - team_data.mmr_updated_at).total_seconds()
                        if elapsed < self.TEAM_MMR_TTL_SECONDS:
                            skipped += 1
                            success_count += 1
                            continue

                    _, _, team_mmr = await team_processor.fetch_team_mmr(team_name, team_data)
                    if team_mmr > 0:
                        await mgr.set_team_mmr(team_name, team_mmr)
                        success_count += 1
                    else:
                        # API 실패로 MMR 0 반환 → 실패로 처리 (mmr_updated_at 미갱신)
                        fail_count += 1

                except Exception as e:
                    logger.error(f"[MMR갱신] 팀 MMR 갱신 실패 - 팀명: {team_name}: {e}", exc_info=True)
                    fail_count += 1
                    continue

            if skipped > 0:
                logger.debug(f"[MMR갱신] {skipped}개 팀 캐시 히트 (TTL 이내 갱신됨)")

        except Exception as e:
            logger.error(f"[MMR갱신] 전체 팀 MMR 갱신 실패: {e}", exc_info=True)

        if success_count > 0:
            mgr.save_backup()

        return success_count, fail_count

    async def verify_unverified_teams(self) -> None:
        """점검 해제 후 미검증 팀의 닉네임을 재검증하고 DM을 발송합니다."""
        mgr = self._manager
        if not mgr.unverified_teams:
            return

        team_processor = BotManager.get_instance().get_team_processor()
        await team_processor.ensure_test_accounts_loaded()
        teams_to_check = list(mgr.unverified_teams)
        logger.info(f"[점검해제] 미검증 팀 {len(teams_to_check)}개 재검증 시작")

        for team_name in teams_to_check:
            if team_name not in mgr.teams:
                mgr.clear_unverified(team_name)
                continue

            team_data = mgr.teams[team_name]
            players = list(team_data.players)
            invalid_members = []

            try:
                async with BSERAPIClient() as api:
                    for player in players:
                        if team_processor.is_test_account(player):
                            continue
                        uid = await api.get_user_uid(player)
                        if not uid:
                            invalid_members.append(player)

                # MMR 갱신 시도 (0 반환 시 기존 MMR 유지)
                if not invalid_members:
                    _, _, team_mmr = await team_processor.fetch_team_mmr(team_name, team_data)
                    if team_mmr > 0:
                        await mgr.set_team_mmr(team_name, team_mmr)

                await self._send_verification_dm(team_name, team_data, invalid_members)

                # 검증 완료 (성공/실패 모두 unverified에서 제거)
                mgr.clear_unverified(team_name)

            except Exception as e:
                logger.error(f"[점검해제] 팀 재검증 실패 - {team_name}: {e}", exc_info=True)

        logger.info(f"[점검해제] 미검증 팀 재검증 완료 - 잔여: {len(mgr.unverified_teams)}개")

    async def _send_verification_dm(self, team_name: str, team_data, invalid_members: list) -> None:
        """점검 해제 후 닉네임 검증 결과를 DM으로 발송합니다."""
        mgr = self._manager
        try:
            if not mgr.client:
                return

            user_id = team_data.user_id
            if not user_id:
                return

            user = mgr.client.get_user(int(user_id))
            if not user:
                try:
                    user = await mgr.client.fetch_user(int(user_id))
                except Exception:
                    return

            players_str = ', '.join(team_data.players)
            view = LayoutView()

            if not invalid_members:
                # 성공 DM
                mmr_val = f"{team_data.mmr:.0f}" if team_data.mmr else "0"
                content = (
                    f"## ✅ 닉네임 확인 완료\n"
                    f"**{team_name}** 팀의 닉네임이 정상 확인되었습니다.\n\n"
                    f"🎮 선수: {players_str}\n"
                    f"📊 MMR: **{mmr_val}**\n\n"
                    f"💡 MMR이 반영되었습니다."
                )
                view.add_item(Container(
                    TextDisplay(content=content),
                    Separator(),
                    TextDisplay(content=FOOTER_TEXT),
                    accent_colour=discord.Color.green(),
                ))
            else:
                # 실패 DM
                invalid_str = ', '.join(invalid_members)
                content = (
                    f"## ⚠️ 닉네임 확인 실패\n"
                    f"**{team_name}** 팀의 닉네임 확인에 실패했습니다.\n\n"
                    f"❌ 확인 실패: **{invalid_str}**\n\n"
                    f"💡 해당 닉네임을 게임 내에서 확인 후\n"
                    f"대시보드의 수정 버튼으로 수정해주세요."
                )
                view.add_item(Container(
                    TextDisplay(content=content),
                    Separator(),
                    TextDisplay(content=FOOTER_TEXT),
                    accent_colour=discord.Color.red(),
                ))

            await user.send(view=view)
            logger.info(f"[점검해제] DM 발송 - {team_name} ({'성공' if not invalid_members else '실패'})")

        except discord.Forbidden:
            logger.warning(f"[점검해제] DM 발송 실패 (DM 차단) - {team_name}")
        except Exception as e:
            logger.error(f"[점검해제] DM 발송 실패 - {team_name}: {e}", exc_info=True)
