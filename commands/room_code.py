"""
방 코드 명령어
"""
import asyncio
import re
from datetime import datetime, timedelta

import discord
from discord.ui import ActionRow, Container, LayoutView, Separator, TextDisplay

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from commands.ui.layout_helpers import error_view, send_response, FOOTER_TEXT
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
    # 6자리 숫자인지 확인 (0으로 시작 가능)
    return bool(re.match(r'^\d{6}$', cleaned_code))


def calculate_round_start_time(current_time: datetime) -> datetime:
    """라운드 시작 시간 계산"""
    # 20시 이전인 경우 다음 라운드 시작 시간을 20시로 고정
    if current_time.hour < 20:
        # 오늘 20시로 설정
        round_start = current_time.replace(hour=20, minute=0, second=0, microsecond=0)
    else:
        # 20시 이후인 경우 현재 시간 + 5분
        round_start = current_time + timedelta(minutes=5)

    return round_start


async def get_round_number(channel: discord.TextChannel) -> int:
    """채널의 방코드 메시지를 스캔하여 현재 라운드 번호를 계산"""
    try:
        round_count = 0
        # 최신 메시지 30개만 확인 (레이트리밋 및 지연 최소화)
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
        return round_count + 1  # 현재 메시지를 포함하여 +1
    except discord.Forbidden:
        logger.warning("[명령어] 채널 히스토리 읽기 권한 없음")
        return 1
    except Exception as e:
        logger.error(f"[명령어] 라운드 번호 계산 실패: {e}", exc_info=True)
        return 1  # 오류 시 1라운드로 기본값


async def get_group_role_mention(guild: discord.Guild, channel: discord.TextChannel) -> str:
    """채널에 해당하는 조별 역할 멘션을 가져옵니다."""
    try:
        group_letter = get_group_letter(channel.id)
        if not group_letter:
            logger.warning(f"[명령어] 채널에 해당하는 조를 찾을 수 없음 - 채널 ID: {channel.id}")
            return ""

        # 조별 역할 찾기
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
        weather_buttons: list | None = None,
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
        desc = ""
        if role_mention:
            desc += f"{role_mention}\n"
        desc += f"**#️⃣ 방 코드**\n# `{cleaned_room_code}`"

        children: list = [
            TextDisplay(content=f"## {title}\n{desc}"),
            TextDisplay(content=f"**🌤️ 날씨**\n{weather_value}"),
            TextDisplay(content=f"**⏱️ 라운드 시작**\n`{round_start_str}`"),
        ]

        if ban_display:
            children.append(TextDisplay(content=f"**🚫 밴 목록**\n{ban_display}"))

        children.append(Separator())
        children.append(TextDisplay(content=FOOTER_TEXT))

        self.add_item(Container(*children, accent_colour=discord.Color.blue()))

        if weather_buttons:
            self.add_item(ActionRow(*weather_buttons))


class WeatherButton(discord.ui.Button):
    """서브 날씨 선택 버튼"""

    def __init__(self, weather: str, group_letter: str, round_number: int):
        super().__init__(label=weather, style=discord.ButtonStyle.primary)
        self.weather = weather
        self.group_letter = group_letter
        self.round_number = round_number

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user):
            await send_response(interaction, error_view("관리자만 날씨를 선택할 수 있습니다."))
            return

        manager = BotManager.get_instance()
        manager.add_selected_weather(self.group_letter, self.weather)

        main_weather = MAIN_WEATHERS.get(self.round_number, "알 수 없음")
        new_weather_value = f"메인 날씨: `{main_weather}`\n서브 날씨: `{self.weather}`"

        # 기존 View 데이터로 새 View 생성 (버튼 없이 = 날씨 확정)
        parent = self.view  # RoomCodeView
        new_view = RoomCodeView(
            round_number=parent.round_number,
            cleaned_room_code=parent.cleaned_room_code,
            weather_value=new_weather_value,
            round_start_str=parent.round_start_str,
            ban_display=parent.ban_display,
            role_mention=parent.role_mention,
            group_letter=parent.group_letter,
        )

        await interaction.response.edit_message(view=new_view)
        logger.info(f"[날씨] {self.group_letter}조 {self.round_number}R 서브 날씨 선택: {self.weather}")


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

        # 빈칸 제거된 방코드
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
            weather_buttons = None
            weather_warning = None

            if group_letter:
                manager = BotManager.get_instance()
                selected = manager.get_selected_weathers(group_letter)
                available = [w for w in SUB_WEATHERS if w not in selected]

                # 이전 라운드 서브 날씨 미선택 체크
                expected_selected = round_number - 1  # N라운드면 N-1개 선택됐어야 함
                if len(selected) < expected_selected:
                    missed = expected_selected - len(selected)
                    weather_warning = (
                        f"⚠️ 이전 라운드의 서브 날씨가 {missed}개 미선택 상태입니다. "
                        f"아래 버튼으로 이번 라운드 서브 날씨를 선택해주세요."
                    )

                if len(available) == 1:
                    # 4라운드: 자동 확정
                    sub_weather = available[0]
                    manager.add_selected_weather(group_letter, sub_weather)
                    weather_value = f"메인 날씨: `{main_weather}`\n서브 날씨: `{sub_weather}`"
                elif len(available) == 0:
                    # 모든 서브 날씨가 소진된 경우
                    weather_value = f"메인 날씨: `{main_weather}`"
                else:
                    # 선택 필요: 서브 날씨 후보를 그대로 노출
                    weather_value = f"메인 날씨: `{main_weather}`\n서브 날씨: `{', '.join(available)}`"
                    weather_buttons = [
                        WeatherButton(w, group_letter, round_number)
                        for w in available
                    ]
            else:
                weather_value = f"메인 날씨: `{main_weather}`\n서브 날씨: `{', '.join(SUB_WEATHERS)}`"

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
                weather_buttons=weather_buttons,
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
                    # 이전 라운드 미선택 경고 메시지 전송
                    if weather_warning:
                        try:
                            if interaction.response.is_done():
                                await interaction.followup.send(weather_warning, ephemeral=True)
                        except Exception:
                            pass
                    break

                except discord.NotFound:
                    logger.warning("[명령어] Interaction 만료되어 응답 전송 불가")
                    try:
                        await interaction.channel.send(**send_kwargs)
                        logger.debug(f"[명령어] Round {round_number} 방코드 공지 완료 (채널 직접 전송): {cleaned_room_code}")
                    except Exception as e:
                        logger.error(f"[명령어] 채널에 메시지 전송 실패: {e}", exc_info=True)
                    break

                except (aiohttp.client_exceptions.ClientOSError, ConnectionResetError) as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"[명령어] 네트워크 연결 오류 발생 - 시도 {attempt + 1}/{max_retries}, 재시도 중: {e}")
                        await asyncio.sleep(retry_delay * (attempt + 1))
                        continue
                    else:
                        logger.error(f"[명령어] 네트워크 연결 최종 실패: {e}", exc_info=True)
                        try:
                            await interaction.channel.send(**send_kwargs)
                            logger.debug(f"[명령어] Round {round_number} 방코드 공지 완료 (재시도 후): {cleaned_room_code}")
                        except Exception as send_error:
                            logger.error(f"[명령어] 채널에 메시지 전송 최종 실패: {send_error}", exc_info=True)
                        break

                except Exception as e:
                    logger.error(f"[명령어] Interaction 응답 전송 실패: {e}", exc_info=True)
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay * (attempt + 1))
                        continue
                    else:
                        try:
                            await interaction.channel.send(**send_kwargs)
                            logger.debug(f"[명령어] Round {round_number} 방코드 공지 완료 (예외 후): {cleaned_room_code}")
                        except Exception as send_error:
                            logger.error(f"[명령어] 채널에 메시지 전송 최종 실패: {send_error}", exc_info=True)
                        break

        except Exception as e:
            logger.error(f"[명령어] 방코드 처리 중 예상치 못한 오류: {e}", exc_info=True)
