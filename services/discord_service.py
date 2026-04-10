"""Discord 서비스 모듈

조편성 공지 전송, 역할 관리, 음성채널 이름 변경, 채널 메시지 삭제 등
Discord API 관련 작업을 담당합니다.
"""
import asyncio
from typing import List, Tuple, TYPE_CHECKING

import discord

from commands.ui.layout_helpers import error_view, FOOTER_TEXT
from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_all_members

if TYPE_CHECKING:
    from models.team_processor import TeamProcessor
    from models.team_data import TeamData

logger = get_logger('discord_service')


class DiscordService:
    """Discord API 작업을 담당하는 클래스"""

    def __init__(self, processor: "TeamProcessor"):
        self._processor = processor

    async def send_global_announcement(self, guild: discord.Guild, groups: List[List], unmatched_teams: List[Tuple[str, "TeamData", float]] = None) -> None:
        """전체 공지를 하나의 LayoutView로 전송합니다."""
        try:
            from discord.ui import Container, LayoutView, MediaGallery, Separator, TextDisplay

            notice_channel = guild.get_channel(settings.NOTICE_CHANNEL_ID)
            if not notice_channel:
                logger.warning("[Discord] 전체 공지 채널을 찾을 수 없음")
                return

            from utils.helpers import get_current_kst_time
            date_str = get_current_kst_time().strftime('%m.%d')

            # LayoutView 구성: 헤더 + 조별 이미지들
            view = LayoutView()
            view.add_item(Container(
                TextDisplay(content=f"## 📢 {date_str} 20시 스크림 조편성입니다"),
                accent_colour=discord.Color.green(),
            ))

            files = []
            for group_index, group in enumerate(groups):
                if not group:
                    continue

                group_letter = chr(65 + group_index)
                group_teams = {}
                group_mmr_averages = {}
                for team_name, team_data, team_mmr in group:
                    group_teams[team_name] = team_data
                    group_mmr_averages[team_name] = team_mmr

                img_io = self._processor._generate_group_image(group_letter, group_teams, group_mmr_averages)
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
                spare_teams_dict = {}
                spare_mmr_dict = {}
                for team_name, team_data, team_mmr in unmatched_teams:
                    spare_teams_dict[team_name] = team_data
                    spare_mmr_dict[team_name] = team_mmr

                img_io = self._processor._generate_group_image("예비", spare_teams_dict, spare_mmr_dict)
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

    async def send_group_announcement_with_image(self, channel: discord.TextChannel, message: str, group: List[Tuple[str, "TeamData", float]]) -> None:
        """조별 MMR 이미지와 함께 공지를 전송합니다."""
        try:
            # 채널 ID로부터 조 이름 추출
            group_letter = None
            for letter, channel_id in settings.GROUP_CHANNEL_IDS.items():
                if channel_id == channel.id:
                    group_letter = letter
                    break

            if not group_letter:
                logger.warning(f"[Discord] 채널에 해당하는 조를 찾을 수 없음 - 채널 ID: {channel.id}")
                group_letter = "A"  # 기본값

            # 캐시된 이미지가 있는지 확인 (LRU: 접근한 항목을 맨 뒤로 이동)
            img_io = None
            if group_letter in self._processor.group_image_cache:
                # 캐시 히트: 항목을 맨 뒤로 이동 (최근 사용)
                img_data = self._processor.group_image_cache.pop(group_letter)
                self._processor.group_image_cache[group_letter] = img_data
                # BytesIO로 변환
                from io import BytesIO
                img_io = BytesIO(img_data)
            else:
                # 캐시에 없으면 새로 생성
                group_teams = {}
                group_mmr_averages = {}

                for team_name, team_data, team_mmr in group:
                    group_teams[team_name] = team_data
                    group_mmr_averages[team_name] = team_mmr

                img_io = self._processor._generate_group_image(group_letter, group_teams, group_mmr_averages)

            # 조별 역할 멘션 추가
            role_mention = await self._get_group_role_mention(channel.guild, group_letter)
            full_message = role_mention + "\n" + message if role_mention else message

            # 조별 로스터 관리 뷰 생성 (공지 텍스트 + 이미지 + 버튼 포함)
            from commands.ui.views import GroupRosterView
            roster_view = GroupRosterView(
                group_letter, group,
                message_text=full_message, has_image=bool(img_io),
            )

            if img_io:
                sent_message = await channel.send(
                    view=roster_view,
                    file=discord.File(img_io, filename='group_mmr_table.png'),
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            else:
                sent_message = await channel.send(
                    view=roster_view,
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
                logger.warning(f"[Discord] 이미지 생성 실패 - 채널: {channel.name}, 메시지만 전송")

            # message_id와 텍스트를 TeamDataManager에 저장
            from bot.manager import BotManager
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            team_data_manager.group_message_ids[group_letter] = sent_message.id
            team_data_manager.group_message_texts[group_letter] = full_message
            team_data_manager._save_backup()

        except Exception as e:
            logger.error(f"[Discord] 조별 공지 전송 실패 - 채널: {channel.name}: {e}", exc_info=True)
            try:
                await channel.send(view=error_view(f"조별 공지 전송 중 오류가 발생했습니다.\n{message}"))
            except Exception as e2:
                logger.error(f"[Discord] 에러 메시지 전송 실패 - 채널: {channel.name}: {e2}", exc_info=True)

    async def send_notices(self, guild: discord.Guild, groups: List[List], unmatched_teams: List[Tuple[str, "TeamData", float]] = None) -> None:
        """공지를 전송합니다."""
        try:
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
                                await self.send_group_announcement_with_image(channel, message, group)
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

            # 디스코드 역할 처리
            await self.handle_discord_roles(guild, groups)

            # 음성채널 이름 변경
            await self.rename_voice_channels(guild, groups)

        except Exception as e:
            logger.error(f"[Discord] 공지 전송 실패: {e}", exc_info=True)

    async def _update_member_roles_with_retry(self, member, roles_to_remove, roles_to_add):
        """멤버의 역할을 재시도 로직과 함께 업데이트합니다."""
        try:
            if roles_to_remove:
                max_retries = 3
                for retry in range(max_retries):
                    try:
                        await member.remove_roles(*roles_to_remove)
                        break
                    except (discord.HTTPException, discord.Forbidden) as e:
                        if retry == max_retries - 1:
                            logger.error(f"[Discord] 역할 제거 실패 - 멤버: {member.display_name}: {e}", exc_info=True)
                        else:
                            await asyncio.sleep(0.2)

            if roles_to_add:
                max_retries = 3
                for retry in range(max_retries):
                    try:
                        await member.add_roles(*roles_to_add)
                        break
                    except (discord.HTTPException, discord.Forbidden) as e:
                        if retry == max_retries - 1:
                            logger.error(f"[Discord] 역할 추가 실패 - 멤버: {member.display_name}: {e}", exc_info=True)
                        else:
                            await asyncio.sleep(0.2)

        except Exception as e:
            logger.error(f"[Discord] 멤버 역할 업데이트 실패 - 멤버: {member.display_name}: {e}", exc_info=True)

    async def handle_discord_roles(self, guild: discord.Guild, groups: List[List]) -> None:
        """디스코드 역할을 처리합니다."""
        try:
            if not guild and self._processor.client:
                guild = self._processor.client.get_guild(settings.GUILD_ID)
                if not guild:
                    raise ValueError(f"서버 정보를 찾을 수 없습니다. (ID: {settings.GUILD_ID})")

            # 1. 모든 조 역할 가져오기 (A조 ~ F조)
            group_roles = {}
            for group_letter in ['A', 'B', 'C', 'D', 'E', 'F']:
                role_name = f"{group_letter}조"
                role = discord.utils.get(guild.roles, name=role_name)
                if role:
                    group_roles[group_letter] = role
                else:
                    logger.warning(f"[Discord] 역할을 찾을 수 없음 - 역할: {role_name}")

            # 2. 오늘 경기 참여자 명단 생성 (그룹별)
            from utils.validators import normalize_nickname_for_comparison

            today_participants = {letter: set() for letter in group_roles.keys()}

            for group_idx, group in enumerate(groups):
                if not group:
                    continue
                group_letter = chr(65 + group_idx)

                for team_name, team_data, _ in group:
                    members = get_all_members(team_data)
                    today_participants[group_letter].update(normalize_nickname_for_comparison(member) for member in members)

            # 3. 역할 변경이 필요한 멤버 목록 생성
            role_updates = []
            for member in guild.members:
                member_display_name = normalize_nickname_for_comparison(member.display_name)
                member_global_name = normalize_nickname_for_comparison(member.global_name) if member.global_name else ""
                member_name = normalize_nickname_for_comparison(member.name)

                current_group_roles = set(role for role in member.roles if role in group_roles.values())
                roles_to_add = set()

                for group_letter, participants in today_participants.items():
                    if (member_display_name in participants or
                        member_global_name in participants or
                        member_name in participants):
                        role = group_roles.get(group_letter)
                        if role:
                            roles_to_add.add(role)

                roles_to_remove = current_group_roles - roles_to_add
                roles_to_add = roles_to_add - current_group_roles

                if roles_to_remove or roles_to_add:
                    role_updates.append((member, roles_to_remove, roles_to_add))

            # 4. 역할 업데이트를 배치로 처리 (Rate Limiting 고려)
            if role_updates:
                batch_size = 10
                for i in range(0, len(role_updates), batch_size):
                    batch = role_updates[i:i + batch_size]
                    tasks = [self._update_member_roles_with_retry(member, roles_to_remove, roles_to_add)
                            for member, roles_to_remove, roles_to_add in batch]
                    await asyncio.gather(*tasks, return_exceptions=True)

                    if i + batch_size < len(role_updates):
                        await asyncio.sleep(0.5)

        except Exception as e:
            logger.error(f"[Discord] 역할 처리 실패: {e}", exc_info=True)

    async def update_group_roles(self, guild: discord.Guild, group_letter: str, group_teams: List[Tuple[str, "TeamData", float]]) -> None:
        """특정 조의 역할만 업데이트합니다 (로스터 변경 시 사용)."""
        try:
            from utils.validators import normalize_nickname_for_comparison

            if not guild and self._processor.client:
                guild = self._processor.client.get_guild(settings.GUILD_ID)
                if not guild:
                    raise ValueError(f"서버 정보를 찾을 수 없습니다. (ID: {settings.GUILD_ID})")

            # 해당 조의 역할 가져오기
            role_name = f"{group_letter}조"
            group_role = discord.utils.get(guild.roles, name=role_name)
            if not group_role:
                logger.warning(f"[Discord] 역할을 찾을 수 없음 - 역할: {role_name}")
                return

            # 해당 조의 참여자 명단 생성
            participants = set()
            for team_name, team_data, _ in group_teams:
                members = get_all_members(team_data)
                participants.update(normalize_nickname_for_comparison(member) for member in members)

            # 역할 변경이 필요한 멤버 목록 생성
            role_updates = []
            for member in guild.members:
                member_display_name = normalize_nickname_for_comparison(member.display_name)
                member_global_name = normalize_nickname_for_comparison(member.global_name) if member.global_name else ""
                member_name = normalize_nickname_for_comparison(member.name)

                has_group_role = group_role in member.roles
                should_have_role = (member_display_name in participants or
                                   member_global_name in participants or
                                   member_name in participants)

                if should_have_role and not has_group_role:
                    role_updates.append((member, None, [group_role]))
                elif not should_have_role and has_group_role:
                    role_updates.append((member, [group_role], None))

            # 역할 업데이트를 배치로 처리
            if role_updates:
                batch_size = 10
                for i in range(0, len(role_updates), batch_size):
                    batch = role_updates[i:i + batch_size]
                    tasks = [self._update_member_roles_with_retry(member, roles_to_remove, roles_to_add)
                            for member, roles_to_remove, roles_to_add in batch]
                    await asyncio.gather(*tasks, return_exceptions=True)

                    if i + batch_size < len(role_updates):
                        await asyncio.sleep(0.5)

            logger.info(f"[Discord] 조별 역할 업데이트 완료 - 조: {group_letter}조, 변경된 멤버: {len(role_updates)}명")

        except Exception as e:
            logger.error(f"[Discord] 조별 역할 업데이트 실패: {e}", exc_info=True)

    async def rename_voice_channels(self, guild: discord.Guild, groups: List[List]) -> None:
        """음성채널 이름을 조별로 변경합니다."""
        try:
            if not guild and self._processor.client:
                guild = self._processor.client.get_guild(settings.GUILD_ID)
                if not guild:
                    raise ValueError(f"서버 정보를 찾을 수 없습니다. (ID: {settings.GUILD_ID})")

            # 모든 조 (A~F)에 대해 처리
            for group_letter in ['A', 'B', 'C', 'D', 'E', 'F']:
                group_index = ord(group_letter) - ord('A')
                category_name = settings.GROUP_CATEGORY_PATTERN.format(letter=group_letter)

                if not category_name:
                    logger.warning(f"[Discord] 카테고리 패턴이 설정되지 않음 - 조: {group_letter}조")
                    continue

                category = discord.utils.get(guild.categories, name=category_name)
                if not category:
                    logger.warning(f"[Discord] 카테고리를 찾을 수 없음 - 카테고리: {category_name}")
                    continue

                voice_channels = [ch for ch in category.voice_channels if isinstance(ch, discord.VoiceChannel)]
                voice_channels.sort(key=lambda x: x.position)

                if len(voice_channels) < 8:
                    logger.warning(f"[Discord] 카테고리 음성채널 부족 - 카테고리: {category_name}, 채널 수: {len(voice_channels)}개")

                changed_count = 0
                error_count = 0

                if group_index < len(groups) and groups[group_index]:
                    group = groups[group_index]
                    for i, (team_name, team_data, mmr) in enumerate(group):
                        if i < len(voice_channels):
                            voice_channel = voice_channels[i]
                            new_name = f"{i+1}. {team_name}"

                            if voice_channel.name != new_name:
                                try:
                                    await voice_channel.edit(name=new_name)
                                    changed_count += 1
                                except discord.HTTPException as e:
                                    logger.error(f"[Discord] 음성채널 이름 변경 실패 - 조: {group_letter}조, 채널: {voice_channel.name}: {e}", exc_info=True)
                                    error_count += 1
                                except discord.Forbidden:
                                    logger.error(f"[Discord] 음성채널 이름 변경 권한 없음 - 조: {group_letter}조, 채널: {voice_channel.name}")
                                    error_count += 1
                        else:
                            logger.warning(f"[Discord] 음성채널 부족 - 조: {group_letter}조, 팀: {i+1}팀, 총 채널: {len(voice_channels)}개")

                    # 남은 채널들을 TBD로 변경
                    for i in range(len(group), len(voice_channels)):
                        voice_channel = voice_channels[i]
                        new_name = "TBD"

                        if voice_channel.name != new_name:
                            try:
                                await voice_channel.edit(name=new_name)
                                changed_count += 1
                            except discord.HTTPException as e:
                                logger.error(f"[Discord] 음성채널 이름 변경 실패 - 조: {group_letter}조, 채널: {voice_channel.name}: {e}", exc_info=True)
                                error_count += 1
                            except discord.Forbidden:
                                logger.error(f"[Discord] 음성채널 이름 변경 권한 없음 - 조: {group_letter}조, 채널: {voice_channel.name}")
                                error_count += 1

                else:
                    for i, voice_channel in enumerate(voice_channels):
                        new_name = "TBD"

                        if voice_channel.name != new_name:
                            try:
                                await voice_channel.edit(name=new_name)
                                changed_count += 1
                            except discord.HTTPException as e:
                                logger.error(f"[Discord] 음성채널 이름 변경 실패 - 조: {group_letter}조, 채널: {voice_channel.name}: {e}", exc_info=True)
                                error_count += 1
                            except discord.Forbidden:
                                logger.error(f"[Discord] 음성채널 이름 변경 권한 없음 - 조: {group_letter}조, 채널: {voice_channel.name}")
                                error_count += 1

                if changed_count > 0 or error_count > 0:
                    logger.info(f"[Discord] 음성채널 이름 변경 완료 - 조: {group_letter}조, 변경: {changed_count}개, 오류: {error_count}개")

        except Exception as e:
            logger.error(f"[Discord] 음성채널 이름 변경 실패: {e}", exc_info=True)

    def create_group_announcement_message(self, group_letter: str, group: List[Tuple[str, "TeamData", float]]) -> str:
        """조별 공지 메시지를 생성합니다."""
        from utils.helpers import get_current_kst_time
        from services.notion_api import get_server_info

        current_time = get_current_kst_time()
        date_str = current_time.strftime('%m.%d')

        info = get_server_info()
        message = f"📢 {date_str} 20시 스크림 {group_letter}조 조편성 결과\n{info['operate']}"

        return message

    async def _get_group_role_mention(self, guild: discord.Guild, group_letter: str) -> str:
        """조별 역할 멘션을 가져옵니다."""
        return ""

    async def clear_channel_messages(self, channel: discord.TextChannel) -> None:
        """채널의 모든 메시지를 삭제합니다."""
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

    async def delete_single_message_with_retry(self, message: discord.Message, max_retries: int = 3) -> int:
        """단일 메시지를 재시도 로직과 함께 삭제합니다."""
        for attempt in range(max_retries):
            try:
                await message.delete()
                return 1
            except discord.NotFound:
                return 0
            except discord.Forbidden:
                logger.warning(f"[Discord] 메시지 삭제 권한 없음 - 메시지 ID: {message.id}")
                return 0
            except discord.HTTPException as e:
                if e.status == 429:
                    if attempt < max_retries - 1:
                        retry_after = getattr(e, 'retry_after', 1.0)
                        await asyncio.sleep(retry_after)
                        continue
                    else:
                        logger.warning(f"[Discord] 메시지 삭제 실패 - Rate limit, 최대 재시도 초과, 메시지 ID: {message.id}")
                        return 0
                else:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    return 0
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                return 0

        return 0

    async def delete_message_batch(self, messages: List[discord.Message]) -> int:
        """메시지 배치를 삭제합니다."""
        deleted_count = 0
        for message in messages:
            deleted_count += await self.delete_single_message_with_retry(message)
        return deleted_count

    async def delete_single_message(self, message: discord.Message) -> int:
        """단일 메시지를 삭제합니다."""
        return await self.delete_single_message_with_retry(message)
