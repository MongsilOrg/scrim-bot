"""운영진이 봐야 하는 알림을 security 채널에 보냄. 역할 멘션 없이 알림만 남김."""
from typing import Optional

import discord

from config.logging_config import get_logger
from config.settings import settings

logger = get_logger('alerts')


async def send(client: Optional[discord.Client], view: discord.ui.LayoutView) -> bool:
    """다시 보내도 소용없는 실패는 True"""
    channel = client.get_channel(settings.ALERT_CHANNEL_ID) if client else None
    if channel is None:
        logger.warning(f"[알림] 알림 채널을 찾지 못해 건너뜀 - 채널 ID: {settings.ALERT_CHANNEL_ID}")
        return True
    try:
        await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        # 4xx는 같은 내용을 다시 보내도 같은 응답
        if 400 <= e.status < 500 and e.status != 429:
            logger.warning(f"[알림] 알림 채널 게시 실패: {type(e).__name__} {e.status}")
            return True
        logger.warning(f"[알림] 알림 채널 게시 실패: {type(e).__name__}")
        return False
    return True
