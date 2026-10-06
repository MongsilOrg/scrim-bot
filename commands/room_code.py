import asyncio
import re
from datetime import datetime, timedelta

import aiohttp
import discord
from discord import ButtonStyle
from discord.ui import ActionRow, Button, Container, DynamicItem, LayoutView, Separator, TextDisplay

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from services.holidays_api import get_rest_day_info
from services.score_aggregation import compute_ban_list_for_channel
from utils.layout_helpers import error_view, permission_error_view, warning_view, send_response, FOOTER_TEXT
from utils.helpers import (
    get_current_kst_time,
    get_group_letter,
    get_group_role_mention,
    get_start_of_day_utc,
    is_admin,
)

logger = get_logger('room_code')

MAIN_WEATHERS = {1: "모래바람", 2: "비", 3: "쾌청", 4: "흐림"}
SUB_WEATHERS = ["무풍", "강풍", "벼락", "자색 안개"]

assert len(MAIN_WEATHERS) == settings.TOTAL_ROUNDS, (
    "MAIN_WEATHERS 는 라운드당 하나씩 정의되어야 합니다"
)

ROUND_OVERFLOW_TEXT = (
    f"오늘 {settings.TOTAL_ROUNDS}라운드 공지를 모두 올렸습니다. "
    "번호가 틀렸다면 운영진에게 알려주세요."
)


def clean_room_code(room_code: str) -> str:
    return room_code.replace(" ", "").replace("\t", "").replace("\n", "")


def validate_room_code(room_code: str) -> bool:
    cleaned_code = clean_room_code(room_code)
    return bool(re.match(r'^\d{6}$', cleaned_code))


def calculate_round_start_time(current_time: datetime) -> datetime:
    if current_time.hour < settings.SCRIM_START_HOUR:
        round_start = current_time.replace(hour=settings.SCRIM_START_HOUR, minute=0, second=0, microsecond=0)
    else:
        round_start = current_time + timedelta(minutes=5)
    return round_start


def _is_scrim_notice_message(message: discord.Message) -> bool:
    try:
        for component in message.components:
            for child in getattr(component, 'children', []):
                content = getattr(child, 'content', '') or ''
                if "스크림 공지" in content:
                    return True
    except Exception:
        logger.debug("[명령어] 공지 메시지 컴포넌트 판별 실패", exc_info=True)
    if message.embeds:
        title = message.embeds[0].title or ""
        if "스크림 공지" in title:
            return True
    return False


async def get_round_number(channel: discord.TextChannel) -> int:
    try:
        start_utc = get_start_of_day_utc()
        round_count = 0
        async for message in channel.history(after=start_utc, limit=None):
            if _is_scrim_notice_message(message):
                round_count += 1
        return round_count + 1
    except discord.Forbidden:
        logger.warning("[명령어] 채널 히스토리 읽기 권한 없음")
        return 1
    except Exception as e:
        logger.error(f"[명령어] 라운드 번호 계산 실패: {e}", exc_info=True)
        return 1


class RoomCodeView(LayoutView):
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

        title = f"📢 스크림 공지 - {round_number}라운드"
        header = f"## {title}"
        if role_mention:
            header += f"\n{role_mention}"

        children: list = [
            TextDisplay(content=header),
            TextDisplay(content=f"# `{cleaned_room_code}`"),
            TextDisplay(content=f"날씨: {weather_value}\n시작: `{round_start_str}`"),
        ]

        if ban_display:
            children.append(TextDisplay(content=f"{BAN_PREFIX}{ban_display}"))

        children.append(Separator())
        children.append(TextDisplay(content=FOOTER_TEXT))

        self.add_item(Container(*children, accent_colour=discord.Color.blue()))

        if weather_options and group_letter:
            buttons = [
                WeatherButton(group_letter, round_number, cleaned_room_code, round_start_str, SUB_WEATHERS.index(name))
                for name in weather_options
            ]
            self.add_item(ActionRow(*buttons))


BAN_PREFIX = "밴: "
# 배포 전에 올라온 공지의 밴 줄
LEGACY_BAN_PREFIX = "🚫 밴: "


def _find_ban_display(message: discord.Message | None) -> str | None:
    for component in getattr(message, 'components', None) or []:
        for child in getattr(component, 'children', None) or []:
            content = getattr(child, 'content', '') or ''
            for prefix in (LEGACY_BAN_PREFIX, BAN_PREFIX):
                if content.startswith(prefix):
                    return content[len(prefix):]
    return None


class WeatherButton(
    DynamicItem[Button],
    template=r"scrim:weather:(?P<group>[A-Z]):(?P<round>\d+):(?P<code>\d{6}):(?P<start>\d{4}):(?P<index>\d)",
):
    def __init__(self, group_letter: str, round_number: int, room_code: str, round_start_str: str, weather_index: int):
        self.group_letter = group_letter
        self.round_number = round_number
        self.room_code = room_code
        self.round_start_str = round_start_str
        self.weather_index = weather_index
        start = round_start_str.replace(":", "")
        super().__init__(Button(
            label=SUB_WEATHERS[weather_index],
            style=ButtonStyle.secondary,
            custom_id=f"scrim:weather:{group_letter}:{round_number}:{room_code}:{start}:{weather_index}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match) -> "WeatherButton":
        start = match["start"]
        return cls(
            match["group"], int(match["round"]), match["code"],
            f"{start[:2]}:{start[2:]}", int(match["index"]),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        denied = await _sub_weather_denial(interaction.user, self.group_letter)
        if denied:
            await send_response(interaction, permission_error_view(denied))
            return
        if self.weather_index >= len(SUB_WEATHERS):
            await send_response(interaction, error_view("알 수 없는 날씨입니다."))
            return

        weather_name = SUB_WEATHERS[self.weather_index]
        team_data_manager = BotManager.get_instance().get_team_data_manager()
        team_data_manager.add_selected_weather(self.group_letter, weather_name)

        main_weather = MAIN_WEATHERS.get(self.round_number, "알 수 없음")
        role_mention = get_group_role_mention(interaction.guild, self.group_letter) if interaction.guild else ""

        new_view = RoomCodeView(
            round_number=self.round_number,
            cleaned_room_code=self.room_code,
            weather_value=f"`{main_weather}`, `{weather_name}`",
            round_start_str=self.round_start_str,
            ban_display=_find_ban_display(interaction.message),
            role_mention=role_mention,
            group_letter=self.group_letter,
        )

        await interaction.response.edit_message(view=new_view)
        logger.info(f"[날씨] {self.group_letter}조 {self.round_number}R 서브 날씨 선택: {weather_name}, 선택: {interaction.user}")


def _can_post_room_code(member: discord.Member, group_letter: str) -> bool:
    if is_admin(member):
        return True
    role_name = f"{group_letter}조"
    return any(role.name == role_name for role in getattr(member, 'roles', []))


REST_DAY_CHECK_TIMEOUT = 2.0


async def _is_rest_day_today() -> bool:
    # 버튼 응답 3초 제한. shield로 조회는 끝까지 돌려 캐시를 채움
    try:
        info = await asyncio.wait_for(asyncio.shield(get_rest_day_info()), REST_DAY_CHECK_TIMEOUT)
    except Exception:
        logger.warning("[날씨] 휴무일 판정 실패 - 평일로 처리", exc_info=True)
        return False
    return bool(info.get("is_rest_day"))


async def _sub_weather_denial(member: discord.Member, group_letter: str) -> str | None:
    """고를 수 있으면 None, 막히면 거부 문구."""
    if is_admin(member):
        return None
    if not await _is_rest_day_today():
        return "서브 날씨는 관리자만 고를 수 있습니다."
    if _can_post_room_code(member, group_letter):
        return None
    return f"서브 날씨는 관리자와 {group_letter}조 참가자만 고를 수 있습니다."


NOTICE_MAX_ATTEMPTS = 3
NOTICE_RETRYABLE_ERRORS = (aiohttp.ClientError, ConnectionResetError, asyncio.TimeoutError, discord.DiscordServerError)


async def _notice_already_posted(channel, room_code: str, bot_user, since: datetime) -> bool:
    code_text = f"`{room_code}`"
    try:
        async for message in channel.history(after=since, limit=20):
            if bot_user is not None and getattr(message.author, 'id', None) != bot_user.id:
                continue
            if not _is_scrim_notice_message(message):
                continue
            for component in getattr(message, 'components', None) or []:
                for child in getattr(component, 'children', None) or []:
                    if code_text in (getattr(child, 'content', '') or ''):
                        return True
    except Exception as e:
        logger.warning(f"[명령어] 공지 중복 확인 실패: {e}")
    return False


async def _send_notice_to_channel(channel, send_kwargs: dict) -> bool:
    try:
        await channel.send(**send_kwargs)
        return True
    except Exception as e:
        logger.error(f"[명령어] 채널에 방코드 공지 전송 실패: {e}", exc_info=True)
        return False


async def _post_notice(interaction: discord.Interaction, send_kwargs: dict, room_code: str) -> bool:
    # 응답 유실로 보이는 오류도 서버에는 반영됐을 수 있어 재전송 전에 채널을 확인
    channel = interaction.channel
    bot_user = interaction.client.user
    for attempt in range(1, NOTICE_MAX_ATTEMPTS + 1):
        try:
            await interaction.followup.send(**send_kwargs)
            return True
        except discord.NotFound:
            logger.warning("[명령어] Interaction 만료 - 채널로 직접 전송")
            return await _send_notice_to_channel(channel, send_kwargs)
        except NOTICE_RETRYABLE_ERRORS as e:
            logger.warning(f"[명령어] 방코드 공지 전송 오류 - 시도 {attempt}/{NOTICE_MAX_ATTEMPTS}: {e}")
            if await _notice_already_posted(channel, room_code, bot_user, interaction.created_at):
                logger.info("[명령어] 오류 응답이었지만 공지는 이미 올라감 - 재전송 생략")
                return True
            if attempt < NOTICE_MAX_ATTEMPTS:
                await asyncio.sleep(attempt)
        except Exception as e:
            logger.error(f"[명령어] 방코드 공지 전송 실패: {e}", exc_info=True)
            return False
    logger.error("[명령어] 방코드 공지 재시도 소진 - 채널로 직접 전송")
    return await _send_notice_to_channel(channel, send_kwargs)


async def 방코드(interaction: discord.Interaction, room_code: str) -> None:
    try:
        if not isinstance(interaction.channel, discord.TextChannel):
            try:
                await send_response(interaction, error_view("이 명령어는 텍스트 채널에서만 사용할 수 있습니다."))
            except discord.NotFound:
                logger.warning("[명령어] Interaction 만료됨")
            return

        if not validate_room_code(room_code):
            try:
                await send_response(interaction, error_view("방 코드는 6자리 숫자로 입력해주세요. 예: `123456`"))
            except discord.NotFound:
                logger.warning("[명령어] Interaction 만료됨")
            return

        group_letter = get_group_letter(interaction.channel.id)
        if not group_letter:
            await send_response(interaction, error_view("방 코드는 조별 채널에서만 올릴 수 있습니다."))
            return
        if not _can_post_room_code(interaction.user, group_letter):
            logger.info(f"[명령어] 방코드 권한 없음 - 사용자: {interaction.user}, 조: {group_letter}조")
            await send_response(interaction, permission_error_view(f"관리자와 {group_letter}조 참가자만 방 코드를 올릴 수 있습니다."))
            return

        # 채널 기록 스캔과 CSV 집계가 3초 응답 제한을 넘김
        try:
            await interaction.response.defer()
        except discord.NotFound:
            logger.warning("[명령어] Interaction 만료되어 defer 불가 - 채널로 직접 전송")

        cleaned_room_code = clean_room_code(room_code)

        now = get_current_kst_time()
        round_start_time = calculate_round_start_time(now)

        round_number = await get_round_number(interaction.channel)
        if round_number > settings.TOTAL_ROUNDS:
            logger.info(f"[명령어] 라운드 초과 방코드 거절 - 사용자: {interaction.user}, 조: {group_letter}조, 코드: {cleaned_room_code}")
            await send_response(interaction, warning_view(ROUND_OVERFLOW_TEXT))
            return

        main_weather = MAIN_WEATHERS.get(round_number, "알 수 없음")
        weather_options = None
        weather_warning = None

        team_data_manager = BotManager.get_instance().get_team_data_manager()
        selected = team_data_manager.get_selected_weathers(group_letter)
        available = [w for w in SUB_WEATHERS if w not in selected]

        expected_selected = round_number - 1
        if len(selected) < expected_selected:
            missed = expected_selected - len(selected)
            weather_warning = f"이전 라운드 공지 {missed}개에서 서브 날씨를 고르지 않았습니다. 이전 공지의 날씨 버튼으로 골라주세요."

        if len(available) == 1:
            sub_weather = available[0]
            team_data_manager.add_selected_weather(group_letter, sub_weather)
            weather_value = f"`{main_weather}`, `{sub_weather}`"
        elif len(available) == 0:
            weather_value = f"`{main_weather}`"
        else:
            weather_value = f"`{main_weather}`, 서브 날씨는 아래 버튼으로 고릅니다"
            weather_options = available

        ban_display = None
        ban_list = await compute_ban_list_for_channel(interaction.channel)
        if ban_list:
            ban_display = " ".join(f"`{char}`" for char in ban_list)

        role_mention = get_group_role_mention(interaction.guild, group_letter)
        if not role_mention:
            logger.warning(f"[명령어] 조별 역할을 찾을 수 없음 - 역할: {group_letter}조")

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

        send_kwargs = {"view": room_code_view}
        if role_mention:
            send_kwargs["allowed_mentions"] = discord.AllowedMentions(roles=True)

        posted = await _post_notice(interaction, send_kwargs, cleaned_room_code)
        if not posted:
            await send_response(interaction, error_view("방 코드 공지를 올리지 못했습니다. 잠시 뒤 다시 입력해주세요."))
            return

        logger.debug(f"[명령어] Round {round_number} 방코드 공지 완료: {cleaned_room_code}")
        if weather_warning:
            try:
                await interaction.followup.send(view=warning_view(weather_warning), ephemeral=True)
            except Exception as e:
                logger.warning(f"[명령어] 날씨 경고 전송 실패: {e}")

    except Exception as e:
        logger.error(f"[명령어] 방코드 처리 중 예상치 못한 오류: {e}", exc_info=True)
