import asyncio
import io
from datetime import datetime, timedelta, timezone

import discord
from discord.ui import Container, File, LayoutView, Separator, TextDisplay

from bot.client import ScrimBot
from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import KST
from utils.layout_helpers import FOOTER_TEXT, warning_view

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


def _line(message: discord.Message) -> str:
    stamp = message.created_at.astimezone(KST).strftime('%Y-%m-%d %H:%M')
    parts = [message.content] if message.content else []
    parts += [e.description or e.title or '' for e in message.embeds]
    parts += _texts(message.components)
    parts += [a.url for a in message.attachments]
    body = '\n    '.join(p for p in parts if p) or '(내용 없음)'
    return f"[{stamp}] {message.author.display_name} ({message.author.id}): {body}"


async def _archive_and_delete(channel: discord.TextChannel, log_channel: discord.TextChannel, reason: str) -> None:
    lines = [_line(m) async for m in channel.history(limit=None, oldest_first=True)]
    openers = ' '.join(f"<@{uid}>" for uid in _opener_ids(channel)) or '알 수 없음'
    filename = f"{channel.name}.txt"
    view = LayoutView()
    view.add_item(Container(
        TextDisplay(content=f"## 🗂️ {channel.name}\n{reason}\n\n신청자 {openers}\n메시지 {len(lines)}개"),
        File(media=f"attachment://{filename}"),
        Separator(),
        TextDisplay(content=FOOTER_TEXT),
        accent_colour=discord.Color.greyple(),
    ))
    # 기록을 남기지 못하면 지우지 않음
    await log_channel.send(
        view=view, file=discord.File(io.BytesIO('\n'.join(lines).encode('utf-8')), filename=filename),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await channel.delete(reason="티켓 자동 정리")
    logger.info(f"[티켓] {channel.name} 삭제 - {reason}")


async def cleanup_tickets(client: ScrimBot) -> None:
    guild = client.get_guild(settings.GUILD_ID)
    category = guild and guild.get_channel(settings.TICKET_CATEGORY_ID)
    log_channel = guild and guild.get_channel(settings.TICKET_LOG_CHANNEL_ID)
    if not isinstance(category, discord.CategoryChannel) or not isinstance(log_channel, discord.TextChannel):
        logger.warning("[티켓] 티켓 카테고리나 기록 채널을 찾을 수 없습니다.")
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
                    await _archive_and_delete(channel, log_channel, "닫힌 뒤 하루가 지나 삭제했습니다.")
            elif last and last[0].author.id == client.user.id and any(NOTICE_TITLE in t for t in _texts(last[0].components)):
                if idle >= DELETE_AFTER_NOTICE:
                    await _archive_and_delete(channel, log_channel, "삭제 예정 안내 뒤 하루 동안 대화가 없어 삭제했습니다.")
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
