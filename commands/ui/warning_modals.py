"""
경고/주의 Modal 컴포넌트들
"""
import discord
from discord.components import RadioGroupOption
from discord.ui import Label, Modal, RadioGroup, TextDisplay, TextInput

from bot.manager import BotManager
from models.warning_manager import WarningManager
from utils.layout_helpers import (
    error_view, custom_view,
    send_response,
)
from config.logging_config import get_logger

logger = get_logger('warning_modals')

# 사유 선택지 → 부여 유형. 제재 정책의 단일 출처
REASON_TYPE = {
    '지각': '경고',
    '대타': '주의',
    '기타주의': '주의',
    '기타경고': '경고',
}

# 주의(caution) 알림 강조색
CAUTION_COLOR = discord.Color.from_str('#FEE75C')

MASTERS_NOTE = ("💡 안내", "마스터즈 진행일은 제한 일수에서 차감되지 않습니다.")


def _caution_history(cautions: list, detailed: bool) -> str:
    """누적 주의 내역 문자열을 만듭니다. detailed는 DM용 줄바꿈 포맷."""
    lines = []
    for i, caution in enumerate(cautions or [], 1):
        caution_date = caution.get('날짜', 'N/A')
        caution_reason = caution.get('사유', 'N/A')
        if detailed:
            lines.append(f"`{i}회` {caution_date}\n└ {caution_reason}")
        else:
            lines.append(f"`{i}회` {caution_date}: {caution_reason}")
    if not lines:
        return "내역 없음"
    return ("\n\n" if detailed else "\n").join(lines)


def _count_summary(auto_warning: dict) -> str:
    info = auto_warning or {}
    return f"{info.get('warning_count', 'N/A')}회 · 제한 {info.get('duration_days', 'N/A')}일"


def _restriction_summary(auto_warning: dict) -> str:
    info = auto_warning or {}
    return (
        f"**{info.get('restricted_until', 'N/A')}**까지 스크림 참여가 제한됩니다. "
        f"(누적 {_count_summary(info)})"
    )


async def send_sanction_dm(
    target_user: discord.Member,
    warning_type: str,
    reason: str,
    auto_warning: dict = None,
    converted_cautions: list = None,
) -> None:
    """제재 부여 DM을 발송합니다. 실패는 로그만 남깁니다."""
    try:
        # 주의 누적으로 경고 전환된 경우
        if auto_warning and converted_cautions:
            fields = [
                ("📋 누적 주의 내역", _caution_history(converted_cautions, detailed=True)),
                ("🚫 참여 제한", _restriction_summary(auto_warning)),
                MASTERS_NOTE,
            ]
            dm_view = custom_view(
                "🚨 경고 알림",
                f"주의 {WarningManager.CAUTION_TO_WARNING_COUNT}회 누적으로 인해 **경고**가 부여되었습니다.",
                discord.Color.red(),
                fields=fields,
            )

        # 일반 경고인 경우 (직접 부여)
        elif warning_type == '경고':
            fields = [
                ("📝 사유", reason),
                ("🚫 참여 제한", _restriction_summary(auto_warning)),
                MASTERS_NOTE,
            ]
            dm_view = custom_view("🚨 경고 알림", "**경고**가 부여되었습니다.", discord.Color.red(), fields=fields)

        # 주의인 경우
        else:
            fields = [
                ("📝 사유", reason),
                ("💡 안내", f"주의 {WarningManager.CAUTION_TO_WARNING_COUNT}회 누적 시 경고로 전환되며,\n스크림 참여가 제한됩니다."),
            ]
            dm_view = custom_view("⚡ 주의 알림", "**주의**가 부여되었습니다.", CAUTION_COLOR, fields=fields)

        await target_user.send(view=dm_view)

    except discord.Forbidden:
        logger.warning(f"[제재DM] 발송 실패 (DM 차단) - 대상: {target_user.display_name}")
    except Exception as e:
        logger.error(f"[제재DM] 발송 실패 - 대상: {target_user.display_name}, 오류: {e}", exc_info=True)


class WarningReasonModal(Modal):
    """
    경고/주의 사유 입력 모달 (통합)

    사유 선택이 유형(주의/경고)을 결정하고,
    TextInput으로 상세 사유를 입력받습니다.
    """

    def __init__(self, target_user: discord.Member):
        super().__init__(title="제재 부여")
        self.target_user = target_user

        # 사유 선택 (사유가 유형을 결정)
        self.reason_radio = RadioGroup(
            options=[
                RadioGroupOption(label="지각", value="지각", description="경고, 참여 제한"),
                RadioGroupOption(label="대타", value="대타", description="주의"),
                RadioGroupOption(label="기타 (주의)", value="기타주의", description="사유 직접 입력"),
                RadioGroupOption(label="기타 (경고)", value="기타경고", description="사유 직접 입력"),
            ],
            required=True,
        )
        self.add_item(Label(text="사유", component=self.reason_radio))

        # 상세 사유 입력
        self.detail_input = TextInput(
            placeholder="기타 선택 시 필수 / 그 외 추가 설명 (선택사항)",
            max_length=200,
            required=False,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(Label(
            text="상세 사유",
            description="간략하게 작성해주세요.",
            component=self.detail_input,
        ))

        # 안내 문구
        self.add_item(TextDisplay(content="📢 제재 부여 시 대상자에게 DM으로 알림이 발송됩니다."))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            # 입력 데이터 수집 (사유가 유형을 결정)
            reason_choice = self.reason_radio.value
            detail = self.detail_input.value.strip() if self.detail_input.value else ""

            warning_type = REASON_TYPE[reason_choice]
            if reason_choice in ("기타주의", "기타경고"):
                if not detail:
                    await interaction.followup.send(view=error_view("기타를 선택한 경우 상세 사유를 입력해주세요."), ephemeral=True)
                    return
                reason = detail
            else:
                reason = f"{reason_choice} - {detail}" if detail else reason_choice

            target_nickname = self.target_user.display_name or self.target_user.name
            target_id = str(self.target_user.id)
            admin_display_name = interaction.user.display_name or interaction.user.name

            warning_manager = BotManager.get_instance().get_warning_manager()

            success, message, auto_warning, converted_cautions = await warning_manager.add_warning(
                target=target_nickname,
                target_id=target_id,
                warning_type=warning_type,
                reason=reason,
                admin_display_name=admin_display_name
            )

            if success:

                # 주의 누적으로 경고 전환된 경우
                if auto_warning and converted_cautions:
                    fields = [
                        ("📌 대상", f"{self.target_user.mention} (`{target_nickname}`)"),
                        ("🚫 제한 해제일", f"`{auto_warning.get('restricted_until', 'N/A')}`"),
                        ("📊 누적 경고", _count_summary(auto_warning)),
                        ("📝 이번 주의 사유", reason),
                        ("📋 누적 주의 내역", _caution_history(converted_cautions, detailed=False)),
                    ]
                    view_result = custom_view(
                        "🚨 경고 자동 부여 완료",
                        f"주의 {WarningManager.CAUTION_TO_WARNING_COUNT}회 누적으로 경고가 자동 부여되었습니다.",
                        discord.Color.red(),
                        fields=fields,
                    )

                # 일반 경고인 경우
                elif warning_type == '경고':
                    warning_info = auto_warning or {}
                    fields = [
                        ("📌 대상", f"{self.target_user.mention} (`{target_nickname}`)"),
                        ("🚫 제한 해제일", f"`{warning_info.get('restricted_until', 'N/A')}`"),
                        ("📊 누적 경고", _count_summary(warning_info)),
                        ("📝 사유", reason),
                    ]
                    view_result = custom_view("🚨 경고 부여 완료", "", discord.Color.red(), fields=fields)

                # 주의인 경우
                else:
                    fields = [
                        ("📌 대상", f"{self.target_user.mention} (`{target_nickname}`)"),
                        ("📝 사유", reason),
                        ("💡 참고", f"주의 {WarningManager.CAUTION_TO_WARNING_COUNT}회 누적 시 경고로 자동 전환됩니다."),
                    ]
                    view_result = custom_view("⚡ 주의 부여 완료", "", CAUTION_COLOR, fields=fields)

                await send_sanction_dm(
                    self.target_user, warning_type, reason,
                    auto_warning=auto_warning,
                    converted_cautions=converted_cautions,
                )
            else:
                logger.error(f"[모달] {warning_type} 추가 실패 - 대상: {target_nickname}, 메시지: {message}")
                view_result = error_view(message, title="❌ 처리 실패")

            await interaction.followup.send(view=view_result, ephemeral=True)

        except Exception as e:
            logger.error(f"[모달] 제재 모달 처리 실패 - 대상: {self.target_user.display_name if self.target_user else 'Unknown'}, 오류: {e}", exc_info=True)
            await send_response(interaction, error_view("제재 처리 중 오류가 발생했습니다."))
