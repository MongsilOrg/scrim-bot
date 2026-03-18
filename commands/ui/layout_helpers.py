"""
표준 LayoutView 빌더 및 전송 유틸리티

모든 사용자 응답을 일관된 Components V2 (LayoutView) 형식으로 제공합니다.

톤앤매너 규칙:
  - 존칭: 하십시오체 통일 ("~입니다", "~해주세요", "~없습니다")
  - 푸터: 모든 응답에 포함
  - 구조: ## 타이틀 → 본문 → Separator → 푸터
"""
import discord
from discord.ui import (
    ActionRow,
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
    """기본 LayoutView를 생성합니다."""
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
    """에러 응답 LayoutView를 생성합니다."""
    return _build_view(title, description, discord.Color.red())


def success_view(description: str, title: str = "✅ 완료") -> LayoutView:
    """성공 응답 LayoutView를 생성합니다."""
    return _build_view(title, description, discord.Color.green())


def warning_view(description: str, title: str = "⚠️ 확인") -> LayoutView:
    """경고 응답 LayoutView를 생성합니다."""
    return _build_view(title, description, discord.Color.orange())


def info_view(description: str, title: str = "스크림 안내") -> LayoutView:
    """정보 응답 LayoutView를 생성합니다."""
    return _build_view(title, description, discord.Color.blue())


def processing_view(description: str = "잠시만 기다려주세요.") -> LayoutView:
    """처리 중 응답 LayoutView를 생성합니다."""
    return _build_view("⏳ 처리 중...", description, discord.Color.blue())


def timeout_view(description: str = "시간이 초과되었습니다. 다시 시도해주세요.") -> LayoutView:
    """타임아웃 응답 LayoutView를 생성합니다."""
    return _build_view("⏳ 시간 초과", description, discord.Color.greyple())


def permission_error_view(description: str = "관리자 권한이 없습니다.") -> LayoutView:
    """권한 에러 응답 LayoutView를 생성합니다."""
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
