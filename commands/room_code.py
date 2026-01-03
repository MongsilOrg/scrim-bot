"""
방 코드 명령어
"""
import asyncio
import re
from datetime import datetime, timedelta

import discord
from discord import Color, Embed

from config.logging_config import get_logger
from config.settings import settings
from utils.error_handlers import ErrorContext, handle_errors
from utils.helpers import get_current_kst_time

logger = get_logger('room_code')

# 전역 락 및 처리 중인 작업 추적
room_code_lock = asyncio.Lock()
processing_room_codes = {}


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
            if "Scrim Announcement" in title or "스크림 방 코드 공지" in title:
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
        # 채널 ID로 조 찾기
        group_letter = None
        for letter, channel_id in settings.GROUP_CHANNEL_IDS.items():
            if channel_id == channel.id:
                group_letter = letter
                break
        
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


@handle_errors(default_return=None, log_level='error', reraise=False)
async def 방코드(interaction: discord.Interaction, room_code: str) -> None:
    """방 코드를 공지합니다"""
    with ErrorContext(default_return=None, log_errors=True):
        # 채널 타입 검증
        if not isinstance(interaction.channel, discord.TextChannel):
            error_embed = Embed(
                title="❌ 오류 / Error",
                description="이 명령어는 텍스트 채널에서만 사용할 수 있습니다. / This command can only be used in text channels.",
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
                title="❌ 잘못된 방코드 / Invalid Room Code",
                description="방코드는 6자리 숫자여야 합니다. / Room code must be 6 digits.",
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
        round_number = await get_round_number(interaction.channel)
        
        # 전역 락을 사용하여 동시 실행 방지
        room_code_key = f"roomcode_{interaction.channel.id}_{round_number}"
        async with room_code_lock:
            if room_code_key in processing_room_codes:
                error_embed = Embed(
                    title="❌ 오류 / Error",
                    description="이미 처리 중인 방코드 요청이 있습니다. 잠시 후 다시 시도해주세요. / A room code request is already being processed. Please try again later.",
                    color=Color.red()
                )
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(embed=error_embed, ephemeral=True)
                    else:
                        await interaction.response.send_message(embed=error_embed, ephemeral=True)
                except:
                    pass
                return
            processing_room_codes[room_code_key] = True
        
        try:
            # 중복 방지: 채널의 최근 메시지에서 이미 같은 라운드의 방코드 embed가 있는지 확인
            try:
                async for message in interaction.channel.history(limit=50):
                    if message.author.bot and message.embeds:
                        for embed in message.embeds:
                            if embed.title and f"Round {round_number}" in embed.title and "Scrim Announcement" in embed.title:
                                error_embed = Embed(
                                    title="❌ 오류 / Error",
                                    description=f"Round {round_number}의 방코드가 이미 공지되었습니다. / Room code for Round {round_number} has already been announced.",
                                    color=Color.red()
                                )
                                try:
                                    if interaction.response.is_done():
                                        await interaction.followup.send(embed=error_embed, ephemeral=True)
                                    else:
                                        await interaction.response.send_message(embed=error_embed, ephemeral=True)
                                except:
                                    pass
                                return
            except Exception as e:
                logger.warning(f"[명령어] 채널 히스토리 확인 중 오류 발생 - 계속 진행: {e}")
            
            # 공지 임베드 생성
            embed = Embed(
                title=f"📢 Scrim Announcement - Round {round_number}",
                description=f"**#️⃣ 방 코드 / Room Code**\n# `{cleaned_room_code}`",
                color=Color.blue()
            )
            
            embed.add_field(
                name="⏱️ 라운드 시작 / Round Start",
                value=f"**`{round_start_time.strftime('%H:%M')}`**",
                    inline=False
                )
            
            embed.set_footer(text="ER Scrim", icon_url=settings.THUMBNAIL_URL)
            
            # 조별 역할 멘션 메시지 생성
            role_mention = await get_group_role_mention(interaction.guild, interaction.channel)
            
            # Interaction 응답 전송 (만료 처리 및 네트워크 오류 재시도)
            import aiohttp
            max_retries = 3
            retry_delay = 1.0
            
            for attempt in range(max_retries):
                try:
                    if interaction.response.is_done():
                        # 이미 응답이 전송된 경우 followup 사용
                        await interaction.followup.send(
                            role_mention if role_mention else None,
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions(roles=True) if role_mention else None
                        )
                    else:
                        # 아직 응답이 전송되지 않은 경우
                        if role_mention:
                            await interaction.response.send_message(
                                role_mention,
                                embed=embed,
                                allowed_mentions=discord.AllowedMentions(roles=True)
                            )
                        else:
                            await interaction.response.send_message(embed=embed)
                    
                    # 성공적으로 전송됨
                    break
                    
                except discord.NotFound:
                    logger.warning("[명령어] Interaction 만료되어 응답 전송 불가")
                    # 채널에 직접 메시지 전송 시도
                    try:
                        if role_mention:
                            await interaction.channel.send(
                                role_mention,
                                embed=embed,
                                allowed_mentions=discord.AllowedMentions(roles=True)
                            )
                        else:
                            await interaction.channel.send(embed=embed)
                    except Exception as e:
                        logger.error(f"[명령어] 채널에 메시지 전송 실패: {e}", exc_info=True)
                    break  # NotFound는 재시도하지 않음
                    
                except (aiohttp.client_exceptions.ClientOSError, ConnectionResetError) as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"[명령어] 네트워크 연결 오류 발생 - 시도 {attempt + 1}/{max_retries}, 재시도 중: {e}")
                        await asyncio.sleep(retry_delay * (attempt + 1))  # 지수 백오프
                        continue
                    else:
                        logger.error(f"[명령어] 네트워크 연결 최종 실패: {e}", exc_info=True)
                        # 마지막 시도: 채널에 직접 메시지 전송
                        try:
                            if role_mention:
                                await interaction.channel.send(
                                    role_mention,
                                    embed=embed,
                                    allowed_mentions=discord.AllowedMentions(roles=True)
                                )
                            else:
                                await interaction.channel.send(embed=embed)
                        except Exception as send_error:
                            logger.error(f"채널에 메시지 전송 실패: {send_error}", exc_info=True)
                        break
                        
                except Exception as e:
                    logger.error(f"[명령어] Interaction 응답 전송 실패: {e}", exc_info=True)
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay * (attempt + 1))
                        continue
                    else:
                        # 마지막 시도: 채널에 직접 메시지 전송
                        try:
                            if role_mention:
                                await interaction.channel.send(
                                    role_mention,
                                    embed=embed,
                                    allowed_mentions=discord.AllowedMentions(roles=True)
                                )
                            else:
                                await interaction.channel.send(embed=embed)
                        except Exception as send_error:
                            logger.error(f"채널에 메시지 전송 실패: {send_error}", exc_info=True)
                        break
            
        finally:
            async with room_code_lock:
                processing_room_codes.pop(room_code_key, None)
