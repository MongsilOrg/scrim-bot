import asyncio
from datetime import datetime, timedelta, timezone

import discord

from bot.client import ScrimBot
from config.logging_config import get_logger
from config.settings import settings
from utils.layout_helpers import warning_view

logger = get_logger('ticket')

# Ticket Tool 채널을 마지막 메시지 기준으로 정리. 닫기는 Ticket Tool 상태와 어긋나 흉내 내지 않고 안내 뒤 삭제
IDLE_NOTICE_AFTER = timedelta(days=2)
DELETE_AFTER_NOTICE = timedelta(days=1)
DELETE_CLOSED_AFTER = timedelta(days=1)
CHECK_INTERVAL_SECONDS = 3600
NOTICE_TITLE = "🗑️ 티켓 삭제 예정"

_cleanup_task: asyncio.Task | None = None


def _opener_ids(channel: discord.TextChannel) -> list[int]:
    ids = []
    for target in channel.overwrites:
        member = channel.guild.get_member(target.id)
        if not isinstance(target, discord.Role) and not (member and member.bot):
            ids.append(target.id)
    return ids


def _texts(components) -> list[str]:
    out = []
    for c in components:
        if isinstance(c, discord.components.TextDisplay):
            out.append(c.content)
        out += _texts(getattr(c, 'children', []))
    return out


async def _delete(channel: discord.TextChannel, reason: str) -> None:
    await channel.delete(reason="티켓 자동 정리")
    logger.info(f"[티켓] {channel.name} 삭제 - {reason}")


async def cleanup_tickets(client: ScrimBot) -> None:
    guild = client.get_guild(settings.GUILD_ID)
    category = guild and guild.get_channel(settings.TICKET_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        logger.warning("[티켓] 티켓 카테고리를 찾을 수 없습니다.")
        return

    now = datetime.now(timezone.utc)
    for channel in category.text_channels:
        if not channel.name.startswith(('ticket-', 'closed-')):
            continue
        try:
            last = [m async for m in channel.history(limit=1)]
            idle = now - (last[0].created_at if last else channel.created_at)

            if channel.name.startswith('closed-'):
                if idle >= DELETE_CLOSED_AFTER:
                    await _delete(channel, "닫힌 뒤 하루 경과")
            elif last and last[0].author.id == client.user.id and any(NOTICE_TITLE in t for t in _texts(last[0].components)):
                if idle >= DELETE_AFTER_NOTICE:
                    await _delete(channel, "삭제 예정 안내 뒤 하루 동안 대화 없음")
            elif idle >= IDLE_NOTICE_AFTER:
                mentions = ' '.join(f"<@{uid}>" for uid in _opener_ids(channel))
                at = int((now + DELETE_AFTER_NOTICE).timestamp())
                await channel.send(
                    view=warning_view(
                        f"{mentions} 이틀 동안 대화가 없어 <t:{at}:f>에 삭제됩니다.\n문의가 남아 있으면 메시지를 남겨주세요.",
                        title=NOTICE_TITLE,
                    ),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                logger.info(f"[티켓] {channel.name} 삭제 예고")
        except discord.HTTPException as e:
            logger.warning(f"[티켓] {channel.name} 정리 실패: {e}")
        await asyncio.sleep(1)


async def _cleanup_loop(client: ScrimBot) -> None:
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            await cleanup_tickets(client)
        except Exception as e:
            logger.error(f"[티켓] 자동 정리 실패: {e}", exc_info=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def setup_ticket_cleanup(client: ScrimBot) -> None:
    global _cleanup_task
    if _cleanup_task is not None:
        _cleanup_task.cancel()
    _cleanup_task = asyncio.create_task(_cleanup_loop(client))
