"""
방 코드 명령어
"""
import asyncio
import re
from datetime import datetime, timedelta

import discord
from discord import ButtonStyle
from discord.ui import ActionRow, Button, Container, LayoutView, Separator, TextDisplay

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from commands.ui.layout_helpers import error_view, warning_view, send_response, FOOTER_TEXT
from utils.error_handlers import ErrorContext, handle_errors
from utils.helpers import get_current_kst_time, is_admin

logger = get_logger('room_code')

# 날씨 상수
MAIN_WEATHERS = {1: "흐림", 2: "쾌청", 3: "비", 4: "모래바람"}
SUB_WEATHERS = ["무풍", "강풍", "벼락", "자색 안개"]


def get_group_letter(channel_id: int) -> str | None:
    """채널 ID로 조 letter를 반환합니다."""
    for letter, ch_id in settings.GROUP_CHANNEL_IDS.items():
        if ch_id == channel_id:
            return letter
    return None


def clean_room_code(room_code: str) -> str:
    """방코드에서 빈칸을 제거하고 정리"""
    return room_code.replace(" ", "").replace("\t", "").replace("\n", "")


def validate_room_code(room_code: str) -> bool:
    """방코드가 6자리 숫자인지 검증 (빈칸 무시, 0으로 시작 가능)"""
    cleaned_code = clean_room_code(room_code)
    return bool(re.match(r'^\d{6}$', cleaned_code))


def calculate_round_start_time(current_time: datetime) -> datetime:
    """라운드 시작 시간 계산"""
    if current_time.hour < 20:
        round_start = current_time.replace(hour=20, minute=0, second=0, microsecond=0)
    else:
        round_start = current_time + timedelta(minutes=5)
    return round_start


async def get_round_number(channel: discord.TextChannel) -> int:
    """채널의 방코드 메시지를 스캔하여 현재 라운드 번호를 계산"""
    try:
        round_count = 0
        async for message in channel.history(limit=30):
            found = False
            # LayoutView 메시지 확인 (Components V2)
            try:
                for component in message.components:
                    if found:
                        break
                    for child in getattr(component, 'children', []):
                        content = getattr(child, 'content', '') or ''
                        if "스크림 공지" in content:
                            found = True
                            break
            except Exception:
                pass
            # 레거시 Embed 메시지 확인
            if not found and message.embeds:
                title = message.embeds[0].title or ""
                if "스크림 공지" in title:
                    found = True
            if found:
                round_count += 1
        return round_count + 1
    except discord.Forbidden:
        logger.warning("[명령어] 채널 히스토리 읽기 권한 없음")
        return 1
    except Exception as e:
        logger.error(f"[명령어] 라운드 번호 계산 실패: {e}", exc_info=True)
        return 1


async def get_group_role_mention(guild: discord.Guild, channel: discord.TextChannel) -> str:
    """채널에 해당하는 조별 역할 멘션을 가져옵니다."""
    try:
        group_letter = get_group_letter(channel.id)
        if not group_letter:
            logger.warning(f"[명령어] 채널에 해당하는 조를 찾을 수 없음 - 채널 ID: {channel.id}")
            return ""

        role_name = f"{group_letter}조"
        role = discord.utils.get(guild.roles, name=role_name)
        if role:
            return f"<@&{role.id}>"
        else:
            logger.warning(f"[명령어] 조별 역할을 찾을 수 없음 - 역할: {role_name}")
            return ""
    except Exception as e:
        logger.error(f"[명령어] 조별 역할 멘션 가져오기 실패: {e}", exc_info=True)
        return ""


class RoomCodeView(LayoutView):
    """방코드 공지 LayoutView"""

    def __init__(
        self,
        round_number: int,
        cleaned_room_code: str,
        weather_value: str,
        round_start_str: str,
        ban_display: str | None = None,
        role_mention: str = "",
        group_letter: str | None = None,
        weather_options: list[str] | None = None,
    ):
        super().__init__(timeout=None)
        self.round_number = round_number
        self.cleaned_room_code = cleaned_room_code
        self.weather_value = weather_value
        self.round_start_str = round_start_str
        self.ban_display = ban_display
        self.role_mention = role_mention
        self.group_letter = group_letter

        # 콘텐츠 구성
        title = f"📢 스크림 공지 - {round_number}라운드"
        header = f"## {title}"
        if role_mention:
            header += f"\n{role_mention}"

        children: list = [
            TextDisplay(content=header),
            TextDisplay(content=f"# `{cleaned_room_code}`"),
            TextDisplay(content=f"🌤️ {weather_value}  ·  ⏱️ `{round_start_str}`"),
        ]

        if ban_display:
            children.append(TextDisplay(content=f"🚫 밴: {ban_display}"))

        children.append(Separator())
        children.append(TextDisplay(content=FOOTER_TEXT))

        self.add_item(Container(*children, accent_colour=discord.Color.blue()))

        # 서브 날씨 선택 버튼 (View 내부에서 생성)
        if weather_options:
            buttons = []
            for weather_name in weather_options:
                btn = Button(label=weather_name, style=ButtonStyle.secondary)
                btn.callback = self._make_weather_callback(weather_name)
                buttons.append(btn)
            self.add_item(ActionRow(*buttons))

    def _make_weather_callback(self, weather_name: str):
        """날씨 버튼 콜백을 생성합니다."""
        async def callback(interaction: discord.Interaction) -> None:
            if not is_admin(interaction.user):
                await send_response(interaction, error_view("관리자만 날씨를 선택할 수 있습니다."))
                return

            manager = BotManager.get_instance()
            manager.add_selected_weather(self.group_letter, weather_name)

            main_weather = MAIN_WEATHERS.get(self.round_number, "알 수 없음")
            new_weather = f"`{main_weather}` · `{weather_name}`"

            # 버튼 없는 확정 View로 교체
            new_view = RoomCodeView(
                round_number=self.round_number,
                cleaned_room_code=self.cleaned_room_code,
                weather_value=new_weather,
                round_start_str=self.round_start_str,
                ban_display=self.ban_display,
                role_mention=self.role_mention,
                group_letter=self.group_letter,
            )

            await interaction.response.edit_message(view=new_view)
            logger.info(f"[날씨] {self.group_letter}조 {self.round_number}R 서브 날씨 선택: {weather_name}")

        return callback


@handle_errors(default_return=None, log_level='error', reraise=False)
async def 방코드(interaction: discord.Interaction, room_code: str) -> None:
    """방 코드를 공지합니다"""
    with ErrorContext(default_return=None, log_errors=True):
        # 채널 타입 검증
        if not isinstance(interaction.channel, discord.TextChannel):
            try:
                await send_response(interaction, error_view("이 명령어는 텍스트 채널에서만 사용할 수 있습니다."))
            except discord.NotFound:
                logger.warning("[명령어] Interaction 만료됨")
            return

        # 방코드 검증
        if not validate_room_code(room_code):
            try:
                await send_response(interaction, error_view("방코드는 6자리 숫자여야 합니다.\n\n💡 예시: `123456`"))
            except discord.NotFound:
                logger.warning("[명령어] Interaction 만료됨")
            return

        cleaned_room_code = clean_room_code(room_code)

        now = get_current_kst_time()
        round_start_time = calculate_round_start_time(now)

        # 라운드 번호 계산
        round_number = await get_round_number(interaction.channel)

        # LayoutView 생성 및 메시지 전송
        try:
            # 날씨 정보 준비
            group_letter = get_group_letter(interaction.channel.id)
            main_weather = MAIN_WEATHERS.get(round_number, "알 수 없음")
            weather_options = None
            weather_warning = None

            if group_letter:
                manager = BotManager.get_instance()
                selected = manager.get_selected_weathers(group_letter)
                available = [w for w in SUB_WEATHERS if w not in selected]

                # 이전 라운드 서브 날씨 미선택 체크
                expected_selected = round_number - 1
                if len(selected) < expected_selected:
                    missed = expected_selected - len(selected)
                    weather_warning = f"이전 라운드의 서브 날씨가 {missed}개 미선택 상태입니다."

                if len(available) == 1:
                    # 4라운드: 자동 확정
                    sub_weather = available[0]
                    manager.add_selected_weather(group_letter, sub_weather)
                    weather_value = f"`{main_weather}` · `{sub_weather}`"
                elif len(available) == 0:
                    weather_value = f"`{main_weather}`"
                else:
                    # 선택 대기: 메인 날씨만 확정 표시, 버튼으로 서브 선택 유도
                    weather_value = f"`{main_weather}` · 서브 날씨 선택 대기"
                    weather_options = available
            else:
                weather_value = f"`{main_weather}`"

            # 밴 리스트 표시
            ban_display = None
            if group_letter:
                ban_list = BotManager.get_instance().get_ban_list(group_letter)
                if ban_list:
                    ban_display = " ".join(f"`{char}`" for char in ban_list)

            # 조별 역할 멘션
            role_mention = await get_group_role_mention(interaction.guild, interaction.channel)

            # RoomCodeView 생성
            room_code_view = RoomCodeView(
                round_number=round_number,
                cleaned_room_code=cleaned_room_code,
                weather_value=weather_value,
                round_start_str=round_start_time.strftime('%H:%M'),
                ban_display=ban_display,
                role_mention=role_mention,
                group_letter=group_letter,
                weather_options=weather_options,
            )

            # 메시지 전송 공통 kwargs
            send_kwargs = {"view": room_code_view}
            if role_mention:
                send_kwargs["allowed_mentions"] = discord.AllowedMentions(roles=True)

            # Interaction 응답 전송 (만료 처리 및 네트워크 오류 재시도)
            import aiohttp
            max_retries = 3
            retry_delay = 1.0

            for attempt in range(max_retries):
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(**send_kwargs)
                    else:
                        await interaction.response.send_message(**send_kwargs)

                    logger.debug(f"[명령어] Round {round_number} 방코드 공지 완료: {cleaned_room_code}")
                    # 이전 라운드 미선택 경고
                    if weather_warning:
                        try:
                            if interaction.response.is_done():
                                await interaction.followup.send(
                                    view=warning_view(weather_warning), ephemeral=True
                                )
                        except Exception:
                            pass
                    break

                except discord.NotFound:
                    logger.warning("[명령어] Interaction 만료되어 응답 전송 불가")
                    try:
                        await interaction.channel.send(**send_kwargs)
                    except Exception as e:
                        logger.error(f"[명령어] 채널에 메시지 전송 실패: {e}", exc_info=True)
                    break

                except (aiohttp.client_exceptions.ClientOSError, ConnectionResetError) as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"[명령어] 네트워크 오류 - 시도 {attempt + 1}/{max_retries}: {e}")
                        await asyncio.sleep(retry_delay * (attempt + 1))
                        continue
                    else:
                        logger.error(f"[명령어] 네트워크 연결 최종 실패: {e}", exc_info=True)
                        try:
                            await interaction.channel.send(**send_kwargs)
                        except Exception as send_error:
                            logger.error(f"[명령어] 메시지 전송 최종 실패: {send_error}", exc_info=True)
                        break

                except Exception as e:
                    logger.error(f"[명령어] 응답 전송 실패: {e}", exc_info=True)
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay * (attempt + 1))
                        continue
                    else:
                        try:
                            await interaction.channel.send(**send_kwargs)
                        except Exception as send_error:
                            logger.error(f"[명령어] 메시지 전송 최종 실패: {send_error}", exc_info=True)
                        break

        except Exception as e:
            logger.error(f"[명령어] 방코드 처리 중 예상치 못한 오류: {e}", exc_info=True)
