"""
일정 관련 Discord View 컴포넌트들
"""
import discord
from discord import ButtonStyle, Color
from discord.ui import ActionRow, Button, Container, LayoutView, Separator, TextDisplay

from bot.manager import BotManager
from config.logging_config import get_logger
from commands.ui.layout_helpers import (
    error_view, success_view,
    permission_error_view,
    send_response, FOOTER_TEXT,
)
from utils.helpers import is_admin

logger = get_logger('schedule_views')


class ScheduleView(LayoutView):
    """주간 일정 관리 뷰

    관리자들이 참가/불참을 등록하고 편성과 투입을 관리합니다.
    현황 표시 + 일정등록/응답삭제/편성/투입 버튼을 포함합니다.
    """

    def __init__(self, status_text: str, *, has_assignments: bool = False):
        super().__init__(timeout=None)

        accent = Color.green() if has_assignments else Color.blue()

        # 현황 Container
        children = [
            TextDisplay(content=status_text),
            Separator(),
            TextDisplay(content=FOOTER_TEXT),
        ]
        self.add_item(Container(*children, accent_colour=accent))

        # 버튼 ActionRow
        self.register_button = Button(label="참가", style=ButtonStyle.primary, emoji="✏️")
        self.register_button.callback = self.register_callback

        self.absence_button = Button(label="불참", style=ButtonStyle.secondary, emoji="🚫")
        self.absence_button.callback = self.absence_callback

        self.assign_button = Button(label="편성", style=ButtonStyle.success, emoji="📋")
        self.assign_button.callback = self.assign_callback

        self.deploy_button = Button(label="투입", style=ButtonStyle.secondary, emoji="✅")
        self.deploy_button.callback = self.deploy_callback

        self.add_item(ActionRow(
            self.register_button, self.absence_button,
            self.assign_button, self.deploy_button,
        ))

    async def register_callback(self, interaction: discord.Interaction) -> None:
        """참가 버튼: 요일 선택 모달"""
        from .views import _check_cooldown
        if await _check_cooldown(interaction):
            return
        if not is_admin(interaction.user):
            await send_response(interaction, permission_error_view())
            return

        schedule_mgr = BotManager.get_instance().get_schedule_manager()
        if not schedule_mgr.week_label:
            await send_response(interaction, error_view("주간 일정이 초기화되지 않았습니다."))
            return
        if schedule_mgr.assignments:
            await send_response(interaction, error_view("편성이 완료된 상태에서는 일정을 수정할 수 없습니다.\n편성 취소 후 다시 시도해주세요."))
            return

        user_id = str(interaction.user.id)
        current_days = schedule_mgr.availability.get(user_id, set())

        from .warning_modals import AvailabilityModal
        await interaction.response.send_modal(AvailabilityModal(current_days))

    async def absence_callback(self, interaction: discord.Interaction) -> None:
        """불참 버튼: 사유 입력 모달"""
        from .views import _check_cooldown
        if await _check_cooldown(interaction):
            return
        if not is_admin(interaction.user):
            await send_response(interaction, permission_error_view())
            return

        schedule_mgr = BotManager.get_instance().get_schedule_manager()
        if not schedule_mgr.week_label:
            await send_response(interaction, error_view("주간 일정이 초기화되지 않았습니다."))
            return
        if schedule_mgr.assignments:
            await send_response(interaction, error_view("편성이 완료된 상태에서는 불참을 수정할 수 없습니다.\n편성 취소 후 다시 시도해주세요."))
            return

        user_id = str(interaction.user.id)
        reasons = schedule_mgr.absence_reasons.get(user_id, {})
        current_reason = reasons.get(-1, '') if reasons else ''

        from .warning_modals import AbsenceReasonModal
        await interaction.response.send_modal(AbsenceReasonModal(current_reason))

    async def assign_callback(self, interaction: discord.Interaction) -> None:
        """편성 버튼: 상태에 따라 편성/재편성/편성취소 분기"""
        from .views import _check_cooldown
        if await _check_cooldown(interaction):
            return
        if not is_admin(interaction.user):
            await send_response(interaction, permission_error_view())
            return

        schedule_mgr = BotManager.get_instance().get_schedule_manager()
        if not schedule_mgr.week_label:
            await send_response(interaction, error_view("주간 일정이 초기화되지 않았습니다."))
            return

        # 편성 전 → 바로 편성
        if not schedule_mgr.assignments:
            if not schedule_mgr.availability:
                await send_response(interaction, error_view("참가 등록된 관리자가 없습니다."))
                return

            assignments = schedule_mgr.generate_assignments()
            assigned_days = sum(1 for v in assignments.values() if v)
            await send_response(
                interaction,
                success_view(
                    f"**{schedule_mgr.week_label}** 편성 완료\n"
                    f"{assigned_days}일 배정되었습니다.",
                    title="📋 편성 완료",
                ),
            )
            await _refresh_schedule_status(interaction)
            return

        # 편성 후 → 재편성 / 편성 취소 선택
        reassign_btn = Button(label="재편성", style=ButtonStyle.primary, emoji="🔄")
        cancel_btn = Button(label="편성 취소", style=ButtonStyle.danger, emoji="↩️")
        back_btn = Button(label="닫기", style=ButtonStyle.secondary)

        async def do_reassign(btn_interaction: discord.Interaction):
            if schedule_mgr.actual_deployments:
                await send_response(btn_interaction, error_view(
                    "투입 기록이 존재하여 재편성할 수 없습니다.\n"
                    "편성 취소 후 다시 진행해주세요."
                ))
                return
            if not schedule_mgr.availability:
                await send_response(btn_interaction, error_view("참가 등록된 관리자가 없습니다."))
                return
            assignments = schedule_mgr.generate_assignments()
            assigned_days = sum(1 for v in assignments.values() if v)
            await send_response(
                btn_interaction,
                success_view(
                    f"**{schedule_mgr.week_label}** 재편성 완료\n"
                    f"{assigned_days}일 배정되었습니다.",
                    title="🔄 재편성 완료",
                ),
            )
            await _refresh_schedule_status(btn_interaction)

        async def do_cancel(btn_interaction: discord.Interaction):
            schedule_mgr.assignments.clear()
            schedule_mgr.actual_deployments.clear()
            schedule_mgr._save_backup()
            await send_response(
                btn_interaction,
                success_view("주간 일정 편성과 투입 기록이 초기화되었습니다.", title="↩️ 편성 취소"),
            )
            await _refresh_schedule_status(btn_interaction)

        async def do_back(btn_interaction: discord.Interaction):
            await btn_interaction.response.defer_update()

        reassign_btn.callback = do_reassign
        cancel_btn.callback = do_cancel
        back_btn.callback = do_back

        deploy_count = len(schedule_mgr.actual_deployments)
        desc = "재편성 시 기존 편성을 덮어씁니다."
        if deploy_count:
            desc += f"\n편성 취소 시 투입 기록 {deploy_count}건도 함께 삭제됩니다."

        menu_view = LayoutView()
        menu_view.add_item(Container(
            TextDisplay(content=f"## 📋 편성 관리\n{desc}"),
            Separator(),
            TextDisplay(content=FOOTER_TEXT),
            accent_colour=Color.blue(),
        ))
        menu_view.add_item(ActionRow(reassign_btn, cancel_btn, back_btn))
        await send_response(interaction, menu_view)

    async def deploy_callback(self, interaction: discord.Interaction) -> None:
        """투입 기록 버튼: 요일별 버튼으로 본인 투입 토글"""
        from .views import _check_cooldown
        if await _check_cooldown(interaction):
            return
        if not is_admin(interaction.user):
            await send_response(interaction, permission_error_view())
            return

        schedule_mgr = BotManager.get_instance().get_schedule_manager()
        if not schedule_mgr.assignments:
            await send_response(interaction, error_view("주간 일정이 편성되지 않았습니다.\n먼저 편성을 실행해주세요."))
            return

        deploy_view = _build_deploy_view(schedule_mgr, str(interaction.user.id))
        await send_response(interaction, deploy_view)



def _build_deploy_view(schedule_mgr, user_id: str) -> LayoutView:
    """투입 기록 뷰를 생성합니다.

    배정된 요일은 눈에 띄게, 미배정 요일은 구분하여 표시합니다.
    """
    from models.schedule_manager import WEEKDAYS, ACTIVE_DAYS

    # 본인 배정 요일 파악
    assigned_days = {
        d for d in ACTIVE_DAYS
        if user_id in schedule_mgr.assignments.get(d, [])
    }

    def _make_day_callback(day_index: int):
        async def _day_btn_callback(day_interaction: discord.Interaction):
            uid = str(day_interaction.user.id)
            schedule_mgr.toggle_self_deployment(day_index, uid)
            updated_view = _build_deploy_view(schedule_mgr, uid)
            await day_interaction.response.edit_message(view=updated_view)
            await _refresh_schedule_status(day_interaction)
        return _day_btn_callback

    # 배정 요일을 앞에, 미배정 요일을 뒤에 배치
    assigned_buttons = []
    extra_buttons = []

    for d in ACTIVE_DAYS:
        deployed = schedule_mgr.actual_deployments.get(d, [])
        is_self_deployed = user_id in deployed
        is_assigned = d in assigned_days

        if is_self_deployed:
            style = ButtonStyle.success
            label = f"{WEEKDAYS[d]} ✓"
        elif is_assigned:
            style = ButtonStyle.primary
            label = WEEKDAYS[d]
        else:
            style = ButtonStyle.secondary
            label = WEEKDAYS[d]

        btn = Button(label=label, style=style)
        btn.callback = _make_day_callback(d)

        if is_assigned:
            assigned_buttons.append(btn)
        else:
            extra_buttons.append(btn)

    # 현재 본인 투입 현황
    my_days = [
        WEEKDAYS[d] for d in ACTIVE_DAYS
        if user_id in schedule_mgr.actual_deployments.get(d, [])
    ]
    my_status = ', '.join(my_days) if my_days else "없음"

    # 배정 요일 안내
    if assigned_days:
        assigned_str = ', '.join(WEEKDAYS[d] for d in sorted(assigned_days))
        info_line = f"내 배정: **{assigned_str}** / 내 투입: **{my_status}**"
    else:
        info_line = f"배정된 요일이 없습니다. / 내 투입: **{my_status}**"

    deploy_view = LayoutView()
    deploy_view.add_item(Container(
        TextDisplay(
            content=f"## ✅ 투입 기록\n"
            f"{info_line}\n\n"
            f"투입한 요일을 선택해주세요. (다시 누르면 해제)"
        ),
        Separator(),
        TextDisplay(content=FOOTER_TEXT),
        accent_colour=Color.blue(),
    ))

    # ActionRow 당 최대 5개 제한: 배정 요일을 우선 배치한 뒤 분할
    all_buttons = assigned_buttons + extra_buttons
    deploy_view.add_item(ActionRow(*all_buttons[:5]))
    if len(all_buttons) > 5:
        deploy_view.add_item(ActionRow(*all_buttons[5:]))

    return deploy_view


async def _refresh_schedule_status(interaction: discord.Interaction) -> None:
    """일정 대시보드 메시지를 갱신합니다."""
    schedule_mgr = BotManager.get_instance().get_schedule_manager()
    guild = interaction.guild
    if not guild:
        return

    # 관리자 목록 수집
    from models.schedule_manager import EXCLUDED_USER_IDS
    all_admins: list[tuple[str, str]] = []
    for member in guild.members:
        if is_admin(member) and not member.bot and member.id not in EXCLUDED_USER_IDS:
            all_admins.append((str(member.id), member.display_name))

    status_text = schedule_mgr.get_status_text(all_admins)
    has_assignments = bool(schedule_mgr.assignments)
    new_view = ScheduleView(status_text, has_assignments=has_assignments)

    # 기존 대시보드 메시지 업데이트
    if schedule_mgr.status_message_id and schedule_mgr.status_channel_id:
        try:
            channel = guild.get_channel(schedule_mgr.status_channel_id)
            if channel:
                msg = await channel.fetch_message(schedule_mgr.status_message_id)
                await msg.edit(view=new_view, content=None, embed=None)
                return
        except (discord.NotFound, discord.HTTPException):
            pass

    # 대시보드 채널에 새 메시지 생성
    from commands.schedule import SCHEDULE_CHANNEL_ID
    channel = guild.get_channel(SCHEDULE_CHANNEL_ID)
    if channel:
        msg = await channel.send(view=new_view)
        schedule_mgr.status_message_id = msg.id
        schedule_mgr.status_channel_id = SCHEDULE_CHANNEL_ID
        schedule_mgr._save_backup()
