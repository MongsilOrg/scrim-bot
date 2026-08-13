"""Discord 서비스 모듈

조편성 공지 전송, 역할 관리, 음성채널 이름 변경, 채널 메시지 삭제 등
Discord API 관련 작업을 담당합니다.
"""
import asyncio
from typing import List, Optional, Tuple, TYPE_CHECKING

import discord
from discord.ui import Container, LayoutView, MediaGallery, Separator, TextDisplay

from commands.ui.roster_views import GroupRosterView, build_rest_day_guide_view
from utils.layout_helpers import error_view, FOOTER_TEXT
from config.logging_config import get_logger
from config.settings import settings
from services.holidays_api import get_rest_day_info
from services.notion_api import get_server_info
from utils.helpers import (
    get_current_kst_time, get_group_letter,
    get_group_role_mention,
)
from utils.validators import member_name_keys, normalize_nickname_for_comparison

if TYPE_CHECKING:
    from models.team_data_manager import TeamDataManager
    from models.team_processor import TeamProcessor
    from models.team_data import TeamData

logger = get_logger('discord_service')

# 조별 공지 첨부 이미지 파일명 (attachment:// 참조와 일치해야 함)
GROUP_IMAGE_FILENAME = 'group_mmr_table.png'


class DiscordService:
    """Discord API 작업을 담당하는 클래스"""

    def __init__(self, processor: "TeamProcessor", team_data_manager: "TeamDataManager"):
        self._processor = processor
        self._team_data_manager = team_data_manager

    async def send_global_announcement(self, guild: discord.Guild, groups: List[List], unmatched_teams: List[Tuple[str, "TeamData", float]] = None) -> None:
        """전체 공지를 하나의 LayoutView로 전송합니다."""
        try:
            notice_channel = guild.get_channel(settings.NOTICE_CHANNEL_ID)
            if not notice_channel:
                logger.warning("[Discord] 전체 공지 채널을 찾을 수 없음")
                return

            date_str = get_current_kst_time().strftime('%m.%d')

            # LayoutView 구성: 헤더 + 조별 이미지들
            view = LayoutView()
            view.add_item(Container(
                TextDisplay(content=f"## 📢 {date_str} {settings.SCRIM_START_HOUR}시 스크림 조편성입니다"),
                accent_colour=discord.Color.green(),
            ))

            files = []
            for group_index, group in enumerate(groups):
                if not group:
                    continue

                group_letter = chr(65 + group_index)
                group_teams = {team_name: team_data for team_name, team_data, _ in group}

                img_io = await self._processor.generate_group_image(group_letter, group_teams)
                filename = f"{group_letter}조_mmr_table.png"

                children = [TextDisplay(content=f"### {group_letter}조")]
                if img_io:
                    children.append(MediaGallery(discord.MediaGalleryItem(media=f"attachment://{filename}")))
                    files.append(discord.File(img_io, filename=filename))
                else:
                    logger.warning(f"[Discord] 전체 공지 이미지 생성 실패 - {group_letter}조")

                view.add_item(Container(*children, accent_colour=discord.Color.blue()))

            # 예비팀 표시 (1~7팀, 이미지 테이블)
            if unmatched_teams:
                spare_teams_dict = {team_name: team_data for team_name, team_data, _ in unmatched_teams}

                img_io = await self._processor.generate_group_image("예비", spare_teams_dict)
                filename = "예비팀_mmr_table.png"

                children = [TextDisplay(content="### 예비팀")]
                if img_io:
                    children.append(MediaGallery(discord.MediaGalleryItem(media=f"attachment://{filename}")))
                    files.append(discord.File(img_io, filename=filename))
                else:
                    logger.warning("[Discord] 전체 공지 예비팀 이미지 생성 실패")

                view.add_item(Container(*children, accent_colour=discord.Color.orange()))

            # 푸터
            view.add_item(Container(
                Separator(),
                TextDisplay(content=FOOTER_TEXT),
            ))

            await notice_channel.send(view=view, files=files if files else None)

        except Exception as e:
            logger.error(f"[Discord] 전체 공지 전송 실패: {e}", exc_info=True)

    def _build_group_notice(self, guild: discord.Guild, group_letter: str, group: List[Tuple[str, "TeamData", float]], message: str, *, has_image: bool):
        """조별 역할 멘션을 붙인 본문과 GroupRosterView를 조립합니다."""
        role_mention = get_group_role_mention(guild, group_letter)
        if not role_mention:
            logger.warning(f"[Discord] 조별 역할을 찾을 수 없음 - 역할: {group_letter}조")
        full_message = role_mention + "\n" + message if role_mention else message

        roster_view = GroupRosterView(
            group_letter, group,
            message_text=full_message, has_image=has_image,
        )
        return full_message, roster_view

    async def send_group_announcement_with_image(self, channel: discord.TextChannel, message: str, group: List[Tuple[str, "TeamData", float]], is_rest_day: bool = False) -> None:
        """조별 MMR 이미지와 함께 공지를 전송합니다."""
        try:
            # 채널 ID로부터 조 이름 추출
            group_letter = get_group_letter(channel.id)
            if not group_letter:
                logger.warning(f"[Discord] 채널에 해당하는 조를 찾을 수 없음 - 채널 ID: {channel.id}")
                group_letter = "A"  # 기본값

            group_teams = {team_name: team_data for team_name, team_data, _ in group}
            img_io = await self._processor.generate_group_image(group_letter, group_teams)

            full_message, roster_view = self._build_group_notice(
                channel.guild, group_letter, group, message, has_image=bool(img_io),
            )

            if img_io:
                sent_message = await channel.send(
                    view=roster_view,
                    file=discord.File(img_io, filename=GROUP_IMAGE_FILENAME),
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            else:
                sent_message = await channel.send(
                    view=roster_view,
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
                logger.warning(f"[Discord] 이미지 생성 실패 - 채널: {channel.name}, 메시지만 전송")

            # message_id와 텍스트를 TeamDataManager에 저장
            team_data_manager = self._team_data_manager
            team_data_manager.group_message_ids[group_letter] = sent_message.id
            team_data_manager.group_message_texts[group_letter] = full_message
            team_data_manager.save_backup()

            # 공휴일/일요일: 1시드 팀 신청자 태그 + 자율 진행 안내 추가 뷰
            # (추가 뷰 실패가 본 공지 성공을 가리지 않도록 별도 예외 처리)
            if is_rest_day and group:
                try:
                    top_team_name, top_team_data, _ = group[0]
                    top_user_id = top_team_data.user_id

                    guide_view = build_rest_day_guide_view(top_team_name, top_user_id)
                    await channel.send(
                        view=guide_view,
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except Exception as e:
                    logger.error(f"[Discord] 자율 진행 안내 전송 실패 - 조: {group_letter}조: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"[Discord] 조별 공지 전송 실패 - 채널: {channel.name}: {e}", exc_info=True)
            try:
                await channel.send(view=error_view(f"조별 공지 전송 중 오류가 발생했습니다.\n{message}"))
            except Exception as e2:
                logger.error(f"[Discord] 에러 메시지 전송 실패 - 채널: {channel.name}: {e2}", exc_info=True)

    async def update_single_group_announcement(self, channel: discord.TextChannel, group_letter: str, group_teams: List[Tuple[str, "TeamData", float]]) -> None:
        """저장된 조별 공지 메시지를 새 로스터로 수정합니다 (로스터 변경 시 사용)."""
        try:
            # 저장된 message_id로 직접 fetch
            team_data_manager = self._team_data_manager
            message_id = team_data_manager.group_message_ids.get(group_letter)
            target_message = None
            if message_id:
                try:
                    target_message = await channel.fetch_message(message_id)
                except discord.NotFound:
                    logger.warning(f"[Discord] 저장된 공지 메시지를 찾을 수 없음 (id={message_id})")

            if not target_message:
                logger.warning(f"[Discord] 조별 공지 메시지를 찾을 수 없음 - 조: {group_letter}조")
                return

            message = self.create_group_announcement_message(group_letter, group_teams)
            # 로스터가 바뀌었으므로 조 캐시 무효화 후 재생성. 삽입 순서 = 팀 번호 순서 유지 (sort_by_mmr=False)
            teams_by_name = {team_name: team_data for team_name, team_data, _ in group_teams}
            img_io = await self._processor.generate_group_image(
                group_letter, teams_by_name, sort_by_mmr=False, refresh=True,
            )

            full_message, roster_view = self._build_group_notice(
                channel.guild, group_letter, group_teams, message, has_image=bool(img_io),
            )

            # 기존 메시지 수정
            if img_io:
                await target_message.edit(
                    view=roster_view,
                    content=None,
                    embed=None,
                    attachments=[discord.File(img_io, filename=GROUP_IMAGE_FILENAME)],
                )
            else:
                await target_message.edit(
                    view=roster_view,
                    content=None,
                    embed=None,
                )

            # 백업에 갱신된 텍스트 저장
            team_data_manager.group_message_texts[group_letter] = full_message
            team_data_manager.save_backup()

            logger.debug(f"[Discord] 조별 공지 업데이트 완료 - 조: {group_letter}조")

        except Exception as e:
            logger.error(f"[Discord] 기존 조별 공지 메시지 수정 실패: {e}", exc_info=True)

    async def send_notices(self, guild: discord.Guild, groups: List[List], unmatched_teams: List[Tuple[str, "TeamData", float]] = None) -> None:
        try:
            # 디스코드 역할 처리 (조별 공지보다 먼저)
            # 역할 핑과 채널 가시성이 '새 조 멤버'에게 올바로 가도록 공지보다 먼저 재배정한다.
            await self.handle_discord_roles(guild, groups)

            # 공휴일/일요일 여부 (자율 진행 안내 표시용)
            # 휴무일 조회 실패가 공지와 역할 흐름 전체를 막지 않게 한다
            try:
                is_rest_day = (await get_rest_day_info())["is_rest_day"]
            except Exception as e:
                logger.error(f"[Discord] 휴무일 정보 조회 실패, 자율 진행 안내 생략: {e}", exc_info=True)
                is_rest_day = False

            # 모든 조별 채널에 대해 처리 (팀이 있는 조와 없는 조 모두)
            for group_letter in settings.GROUP_CHANNEL_IDS.keys():
                try:
                    channel_id = settings.GROUP_CHANNEL_IDS.get(group_letter)

                    if channel_id:
                        channel = guild.get_channel(channel_id)
                        if channel:
                            # 조별 채널의 모든 메시지 삭제 (팀이 있든 없든)
                            await self.clear_channel_messages(channel)

                            # 해당 조에 팀이 있는지 확인
                            group_index = ord(group_letter) - ord('A')  # A=0, B=1, ...
                            # groups 리스트의 범위를 안전하게 체크하고, 해당 인덱스에 팀이 있는지 확인
                            if group_index < len(groups) and len(groups[group_index]) > 0:
                                # 팀이 있는 경우: 조별 공지 전송
                                group = groups[group_index]
                                message = self.create_group_announcement_message(group_letter, group)
                                await self.send_group_announcement_with_image(channel, message, group, is_rest_day=is_rest_day)
                            else:
                                # 팀이 없는 경우: 메시지만 삭제하고 공지는 전송하지 않음
                                pass
                        else:
                            logger.warning(f"[Discord] 조별 채널을 찾을 수 없음 - 조: {group_letter}조, 채널 ID: {channel_id}")
                    else:
                        logger.warning(f"[Discord] 조별 채널 ID가 설정되지 않음 - 조: {group_letter}조")
                except Exception as e:
                    # 각 조별 처리 중 오류가 발생해도 다른 조에 영향을 주지 않도록 개별 처리
                    logger.error(f"[Discord] 조별 공지 전송 실패 - 조: {group_letter}조: {e}", exc_info=True)
                    continue

            # 음성채널 이름 변경
            await self.rename_voice_channels(guild, groups)

        except Exception as e:
            logger.error(f"[Discord] 공지 전송 실패: {e}", exc_info=True)

    async def _retry_discord(self, coro_factory, *, error_message: str, retries: int = 3, base_delay: float = 0.2) -> None:
        """Discord API 호출을 재시도합니다. 최종 실패 시 로그만 남깁니다."""
        for retry in range(retries):
            try:
                await coro_factory()
                return
            except (discord.HTTPException, discord.Forbidden) as e:
                if retry == retries - 1:
                    logger.error(f"{error_message}: {e}", exc_info=True)
                else:
                    await asyncio.sleep(base_delay)

    async def _update_member_roles_with_retry(self, member, roles_to_remove, roles_to_add):
        """멤버의 역할을 재시도 로직과 함께 업데이트합니다."""
        try:
            if roles_to_remove:
                await self._retry_discord(
                    lambda: member.remove_roles(*roles_to_remove),
                    error_message=f"[Discord] 역할 제거 실패 - 멤버: {member.display_name}",
                )

            if roles_to_add:
                await self._retry_discord(
                    lambda: member.add_roles(*roles_to_add),
                    error_message=f"[Discord] 역할 추가 실패 - 멤버: {member.display_name}",
                )

        except Exception as e:
            logger.error(f"[Discord] 멤버 역할 업데이트 실패 - 멤버: {member.display_name}: {e}", exc_info=True)

    def _resolve_guild(self, guild: Optional[discord.Guild]) -> Optional[discord.Guild]:
        """guild가 없으면 클라이언트에서 조회합니다."""
        if not guild and self._processor.client:
            guild = self._processor.client.get_guild(settings.GUILD_ID)
            if not guild:
                raise ValueError(f"서버 정보를 찾을 수 없습니다. (ID: {settings.GUILD_ID})")
        return guild

    @staticmethod
    def _team_participants(teams: List[Tuple[str, "TeamData", float]]) -> set:
        """팀 목록에서 정규화된 참여자(선수+스태프) 명단을 만듭니다."""
        participants = set()
        for team_name, team_data, _ in teams:
            participants.update(normalize_nickname_for_comparison(member) for member in team_data.all_members)
        return participants

    @staticmethod
    def _build_role_updates(guild: discord.Guild, group_roles: dict, participants_by_letter: dict) -> list:
        """참여자 명단과 실제 역할 보유를 비교해 (member, 제거, 추가) 목록을 만듭니다."""
        role_values = set(group_roles.values())
        role_updates = []
        for member in guild.members:
            member_keys = member_name_keys(member)

            current_group_roles = set(role for role in member.roles if role in role_values)
            roles_to_add = set()

            for group_letter, participants in participants_by_letter.items():
                if member_keys & participants:
                    role = group_roles.get(group_letter)
                    if role:
                        roles_to_add.add(role)

            roles_to_remove = current_group_roles - roles_to_add
            roles_to_add = roles_to_add - current_group_roles

            if roles_to_remove or roles_to_add:
                role_updates.append((member, roles_to_remove, roles_to_add))
        return role_updates

    async def _apply_role_updates(self, role_updates: list) -> None:
        """역할 업데이트를 배치로 처리합니다 (Rate Limiting 고려)."""
        batch_size = 10
        for i in range(0, len(role_updates), batch_size):
            batch = role_updates[i:i + batch_size]
            tasks = [self._update_member_roles_with_retry(member, roles_to_remove, roles_to_add)
                    for member, roles_to_remove, roles_to_add in batch]
            await asyncio.gather(*tasks, return_exceptions=True)

            if i + batch_size < len(role_updates):
                await asyncio.sleep(0.5)

    async def handle_discord_roles(self, guild: discord.Guild, groups: List[List]) -> None:
        try:
            guild = self._resolve_guild(guild)

            # 설정된 모든 조의 역할 가져오기
            group_roles = {}
            for group_letter in settings.GROUP_CHANNEL_IDS:
                role_name = f"{group_letter}조"
                role = discord.utils.get(guild.roles, name=role_name)
                if role:
                    group_roles[group_letter] = role
                else:
                    logger.warning(f"[Discord] 역할을 찾을 수 없음 - 역할: {role_name}")

            # 오늘 경기 참여자 명단 생성 (그룹별)
            today_participants = {letter: set() for letter in group_roles.keys()}
            for group_idx, group in enumerate(groups):
                if not group:
                    continue
                group_letter = chr(65 + group_idx)
                today_participants[group_letter] |= self._team_participants(group)

            role_updates = self._build_role_updates(guild, group_roles, today_participants)
            await self._apply_role_updates(role_updates)

        except Exception as e:
            logger.error(f"[Discord] 역할 처리 실패: {e}", exc_info=True)

    async def update_group_roles(self, guild: discord.Guild, group_letter: str, group_teams: List[Tuple[str, "TeamData", float]]) -> None:
        """특정 조의 역할만 업데이트합니다 (로스터 변경 시 사용)."""
        try:
            guild = self._resolve_guild(guild)

            # 해당 조의 역할 가져오기
            role_name = f"{group_letter}조"
            group_role = discord.utils.get(guild.roles, name=role_name)
            if not group_role:
                logger.warning(f"[Discord] 역할을 찾을 수 없음 - 역할: {role_name}")
                return

            participants = self._team_participants(group_teams)
            role_updates = self._build_role_updates(
                guild, {group_letter: group_role}, {group_letter: participants}
            )
            await self._apply_role_updates(role_updates)

            logger.info(f"[Discord] 조별 역할 업데이트 완료 - 조: {group_letter}조, 변경된 멤버: {len(role_updates)}명")

        except Exception as e:
            logger.error(f"[Discord] 조별 역할 업데이트 실패: {e}", exc_info=True)

    @staticmethod
    def _sorted_group_voice_channels(guild: discord.Guild, group_letter: str) -> Optional[List[discord.VoiceChannel]]:
        """조 카테고리의 음성채널 목록 (position 순). 카테고리를 못 찾으면 None."""
        category_name = settings.GROUP_CATEGORY_PATTERN.format(letter=group_letter)
        if not category_name:
            logger.warning(f"[Discord] 카테고리 패턴이 설정되지 않음 - 조: {group_letter}조")
            return None

        category = discord.utils.get(guild.categories, name=category_name)
        if not category:
            logger.warning(f"[Discord] 카테고리를 찾을 수 없음 - 카테고리: {category_name}")
            return None

        voice_channels = [ch for ch in category.voice_channels if isinstance(ch, discord.VoiceChannel)]
        voice_channels.sort(key=lambda x: x.position)
        return voice_channels

    @staticmethod
    async def _safe_rename(voice_channel: discord.VoiceChannel, new_name: str, context: str) -> Optional[bool]:
        """음성채널 이름을 변경합니다. 변경 True, 실패 False, 변경 불필요 None."""
        if voice_channel.name == new_name:
            return None
        try:
            await voice_channel.edit(name=new_name)
            return True
        except discord.HTTPException as e:
            logger.error(f"[Discord] 음성채널 이름 변경 실패 - {context}, 채널: {voice_channel.name}: {e}", exc_info=True)
            return False
        except discord.Forbidden:
            logger.error(f"[Discord] 음성채널 이름 변경 권한 없음 - {context}, 채널: {voice_channel.name}")
            return False

    async def rename_group_voice_channel(self, guild: discord.Guild, group_letter: str, team_index: int, team_name: str) -> None:
        """단일 팀 슬롯의 음성채널 이름을 변경합니다 (로스터 변경 시 사용)."""
        voice_channels = self._sorted_group_voice_channels(guild, group_letter)
        if voice_channels is None:
            return
        if team_index < len(voice_channels):
            await self._safe_rename(voice_channels[team_index], f"{team_index + 1}. {team_name}", f"조: {group_letter}조")

    async def rename_voice_channels(self, guild: discord.Guild, groups: List[List]) -> None:
        """음성채널 이름을 조별로 변경합니다."""
        try:
            guild = self._resolve_guild(guild)

            # 설정된 모든 조에 대해 처리
            for group_letter in settings.GROUP_CHANNEL_IDS:
                group_index = ord(group_letter) - ord('A')

                voice_channels = self._sorted_group_voice_channels(guild, group_letter)
                if voice_channels is None:
                    continue

                if len(voice_channels) < settings.TEAMS_PER_GROUP:
                    category_name = settings.GROUP_CATEGORY_PATTERN.format(letter=group_letter)
                    logger.warning(f"[Discord] 카테고리 음성채널 부족 - 카테고리: {category_name}, 채널 수: {len(voice_channels)}개")

                # 팀 슬롯은 "번호. 팀명", 나머지는 TBD
                group = groups[group_index] if group_index < len(groups) else None
                targets = []
                team_count = 0
                if group:
                    team_count = len(group)
                    for i, (team_name, team_data, mmr) in enumerate(group):
                        if i < len(voice_channels):
                            targets.append((voice_channels[i], f"{i+1}. {team_name}"))
                        else:
                            logger.warning(f"[Discord] 음성채널 부족 - 조: {group_letter}조, 팀: {i+1}팀, 총 채널: {len(voice_channels)}개")
                for voice_channel in voice_channels[team_count:]:
                    targets.append((voice_channel, "TBD"))

                changed_count = 0
                error_count = 0
                for voice_channel, new_name in targets:
                    renamed = await self._safe_rename(voice_channel, new_name, f"조: {group_letter}조")
                    if renamed is True:
                        changed_count += 1
                    elif renamed is False:
                        error_count += 1

                if changed_count > 0 or error_count > 0:
                    logger.info(f"[Discord] 음성채널 이름 변경 완료 - 조: {group_letter}조, 변경: {changed_count}개, 오류: {error_count}개")

        except Exception as e:
            logger.error(f"[Discord] 음성채널 이름 변경 실패: {e}", exc_info=True)

    def create_group_announcement_message(self, group_letter: str, group: List[Tuple[str, "TeamData", float]]) -> str:
        current_time = get_current_kst_time()
        date_str = current_time.strftime('%m.%d')

        info = get_server_info()
        message = f"📢 {date_str} {settings.SCRIM_START_HOUR}시 스크림 {group_letter}조 조편성 결과\n{info['operate']}"

        return message

    async def clear_channel_messages(self, channel: discord.TextChannel) -> None:
        try:
            if not isinstance(channel, discord.TextChannel):
                return

            max_batches = 5
            batch_size = 200
            total_deleted = 0

            for _ in range(max_batches):
                deleted = await channel.purge(limit=batch_size, oldest_first=False, reason="Scrim auto-clean")
                batch_deleted = len(deleted)
                total_deleted += batch_deleted

                if batch_deleted == 0:
                    break

                await asyncio.sleep(0.7)

            if total_deleted > 0:
                logger.info(f"[Discord] 채널 메시지 삭제 완료 - 채널: {channel.name}, 삭제된 메시지: {total_deleted}개")

        except Exception as e:
            logger.error(f"[Discord] 채널 메시지 삭제 실패 - 채널: {channel.name}: {e}", exc_info=True)

