import discord
from discord.components import RadioGroupOption
from discord import ButtonStyle
from discord.ui import ActionRow, Button, Container, Label, Modal, RadioGroup, Separator, TextDisplay, TextInput

from bot.manager import BotManager
from models.warning_manager import MASTERS_NOT_DEDUCTED, WarningManager
from utils.layout_helpers import (
    error_view, custom_view,
    send_response, FOOTER_TEXT,
    TimeoutEditView,
    format_kr_date,
)
from config.logging_config import get_logger

logger = get_logger('warning_modals')

REASON_TYPE = {
    '지각': '경고',
    '대타': '주의',
    '기타주의': '주의',
    '기타경고': '경고',
}

CAUTION_COLOR = discord.Color.from_str('#FEE75C')

MASTERS_NOTE = ("안내", MASTERS_NOT_DEDUCTED)
NO_RECORD = "기록 없음"


def _caution_history(cautions: list, detailed: bool) -> str:
    lines = []
    for i, caution in enumerate(cautions or [], 1):
        caution_date = format_kr_date(caution.get('날짜') or NO_RECORD)
        caution_reason = caution.get('사유') or NO_RECORD
        if detailed:
            lines.append(f"`{i}회` {caution_date}\n{caution_reason}")
        else:
            lines.append(f"`{i}회` {caution_date}: {caution_reason}")
    if not lines:
        return "내역 없음"
    return ("\n\n" if detailed else "\n").join(lines)


def _count_summary(auto_warning: dict) -> str:
    info = auto_warning or {}
    return f"{info.get('warning_count', NO_RECORD)}회, 제한 {info.get('duration_days', NO_RECORD)}일"


def _restricted_until(auto_warning: dict) -> str:
    return format_kr_date((auto_warning or {}).get('restricted_until') or NO_RECORD)


def _restriction_summary(auto_warning: dict) -> str:
    return (
        f"**{_restricted_until(auto_warning)}**까지 스크림에 참여할 수 없습니다.\n"
        f"누적 경고 {_count_summary(auto_warning)}"
    )


async def send_sanction_dm(
    target_user: discord.Member,
    warning_type: str,
    reason: str,
    auto_warning: dict = None,
    converted_cautions: list = None,
) -> bool:
    """대상자에게 DM이 갔으면 True."""
    try:
        if auto_warning and converted_cautions:
            fields = [
                ("누적 주의 내역", _caution_history(converted_cautions, detailed=True)),
                ("참여 제한", _restriction_summary(auto_warning)),
                MASTERS_NOTE,
            ]
            dm_view = custom_view(
                "🚨 경고 알림",
                f"주의가 {WarningManager.CAUTION_TO_WARNING_COUNT}회 쌓여 **경고**가 부여되었습니다.",
                discord.Color.red(),
                fields=fields,
            )

        elif warning_type == '경고':
            fields = [
                ("사유", reason),
                ("참여 제한", _restriction_summary(auto_warning)),
                MASTERS_NOTE,
            ]
            dm_view = custom_view("🚨 경고 알림", "**경고**가 부여되었습니다.", discord.Color.red(), fields=fields)

        else:
            fields = [
                ("사유", reason),
                ("안내", f"주의가 {WarningManager.CAUTION_TO_WARNING_COUNT}회 쌓이면 경고로 바뀌고 스크림 참여가 제한됩니다."),
            ]
            dm_view = custom_view("⚡ 주의 알림", "**주의**가 부여되었습니다.", CAUTION_COLOR, fields=fields)

        await target_user.send(view=dm_view)
        return True

    except discord.Forbidden:
        logger.warning(f"[제재DM] 발송 실패 (DM 차단) - 대상: {target_user.display_name}")
    except Exception as e:
        logger.error(f"[제재DM] 발송 실패 - 대상: {target_user.display_name}, 오류: {e}", exc_info=True)
    return False


DM_FAILED_TEXT = "DM을 보내지 못했습니다. 대상자에게 직접 알려주세요."


def _registered_team(target_user: discord.Member) -> str | None:
    try:
        team_data_manager = BotManager.get_instance().get_team_data_manager()
        return team_data_manager.find_user_team(str(target_user.id), member=target_user)
    except Exception as e:
        logger.warning(f"[모달] 등록 팀 조회 실패 - 대상: {target_user.display_name}, 오류: {e}")
        return None


def _follow_up_fields(target_user: discord.Member, dm_sent: bool, restricted: bool) -> list:
    fields = []
    if not dm_sent:
        fields.append(("DM", DM_FAILED_TEXT))
    if restricted:
        team_name = _registered_team(target_user)
        if team_name:
            fields.append((
                "신청 팀",
                f"현재 **{team_name}** 팀에 등록되어 있습니다. 필요하면 관리 버튼에서 강제 취소해주세요.",
            ))
    return fields


MISSING_DETAIL_TEXT = "기타를 고르면 상세 사유를 적어야 합니다. 다시 입력 버튼을 눌러주세요."


def _missing_detail_view(target_user: discord.Member, reason_choice: str) -> TimeoutEditView:
    view = TimeoutEditView()
    view.add_item(Container(
        TextDisplay(content=f"## ❌ 상세 사유 없음\n{MISSING_DETAIL_TEXT}"),
        Separator(),
        TextDisplay(content=FOOTER_TEXT),
        accent_colour=discord.Color.red(),
    ))
    retry_button = Button(label="다시 입력", style=ButtonStyle.primary)

    async def reopen(btn_interaction: discord.Interaction) -> None:
        await btn_interaction.response.send_modal(WarningReasonModal(target_user, reason_choice))

    retry_button.callback = reopen
    view.add_item(ActionRow(retry_button))
    return view


class WarningReasonModal(Modal):

    def __init__(self, target_user: discord.Member, selected: str | None = None):
        super().__init__(title="제재 부여")
        self.target_user = target_user

        choices = [
            ("지각", "지각", "경고, 참여 제한"),
            ("대타", "대타", "주의"),
            ("기타 주의", "기타주의", "사유 직접 입력"),
            ("기타 경고", "기타경고", "사유 직접 입력"),
        ]
        self.reason_radio = RadioGroup(
            options=[
                RadioGroupOption(label=label, value=value, description=description, default=value == selected)
                for label, value, description in choices
            ],
            required=True,
        )
        self.add_item(Label(text="사유", component=self.reason_radio))

        self.detail_input = TextInput(
            placeholder="기타 선택 시 필수, 그 외에는 추가 설명",
            max_length=200,
            required=False,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(Label(
            text="상세 사유",
            description="간략하게 작성해주세요.",
            component=self.detail_input,
        ))

        self.add_item(TextDisplay(content="제재를 부여하면 대상자에게 DM으로 알립니다."))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            reason_choice = self.reason_radio.value
            detail = self.detail_input.value.strip() if self.detail_input.value else ""

            # 모달 제출에는 모달로 답할 수 없어 다시 여는 버튼을 붙임
            if reason_choice in ("기타주의", "기타경고") and not detail:
                retry_view = _missing_detail_view(self.target_user, reason_choice)
                retry_view.message = await send_response(interaction, retry_view)
                return

            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            warning_type = REASON_TYPE[reason_choice]
            if reason_choice in ("기타주의", "기타경고"):
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
                dm_sent = await send_sanction_dm(
                    self.target_user, warning_type, reason,
                    auto_warning=auto_warning,
                    converted_cautions=converted_cautions,
                )
                follow_up = _follow_up_fields(self.target_user, dm_sent, restricted=bool(auto_warning))
                target_line = f"{self.target_user.mention} `{target_nickname}`"

                if auto_warning and converted_cautions:
                    fields = [
                        ("대상", target_line),
                        ("참여 제한", f"{_restricted_until(auto_warning)}까지"),
                        ("누적 경고", _count_summary(auto_warning)),
                        ("방금 부여한 주의 사유", reason),
                        ("누적 주의 내역", _caution_history(converted_cautions, detailed=False)),
                        *follow_up,
                    ]
                    view_result = custom_view(
                        "🚨 경고 자동 부여 완료",
                        f"주의가 {WarningManager.CAUTION_TO_WARNING_COUNT}회 쌓여 경고가 자동 부여되었습니다.",
                        discord.Color.red(),
                        fields=fields,
                    )

                elif warning_type == '경고':
                    fields = [
                        ("대상", target_line),
                        ("참여 제한", f"{_restricted_until(auto_warning)}까지"),
                        ("누적 경고", _count_summary(auto_warning)),
                        ("사유", reason),
                        *follow_up,
                    ]
                    view_result = custom_view("🚨 경고 부여 완료", "", discord.Color.red(), fields=fields)

                else:
                    fields = [
                        ("대상", target_line),
                        ("사유", reason),
                        ("참고", f"주의가 {WarningManager.CAUTION_TO_WARNING_COUNT}회 쌓이면 경고로 자동 전환됩니다."),
                        *follow_up,
                    ]
                    view_result = custom_view("⚡ 주의 부여 완료", "", CAUTION_COLOR, fields=fields)
            else:
                logger.error(f"[모달] {warning_type} 추가 실패 - 대상: {target_nickname}, 메시지: {message}")
                view_result = error_view(message, title="❌ 처리 실패")

            await interaction.followup.send(view=view_result, ephemeral=True)

        except Exception as e:
            logger.error(f"[모달] 제재 모달 처리 실패 - 대상: {self.target_user.display_name if self.target_user else 'Unknown'}, 오류: {e}", exc_info=True)
            await send_response(interaction, error_view("제재를 처리하지 못했습니다. 시트에 기록됐는지 확인한 뒤 다시 시도해주세요."))
