"""
방 코드 명령어
"""
import asyncio
import re
from datetime import datetime, timedelta

import discord
from discord import Color, Embed

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
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
            if not message.embeds:
                continue
            embed = message.embeds[0]
            title = embed.title or ""
            if "스크림 공지" in title:
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


class WeatherButton(discord.ui.Button):
    """서브 날씨 선택 버튼"""

    def __init__(self, weather: str, group_letter: str, round_number: int):
        super().__init__(label=weather, style=discord.ButtonStyle.primary)
        self.weather = weather
        self.group_letter = group_letter
        self.round_number = round_number

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ 관리자만 날씨를 선택할 수 있습니다.", ephemeral=True
            )
            return

        manager = BotManager.get_instance()
        manager.add_selected_weather(self.group_letter, self.weather)

        # embed 업데이트: "미정" → 선택값
        embed = interaction.message.embeds[0]
        main_weather = MAIN_WEATHERS.get(self.round_number, "알 수 없음")
        for i, field in enumerate(embed.fields):
            if field.name == "🌤️ 날씨":
                embed.set_field_at(
                    i,
                    name="🌤️ 날씨",
                    value=f"메인 날씨: `{main_weather}`\n서브 날씨: `{self.weather}`",
                    inline=False,
                )
                break

        # 모든 버튼 비활성화
        for item in self.view.children:
            item.disabled = True

        await interaction.response.edit_message(embed=embed, view=self.view)
        logger.info(f"[날씨] {self.group_letter}조 {self.round_number}R 서브 날씨 선택: {self.weather}")


class WeatherButtonView(discord.ui.View):
    """서브 날씨 선택 버튼 View"""

    def __init__(self, group_letter: str, round_number: int):
        super().__init__(timeout=None)
        self.group_letter = group_letter
        self.round_number = round_number

        selected = BotManager.get_instance().get_selected_weathers(group_letter)
        available = [w for w in SUB_WEATHERS if w not in selected]

        for weather in available:
            self.add_item(WeatherButton(weather, group_letter, round_number))


@handle_errors(default_return=None, log_level='error', reraise=False)
async def 방코드(interaction: discord.Interaction, room_code: str) -> None:
    """방 코드를 공지합니다"""
    with ErrorContext(default_return=None, log_errors=True):
        # 채널 타입 검증
        if not isinstance(interaction.channel, discord.TextChannel):
            error_embed = Embed(
                title="❌ 오류",
                description="이 명령어는 텍스트 채널에서만 사용할 수 있습니다.",
                color=Color.red()
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=error_embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=error_embed, ephemeral=True)
            except discord.NotFound:
                logger.warning("[명령어] Interaction 만료됨")
            return
        
        # 방코드 검증
        if not validate_room_code(room_code):
            error_embed = Embed(
                title="❌ 오류",
                description="방코드는 6자리 숫자여야 합니다.\n\n💡 예시: `123456`",
                color=Color.red()
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=error_embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=error_embed, ephemeral=True)
            except discord.NotFound:
                logger.warning("[명령어] Interaction 만료됨")
            return
        
        # 빈칸 제거된 방코드
        cleaned_room_code = clean_room_code(room_code)

        now = get_current_kst_time()
        round_start_time = calculate_round_start_time(now)

        # 라운드 번호 계산
        round_number = await get_round_number(interaction.channel)

        # 임베드 생성 및 메시지 전송
        try:
            # 공지 임베드 생성
            embed = Embed(
                title=f"📢 스크림 공지 - {round_number}라운드",
                description=f"**#️⃣ 방 코드**\n# `{cleaned_room_code}`",
                color=Color.blue()
            )

            # 날씨 field 추가
            group_letter = get_group_letter(interaction.channel.id)
            main_weather = MAIN_WEATHERS.get(round_number, "알 수 없음")
            weather_view = None
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
                    weather_view = WeatherButtonView(group_letter, round_number)
            else:
                weather_value = f"메인 날씨: `{main_weather}`\n서브 날씨: `{', '.join(SUB_WEATHERS)}`"

            embed.add_field(
                name="🌤️ 날씨",
                value=weather_value,
                inline=False
            )

            embed.add_field(
                name="⏱️ 라운드 시작",
                value=f"`{round_start_time.strftime('%H:%M')}`",
                inline=False
            )

            # 밴 리스트 표시
            if group_letter:
                ban_list = BotManager.get_instance().get_ban_list(group_letter)
                if ban_list:
                    ban_display = " ".join(f"`{char}`" for char in ban_list)
                    embed.add_field(
                        name="🚫 밴 목록",
                        value=ban_display,
                        inline=False
                    )

            embed.set_footer(text=settings.EMBED_FOOTER_TEXT, icon_url=settings.THUMBNAIL_URL)

            # 조별 역할 멘션 메시지 생성
            role_mention = await get_group_role_mention(interaction.guild, interaction.channel)

            # 메시지 전송 공통 kwargs
            send_kwargs = {"embed": embed}
            if weather_view:
                send_kwargs["view"] = weather_view

            # Interaction 응답 전송 (만료 처리 및 네트워크 오류 재시도)
            import aiohttp
            max_retries = 3
            retry_delay = 1.0

            for attempt in range(max_retries):
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(
                            role_mention if role_mention else None,
                            **send_kwargs,
                            allowed_mentions=discord.AllowedMentions(roles=True) if role_mention else None
                        )
                    else:
                        if role_mention:
                            await interaction.response.send_message(
                                role_mention,
                                **send_kwargs,
                                allowed_mentions=discord.AllowedMentions(roles=True)
                            )
                        else:
                            await interaction.response.send_message(**send_kwargs)

                    logger.debug(f"[명령어] Round {round_number} 방코드 공지 완료: {cleaned_room_code}")
                    # 이전 라운드 미선택 경고 메시지 전송
                    if weather_warning:
                        try:
                            if interaction.response.is_done():
                                await interaction.followup.send(weather_warning, ephemeral=True)
                            # response가 방금 사용됐으면 followup으로 전송
                        except Exception:
                            pass
                    break

                except discord.NotFound:
                    logger.warning("[명령어] Interaction 만료되어 응답 전송 불가")
                    try:
                        if role_mention:
                            await interaction.channel.send(
                                role_mention,
                                **send_kwargs,
                                allowed_mentions=discord.AllowedMentions(roles=True)
                            )
                        else:
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
                            if role_mention:
                                await interaction.channel.send(
                                    role_mention,
                                    **send_kwargs,
                                    allowed_mentions=discord.AllowedMentions(roles=True)
                                )
                            else:
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
                            if role_mention:
                                await interaction.channel.send(
                                    role_mention,
                                    **send_kwargs,
                                    allowed_mentions=discord.AllowedMentions(roles=True)
                                )
                            else:
                                await interaction.channel.send(**send_kwargs)
                            logger.debug(f"[명령어] Round {round_number} 방코드 공지 완료 (예외 후): {cleaned_room_code}")
                        except Exception as send_error:
                            logger.error(f"[명령어] 채널에 메시지 전송 최종 실패: {send_error}", exc_info=True)
                        break

        except Exception as e:
            logger.error(f"[명령어] 방코드 처리 중 예상치 못한 오류: {e}", exc_info=True)
