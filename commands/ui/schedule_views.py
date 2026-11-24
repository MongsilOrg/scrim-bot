from datetime import timedelta
from typing import Dict, List, Set

import discord
from discord import ButtonStyle, Color
from discord.components import CheckboxGroupOption
from discord.ui import (
    ActionRow, Button, CheckboxGroup, Container, Label, LayoutView,
    Modal, TextDisplay, TextInput,
)

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from models.schedule_manager import ACTIVE_DAYS, EXCLUDED_USER_IDS, WEEKDAYS
from services.holidays_api import get_rest_day_info
from utils import operator_log
from utils.layout_helpers import (
    check_cooldown,
    error_view, info_view, success_view,
    permission_error_view,
    send_response,
    upsert_persistent_message,
)
from utils.helpers import is_admin

logger = get_logger('schedule_views')

RESPOND_TEXT = "위 메시지에서 참가나 불참을 선택해주세요."


class AvailabilityModal(Modal):

    def __init__(self, current_days: set, days: List[int]):
        super().__init__(title="참가 등록")
        options = [
            CheckboxGroupOption(
                label=f"{WEEKDAYS[i]}요일",
                value=str(i),
                default=i in current_days,
            )
            for i in days
        ]
        self.days_checkbox = CheckboxGroup(
            options=options,
            min_values=1,
            max_values=len(options),
        )
        self.add_item(Label(text="참가 요일", component=self.days_checkbox))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            selected_days = {int(v) for v in self.days_checkbox.values}
            schedule_mgr = BotManager.get_instance().get_schedule_manager()
            schedule_mgr.register_schedule(
                str(interaction.user.id), interaction.user.display_name, selected_days,
            )
            day_str = ', '.join(WEEKDAYS[d] for d in sorted(selected_days))
            await send_response(
                interaction,
                success_view(f"{day_str} 참가로 등록했습니다.", title="참가 등록"),
            )
            await _refresh_schedule_status(interaction)
        except Exception as e:
            logger.error(f"[모달] 참가 등록 실패: {e}", exc_info=True)
            await send_response(interaction, error_view("참가 등록을 저장하지 못했습니다.\n참가를 다시 등록해주세요."))


class AbsenceReasonModal(Modal):

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
                success_view(f"이번 주 전체 불참으로 등록했습니다.\n사유: {reason}", title="불참 등록"),
            )
            await _refresh_schedule_status(interaction)
        except Exception as e:
            logger.error(f"[모달] 불참 등록 실패: {e}", exc_info=True)
            await send_response(interaction, error_view("불참 등록을 저장하지 못했습니다.\n불참을 다시 등록해주세요."))


async def _ensure_schedule_ready(interaction: discord.Interaction):
    """통과하면 일정 매니저, 통과하지 못하면 None."""
    if await check_cooldown(interaction):
        return None
    if not is_admin(interaction.user):
        await send_response(interaction, permission_error_view())
        return None

    schedule_mgr = BotManager.get_instance().get_schedule_manager()
    if not schedule_mgr.week_label:
        await send_response(interaction, error_view("이번 주 일정이 아직 열리지 않았습니다.\n봇 관리자에게 알려주세요."))
        return None
    return schedule_mgr


class ScheduleView(LayoutView):

    def __init__(self, status_text: str):
        super().__init__(timeout=None)
        self.add_item(Container(TextDisplay(content=status_text), accent_colour=Color.blue()))

        self.register_button = Button(label="참가", style=ButtonStyle.primary)
        self.register_button.callback = self.register_callback

        self.absence_button = Button(label="불참", style=ButtonStyle.secondary)
        self.absence_button.callback = self.absence_callback

        # 진행 기록은 조 확정 때 자동으로 쌓이고 고치는 곳은 Labs 운영 화면 하나
        record_button = Button(label="진행 기록", style=ButtonStyle.link, url=f"{settings.LABS_URL}/er/scrim/schedule")

        self.add_item(ActionRow(self.register_button, self.absence_button, record_button))

    async def register_callback(self, interaction: discord.Interaction) -> None:
        schedule_mgr = await _ensure_schedule_ready(interaction)
        if schedule_mgr is None:
            return

        days = [d for d in ACTIVE_DAYS if d not in schedule_mgr.rest_days]
        if not days:
            await send_response(
                interaction,
                info_view("이번 주는 모든 요일이 자율 진행일입니다.\n참가를 등록하지 않아도 됩니다.", title="참가 등록"),
            )
            return
        user_id = str(interaction.user.id)
        current_days = schedule_mgr.availability.get(user_id, set())

        await interaction.response.send_modal(AvailabilityModal(current_days, days))

    async def absence_callback(self, interaction: discord.Interaction) -> None:
        schedule_mgr = await _ensure_schedule_ready(interaction)
        if schedule_mgr is None:
            return

        user_id = str(interaction.user.id)
        reasons = schedule_mgr.absence_reasons.get(user_id, {})
        current_reason = reasons.get(-1, '') if reasons else ''

        await interaction.response.send_modal(AbsenceReasonModal(current_reason))


def schedule_members(guild: discord.Guild) -> list[discord.Member]:
    """일정에 응답해야 하는 운영진"""
    return [m for m in guild.members if is_admin(m) and not m.bot and m.id not in EXCLUDED_USER_IDS]


def reminder_view(user_ids: List[str]) -> LayoutView:
    return info_view(f"{' '.join(f'<@{uid}>' for uid in user_ids)}\n{RESPOND_TEXT}", title="일정 미응답")


async def delete_old_reminders(channel, schedule_mgr) -> None:
    schedule_mgr.reminder_shown = None
    if not schedule_mgr.reminder_message_ids:
        return
    failed = []
    for message_id in schedule_mgr.reminder_message_ids:
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.warning(f"[일정] 지난 미응답 알림 삭제 실패 - 메시지 ID: {message_id}: {e}")
            failed.append(message_id)
    schedule_mgr.reminder_message_ids = failed
    schedule_mgr.save_backup()


async def _update_reminders(channel, schedule_mgr, missing: List[str]) -> None:
    """미응답 알림에 아직 응답하지 않은 사람의 멘션만 남김"""
    if not schedule_mgr.reminder_message_ids or missing == schedule_mgr.reminder_shown:
        return
    for message_id in schedule_mgr.reminder_message_ids:
        try:
            await channel.get_partial_message(message_id).edit(
                view=reminder_view(missing), allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException as e:
            logger.warning(f"[일정] 미응답 알림 편집 실패 - 메시지 ID: {message_id}: {e}")
    schedule_mgr.reminder_shown = list(missing)


async def _rest_days(schedule_mgr) -> Set[int]:
    if not schedule_mgr.week_start:
        return set()
    rest = set()
    for day_index in ACTIVE_DAYS:
        day = (schedule_mgr.week_start + timedelta(days=day_index)).date()
        if (await get_rest_day_info(day))["is_rest_day"]:
            rest.add(day_index)
    return rest


def _day_groups(schedule_mgr) -> Dict[int, Dict[str, List[str]]]:
    if not schedule_mgr.week_start:
        return {}
    return {
        day_index: operator_log.deployed((schedule_mgr.week_start + timedelta(days=day_index)).date())
        for day_index in schedule_mgr.actual_deployments
    }


async def refresh_dashboard(
    guild: discord.Guild,
    channel=None,
    schedule_mgr=None,
) -> None:
    if schedule_mgr is None:
        schedule_mgr = BotManager.get_instance().get_schedule_manager()

    all_admins = [(str(m.id), m.display_name) for m in schedule_members(guild)]
    try:
        schedule_mgr.rest_days = await _rest_days(schedule_mgr)
    except Exception as e:
        # 판정이 깨지면 앞서 받은 값으로 메시지를 갱신
        logger.warning(f"[일정] 자율 진행일 조회 실패: {type(e).__name__}")

    # 멤버 목록을 받기 전에는 대상이 0명이라 모두 응답한 것으로 보지 않음
    reminder_channel = guild.get_channel(settings.SCHEDULE_CHANNEL_ID) if all_admins else None
    if reminder_channel:
        responded = schedule_mgr.get_responded_user_ids()
        missing = [uid for uid, _ in all_admins if uid not in responded]
        if missing and not schedule_mgr.all_rest:
            await _update_reminders(reminder_channel, schedule_mgr, missing)
        else:
            await delete_old_reminders(reminder_channel, schedule_mgr)

    view = ScheduleView(schedule_mgr.get_status_text(all_admins, _day_groups(schedule_mgr)))

    target_channel = channel
    if target_channel is None and schedule_mgr.status_channel_id:
        target_channel = guild.get_channel(schedule_mgr.status_channel_id)
    if target_channel is None:
        target_channel = guild.get_channel(settings.SCHEDULE_CHANNEL_ID)
    if target_channel is None:
        return

    new_id = await upsert_persistent_message(target_channel, schedule_mgr.status_message_id, view)
    if (new_id != schedule_mgr.status_message_id
            or schedule_mgr.status_channel_id != target_channel.id):
        schedule_mgr.status_message_id = new_id
        schedule_mgr.status_channel_id = target_channel.id
        schedule_mgr.save_backup()


async def _refresh_schedule_status(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if not guild:
        return
    await refresh_dashboard(guild)
