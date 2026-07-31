"""
경고/주의 및 일정 관련 Modal 컴포넌트들
"""
import discord
from discord.components import CheckboxGroupOption, RadioGroupOption
from discord.ui import CheckboxGroup, Label, Modal, RadioGroup, TextDisplay, TextInput

from bot.manager import BotManager
from commands.ui.layout_helpers import (
    error_view, success_view, custom_view,
    send_response,
)
from config.logging_config import get_logger

logger = get_logger('warning_modals')


class WarningReasonModal(Modal):
    """
    경고/주의 사유 입력 모달 (통합)

    Select로 유형(주의/경고)과 사유(지각/대타/직접입력)를 선택하고,
    TextInput으로 상세 사유를 입력받습니다.
    """

    def __init__(self, target_user: discord.Member):
        super().__init__(title="제재 부여")
        self.target_user = target_user

        # 유형 선택 (주의/경고)
        self.type_radio = RadioGroup(
            options=[
                RadioGroupOption(label="주의", value="주의", description="주의 2회 누적 시 경고로 전환"),
                RadioGroupOption(label="경고", value="경고", description="즉시 스크림 참여 제한"),
            ],
            required=True,
        )
        self.add_item(Label(text="유형", component=self.type_radio))

        # 사유 선택 (지각/대타/직접입력)
        self.reason_radio = RadioGroup(
            options=[
                RadioGroupOption(label="지각", value="지각"),
                RadioGroupOption(label="대타", value="대타"),
                RadioGroupOption(label="직접입력", value="직접입력", description="상세 사유에 직접 입력"),
            ],
            required=True,
        )
        self.add_item(Label(text="사유", component=self.reason_radio))

        # 상세 사유 입력
        self.detail_input = TextInput(
            placeholder="직접입력 선택 시 필수 / 그 외 추가 설명 (선택사항)",
            max_length=200,
            required=False,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(Label(
            text="상세 사유",
            description="'지각', '대타'처럼 한 단어로 간략하게 작성해주세요.",
            component=self.detail_input,
        ))

        # 안내 문구
        self.add_item(TextDisplay(content="📢 제재 부여 시 대상자에게 DM으로 알림이 발송됩니다."))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모달 제출 처리"""
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            # 입력 데이터 수집
            warning_type = self.type_radio.value
            reason_choice = self.reason_radio.value
            detail = self.detail_input.value.strip() if self.detail_input.value else ""

            # 사유 결합
            if reason_choice == "직접입력":
                if not detail:
                    await interaction.followup.send(view=error_view("직접입력을 선택한 경우 상세 사유를 입력해주세요."), ephemeral=True)
                    return
                reason = detail
            else:
                reason = f"{reason_choice} - {detail}" if detail else reason_choice

            target_nickname = self.target_user.display_name or self.target_user.name
            target_id = str(self.target_user.id)
            admin_display_name = interaction.user.display_name or interaction.user.name

            # WarningManager를 통해 경고 추가
            warning_manager = BotManager.get_instance().get_warning_manager()

            success, message, auto_warning, converted_cautions = await warning_manager.add_warning(
                target=target_nickname,
                target_id=target_id,
                warning_type=warning_type,
                reason=reason,
                admin_display_name=admin_display_name
            )

            if success:

                # 주의 2회 누적으로 경고 전환된 경우
                if auto_warning and converted_cautions:
                    caution_lines = []
                    for i, caution in enumerate(converted_cautions, 1):
                        caution_date = caution.get('날짜', 'N/A')
                        caution_reason = caution.get('사유', 'N/A')
                        caution_lines.append(f"`{i}회` {caution_date}: {caution_reason}")
                    fields = [
                        ("📌 대상", f"{self.target_user.mention} (`{target_nickname}`)"),
                        ("🚫 제한 해제일", f"`{auto_warning.get('restricted_until', 'N/A')}`"),
                        ("📝 이번 주의 사유", reason),
                        ("📋 누적 주의 내역", "\n".join(caution_lines) if caution_lines else "내역 없음"),
                    ]
                    view_result = custom_view(
                        "🚨 경고 자동 부여 완료",
                        "주의 2회 누적으로 경고가 자동 부여되었습니다.",
                        discord.Color.red(),
                        fields=fields,
                    )

                # 일반 경고인 경우
                elif warning_type == '경고':
                    fields = [
                        ("📌 대상", f"{self.target_user.mention} (`{target_nickname}`)"),
                        ("🚫 제한 해제일", f"`{auto_warning.get('restricted_until', 'N/A') if auto_warning else 'N/A'}`"),
                        ("📝 사유", reason),
                    ]
                    view_result = custom_view("🚨 경고 부여 완료", "", discord.Color.red(), fields=fields)

                # 주의인 경우
                else:
                    fields = [
                        ("📌 대상", f"{self.target_user.mention} (`{target_nickname}`)"),
                        ("📝 사유", reason),
                        ("💡 참고", "주의 2회 누적 시 경고로 자동 전환됩니다."),
                    ]
                    view_result = custom_view("⚡ 주의 부여 완료", "", discord.Color.from_str("#FEE75C"), fields=fields)

                # 대상자에게 DM 발송
                await self._send_warning_dm(
                    target_user=self.target_user,
                    warning_type=warning_type,
                    reason=reason,
                    admin_name=admin_display_name,
                    auto_warning=auto_warning,
                    converted_cautions=converted_cautions
                )
            else:
                logger.error(f"[모달] {warning_type} 추가 실패 - 대상: {target_nickname}, 메시지: {message}")
                view_result = error_view(message, title="❌ 처리 실패")

            await interaction.followup.send(view=view_result, ephemeral=True)

        except Exception as e:
            logger.error(f"[모달] 제재 모달 처리 실패 - 대상: {self.target_user.display_name if self.target_user else 'Unknown'}, 오류: {e}", exc_info=True)
            await send_response(interaction, error_view("제재 처리 중 오류가 발생했습니다."))

    async def _send_warning_dm(
        self,
        target_user: discord.Member,
        warning_type: str,
        reason: str,
        admin_name: str,
        auto_warning: dict = None,
        converted_cautions: list = None
    ) -> None:
        """경고/주의 부여 시 대상자에게 DM을 발송합니다."""
        try:
            # 주의 2회 누적으로 경고 전환된 경우
            if auto_warning and converted_cautions:
                restricted_until = auto_warning.get('restricted_until', 'N/A')

                caution_lines = []
                for i, caution in enumerate(converted_cautions, 1):
                    caution_date = caution.get('날짜', 'N/A')
                    caution_reason = caution.get('사유', 'N/A')
                    caution_lines.append(f"`{i}회` {caution_date}\n└ {caution_reason}")

                fields = [
                    ("📋 누적 주의 내역", "\n\n".join(caution_lines) if caution_lines else "내역 없음"),
                    ("🚫 참여 제한", f"**{restricted_until}**까지 스크림 참여가 제한됩니다."),
                ]
                dm_view = custom_view("🚨 경고 알림", "주의 2회 누적으로 인해 **경고**가 부여되었습니다.", discord.Color.red(), fields=fields)

            # 일반 경고인 경우 (직접 부여)
            elif warning_type == '경고':
                restricted_until = auto_warning.get('restricted_until', 'N/A') if auto_warning else 'N/A'

                fields = [
                    ("📝 사유", reason),
                    ("🚫 참여 제한", f"**{restricted_until}**까지 스크림 참여가 제한됩니다."),
                ]
                dm_view = custom_view("🚨 경고 알림", "**경고**가 부여되었습니다.", discord.Color.red(), fields=fields)

            # 주의인 경우
            else:
                fields = [
                    ("📝 사유", reason),
                    ("💡 안내", "주의 2회 누적 시 경고로 전환되며,\n스크림 참여가 제한됩니다."),
                ]
                dm_view = custom_view("⚡ 주의 알림", "**주의**가 부여되었습니다.", discord.Color.from_str("#FEE75C"), fields=fields)

            # DM 발송
            await target_user.send(view=dm_view)

        except discord.Forbidden:
            logger.warning(f"[모달] DM 발송 실패 (DM 차단) - 대상: {target_user.display_name}")
        except Exception as e:
            logger.error(f"[모달] DM 발송 실패 - 대상: {target_user.display_name}, 오류: {e}", exc_info=True)


class AvailabilityModal(Modal):
    """참가 요일 선택 모달"""

    def __init__(self, current_days: set):
        super().__init__(title="참가 등록")
        from models.schedule_manager import WEEKDAYS, ACTIVE_DAYS

        options = [
            CheckboxGroupOption(
                label=f"{WEEKDAYS[i]}요일",
                value=str(i),
                default=i in current_days,
            )
            for i in ACTIVE_DAYS
        ]
        self.days_checkbox = CheckboxGroup(
            options=options,
            min_values=1,
            max_values=len(ACTIVE_DAYS),
        )
        self.add_item(Label(text="참가 가능한 요일", component=self.days_checkbox))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from models.schedule_manager import WEEKDAYS
        try:
            selected_days = {int(v) for v in self.days_checkbox.values}
            schedule_mgr = BotManager.get_instance().get_schedule_manager()
            schedule_mgr.register_schedule(
                str(interaction.user.id), interaction.user.display_name, selected_days,
            )
            day_str = ', '.join(WEEKDAYS[d] for d in sorted(selected_days))
            await send_response(
                interaction,
                success_view(f"{day_str} ({len(selected_days)}일) 참가 등록되었습니다.", title="✅ 참가 등록"),
            )
            from .schedule_views import _refresh_schedule_status
            await _refresh_schedule_status(interaction)
        except Exception as e:
            logger.error(f"[모달] 참가 등록 실패: {e}", exc_info=True)
            await send_response(interaction, error_view("참가 등록 중 오류가 발생했습니다."))


class AbsenceReasonModal(Modal):
    """전체 불참 사유 입력 모달"""

    def __init__(self, current_reason: str = ''):
        super().__init__(title="불참 등록")
        self.reason_input = TextInput(
            label="불참 사유",
            placeholder="예: 개인 일정, 출장 등",
            default=current_reason,
            min_length=1,
            max_length=100,
            required=True,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            reason = self.reason_input.value.strip()
            schedule_mgr = BotManager.get_instance().get_schedule_manager()
            schedule_mgr.register_schedule(
                str(interaction.user.id), interaction.user.display_name, set(), reason,
            )
            await send_response(
                interaction,
                success_view(f"전체 불참으로 등록되었습니다.\n사유: {reason}", title="🚫 불참 등록"),
            )
            from .schedule_views import _refresh_schedule_status
            await _refresh_schedule_status(interaction)
        except Exception as e:
            logger.error(f"[모달] 불참 등록 실패: {e}", exc_info=True)
            await send_response(interaction, error_view("불참 등록 중 오류가 발생했습니다."))
