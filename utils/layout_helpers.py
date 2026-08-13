"""
표준 LayoutView 빌더 및 전송 유틸리티

모든 사용자 응답을 일관된 Components V2 (LayoutView) 형식으로 제공합니다.

톤앤매너 규칙:
  - 존칭: 하십시오체 통일 ("~입니다", "~해주세요", "~없습니다")
  - 푸터: 모든 응답에 포함
  - 구조: ## 타이틀 → 본문 → Separator → 푸터
"""
import time

import discord
from discord.ui import (
    Container,
    LayoutView,
    MediaGallery,
    Separator,
    TextDisplay,
)

from config.logging_config import get_logger
from config.settings import settings

logger = get_logger('layout_helpers')

FOOTER_TEXT = f"-# {settings.EMBED_FOOTER_TEXT}"


# ---------------------------------------------------------------------------
# 팩토리 함수 (Container + TextDisplay + 푸터)
# ---------------------------------------------------------------------------

def _build_view(title: str, description: str, accent_color: discord.Color) -> LayoutView:
    view = LayoutView()
    container = Container(
        TextDisplay(content=f"## {title}\n{description}"),
        Separator(),
        TextDisplay(content=FOOTER_TEXT),
        accent_colour=accent_color,
    )
    view.add_item(container)
    return view


def error_view(description: str, title: str = "❌ 오류") -> LayoutView:
    return _build_view(title, description, discord.Color.red())


def success_view(description: str, title: str = "✅ 완료") -> LayoutView:
    return _build_view(title, description, discord.Color.green())


def warning_view(description: str, title: str = "⚠️ 확인") -> LayoutView:
    return _build_view(title, description, discord.Color.orange())


def info_view(description: str, title: str = "스크림 안내") -> LayoutView:
    return _build_view(title, description, discord.Color.blue())


def processing_view(description: str = "잠시만 기다려주세요.") -> LayoutView:
    return _build_view("⏳ 처리 중...", description, discord.Color.blue())


def timeout_view(description: str = "시간이 초과되었습니다. 다시 시도해주세요.") -> LayoutView:
    return _build_view("⏳ 시간 초과", description, discord.Color.greyple())


def permission_error_view(description: str = "관리자 권한이 없습니다.") -> LayoutView:
    return _build_view("❌ 권한 없음", description, discord.Color.red())


# ---------------------------------------------------------------------------
# 커스텀 빌더 (추가 필드가 필요한 응답용)
# ---------------------------------------------------------------------------

def custom_view(
    title: str,
    description: str,
    accent_color: discord.Color,
    *,
    fields: list[tuple[str, str]] | None = None,
) -> LayoutView:
    """추가 필드가 포함된 커스텀 LayoutView를 생성합니다.

    Args:
        fields: [("필드명", "필드값"), ...] 형태의 리스트
    """
    children: list = [TextDisplay(content=f"## {title}\n{description}")]

    if fields:
        for name, value in fields:
            children.append(TextDisplay(content=f"**{name}**\n{value}"))

    children.append(Separator())
    children.append(TextDisplay(content=FOOTER_TEXT))

    view = LayoutView()
    view.add_item(Container(*children, accent_colour=accent_color))
    return view


# ---------------------------------------------------------------------------
# 이미지 포함 빌더 (MediaGallery)
# ---------------------------------------------------------------------------

def image_response_view(
    title: str,
    description: str,
    image_url: str,
    accent_color: discord.Color,
    *,
    fields: list[tuple[str, str]] | None = None,
) -> LayoutView:
    """이미지가 포함된 LayoutView를 생성합니다.

    image_url 에 attachment://filename.png 형식을 전달하면
    send_response / edit_to_layout 의 files 매개변수로 첨부할 수 있습니다.
    """
    children: list = [TextDisplay(content=f"## {title}\n{description}")]

    if fields:
        for name, value in fields:
            children.append(TextDisplay(content=f"**{name}**\n{value}"))

    children.append(
        MediaGallery(discord.MediaGalleryItem(media=image_url))
    )
    children.append(Separator())
    children.append(TextDisplay(content=FOOTER_TEXT))

    view = LayoutView()
    view.add_item(Container(*children, accent_colour=accent_color))
    return view


# ---------------------------------------------------------------------------
# 전송 유틸리티
# ---------------------------------------------------------------------------

async def send_response(
    interaction: discord.Interaction,
    view: LayoutView,
    *,
    ephemeral: bool = True,
    files: list[discord.File] | None = None,
) -> discord.Message | None:
    """LayoutView를 interaction 응답으로 전송합니다.

    ``is_done()`` 여부를 자동으로 확인하여 ``send_message`` 또는
    ``followup.send`` 를 사용합니다.
    """
    try:
        kwargs: dict = {"view": view, "ephemeral": ephemeral}
        if files:
            kwargs["files"] = files

        if not interaction.response.is_done():
            await interaction.response.send_message(**kwargs)
            return await interaction.original_response()
        else:
            return await interaction.followup.send(**kwargs, wait=True)
    except Exception as e:
        logger.error(f"[레이아웃] 응답 전송 실패: {e}", exc_info=True)
        # 폴백: 일반 텍스트
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "오류가 발생했습니다.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "오류가 발생했습니다.", ephemeral=True
                )
        except Exception:
            pass
        return None


async def update_temp_message(temp_message: discord.Message, message: str, color: discord.Color) -> None:
    """임시 메시지를 LayoutView로 업데이트합니다."""
    try:
        if color == discord.Color.green():
            view = success_view(message)
        elif color == discord.Color.red():
            view = error_view(message)
        elif color == discord.Color.orange():
            view = warning_view(message)
        else:
            view = info_view(message)
        await edit_to_layout(temp_message, view)
    except Exception as e:
        logger.error(f"[레이아웃] 임시 메시지 업데이트 실패: {e}", exc_info=True)


async def send_error_message(interaction: discord.Interaction, message: str) -> None:
    """에러 메시지를 전송하는 공통 유틸리티 함수"""
    try:
        await send_response(interaction, error_view(message))
    except Exception as e:
        logger.error(f"[레이아웃] 에러 메시지 전송 실패: {e}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"오류: {message}", ephemeral=True)
            else:
                await interaction.followup.send(f"오류: {message}", ephemeral=True)
        except Exception:
            pass


async def upsert_persistent_message(
    channel: discord.abc.Messageable,
    message_id: int | None,
    view: LayoutView,
    *,
    files: list[discord.File] | None = None,
) -> int:
    """상시 메시지(대시보드 등)를 기존 메시지 편집으로 갱신하고, 불가하면 재생성합니다.

    편집이 일시 오류(HTTPException)로 실패하면 옛 메시지를 삭제 시도한 뒤
    새로 보내, 옛 대시보드가 방치되어 이중으로 남는 것을 막습니다.

    Returns:
        갱신(또는 재생성)된 메시지 id
    """
    old_message: discord.Message | None = None
    if message_id:
        try:
            old_message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.HTTPException):
            old_message = None

    if old_message:
        edit_kwargs: dict = {"view": view, "content": None, "embed": None}
        if files:
            edit_kwargs["attachments"] = files
        try:
            await old_message.edit(**edit_kwargs)
            return old_message.id
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.warning(f"[레이아웃] 상시 메시지 편집 실패 - 재생성: {e}")
            try:
                await old_message.delete()
            except Exception:
                pass

    send_kwargs: dict = {"view": view}
    if files:
        send_kwargs["files"] = files
    new_message = await channel.send(**send_kwargs)
    return new_message.id


async def edit_to_layout(
    message: discord.Message,
    view: LayoutView,
    *,
    files: list[discord.File] | None = None,
) -> None:
    """기존 메시지(embed 포함 가능)를 LayoutView로 교체합니다.

    embed 및 content 를 None 으로 지정하여 기존 임베드를 제거합니다.
    """
    try:
        kwargs: dict = {"view": view, "embed": None, "content": None}
        if files:
            kwargs["attachments"] = files
        await message.edit(**kwargs)
    except Exception as e:
        logger.error(f"[레이아웃] 메시지 편집 실패: {e}", exc_info=True)


# 버튼 cooldown 관리 (사용자별 마지막 클릭 시간)
_button_cooldowns: dict = {}
BUTTON_COOLDOWN_SECONDS = 1
_COOLDOWN_CLEANUP_THRESHOLD = 100  # 이 크기 초과 시 만료 항목 정리


async def check_cooldown(interaction: discord.Interaction, cooldown_seconds: float = BUTTON_COOLDOWN_SECONDS) -> bool:
    """버튼 cooldown을 확인합니다. True면 cooldown 중이므로 무시해야 합니다."""
    user_id = interaction.user.id
    now = time.monotonic()
    last_click = _button_cooldowns.get(user_id, 0)
    if now - last_click < cooldown_seconds:
        await send_response(interaction, info_view("요청 처리 중입니다. 잠시 기다려주세요.", title="⏳ 대기"))
        return True
    _button_cooldowns[user_id] = now
    # 만료된 쿨다운 항목 주기적 정리
    if len(_button_cooldowns) > _COOLDOWN_CLEANUP_THRESHOLD:
        expired = [uid for uid, t in _button_cooldowns.items() if now - t > cooldown_seconds]
        for uid in expired:
            del _button_cooldowns[uid]
    return False
