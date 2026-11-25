"""
권한 신청: 대타와 스태프의 조 역할을 버튼으로 신청받고, 운영진이 승인하면 역할과 대타 제재를 부여
"""
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import discord
from discord import ButtonStyle, SelectOption
from discord.ui import (
    ActionRow, Button, Container, DynamicItem, Label, LayoutView, Modal, Select, TextDisplay, UserSelect,
)

from bot.manager import BotManager
from commands.team_pipeline import apply_roster_warnings
from config.logging_config import get_logger
from config.settings import settings
from models import role_requests
from models.role_requests import APPROVED, PENDING, REJECTED
from models.team_data import TeamData
from utils.helpers import get_current_kst_time, is_admin
from utils.layout_helpers import (
    error_view, info_view, permission_error_view, send_response, upsert_persistent_message,
)
from utils.validators import member_name_keys, normalize_nickname_for_comparison

logger = get_logger('role_request')

SUB, STAFF = 'sub', 'staff'
APPROVE, WAIVE, REJECT = 'approve', 'waive', 'reject'

SLOTS = 3
LATE_MINUTES = 30
LATE_REASON = "대타 - 스크림 시작 30분 전이 지난 뒤 신청"

TITLES = {SUB: "대타", STAFF: "스태프 권한"}
STATUS_TEXT = {PENDING: "신청", APPROVED: "승인", REJECTED: "거절"}
STATUS_COLOR = {
    PENDING: discord.Color.blue(),
    APPROVED: discord.Color.green(),
    REJECTED: discord.Color.light_grey(),
}

NOT_ASSIGNED_TEXT = "조편성 뒤에 신청할 수 있습니다."
NOT_IN_TEAM_TEXT = "오늘 조에 편성된 팀의 선수와 스태프만 신청할 수 있습니다."
CLOSED_TEXT = "이미 처리했거나 지난 신청입니다."

_decision_lock = asyncio.Lock()
_panel_lock = asyncio.Lock()


def sanction_kind(now: datetime) -> str:
    start = now.replace(hour=settings.SCRIM_START_HOUR, minute=0, second=0, microsecond=0)
    return '주의' if now <= start - timedelta(minutes=LATE_MINUTES) else '경고'


def find_team(manager, member: discord.Member) -> Optional[Tuple[str, str, TeamData]]:
    """반환: 조, 팀 이름, 팀. 조에 편성된 팀에 없으면 None."""
    name = manager.find_user_team(str(member.id), member=member)
    for index, group in enumerate(manager.groups):
        for team_name, team, _ in group:
            if team_name == name:
                return chr(ord('A') + index), team_name, team
    return None


def _operator_ids(letter: str) -> List[int]:
    """그 조를 선택한 운영진. 아직 선택 전이면 오늘 진행할 운영진 전원."""
    pick = BotManager.get_instance().get_schedule_manager().pick
    if not pick or pick.get('date') != get_current_kst_time().date().isoformat():
        return []
    chosen = pick['choices'].get(letter)
    return [int(chosen)] if chosen else [int(uid) for uid in pick['targets']]


def _mentions(user_ids: List[int]) -> str:
    return ' '.join(f"<@{uid}>" for uid in user_ids)


class RequestButton(DynamicItem[Button], template=r"scrim:role:(?P<kind>sub|staff)"):
    def __init__(self, kind: str):
        self.kind = kind
        label = "대타" if kind == SUB else "스태프"
        super().__init__(Button(label=label, style=ButtonStyle.primary, custom_id=f"scrim:role:{kind}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match) -> "RequestButton":
        return cls(match["kind"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await _open(interaction, self.kind)


class DecisionButton(DynamicItem[Button], template=r"scrim:role:(?P<action>approve|waive|reject)"):
    LABELS = {APPROVE: "승인", WAIVE: "제재 없이 승인", REJECT: "거절"}
    STYLES = {APPROVE: ButtonStyle.success, WAIVE: ButtonStyle.secondary, REJECT: ButtonStyle.danger}

    def __init__(self, action: str):
        self.action = action
        super().__init__(Button(
            label=self.LABELS[action], style=self.STYLES[action], custom_id=f"scrim:role:{action}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match) -> "DecisionButton":
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await _decide(interaction, self.action)


def panel_view() -> LayoutView:
    view = LayoutView(timeout=None)
    view.add_item(Container(TextDisplay(content="## 권한 신청"), accent_colour=discord.Color.blue()))
    view.add_item(ActionRow(RequestButton(SUB), RequestButton(STAFF)))
    return view


def request_view(request: dict, *, operators: Optional[List[int]] = None) -> LayoutView:
    kind, status = request['kind'], request['status']
    lines = [
        f"## {TITLES[kind]} {STATUS_TEXT[status]}",
        f"**{request['letter']}조** `{request['team']}`",
    ]
    if kind == SUB:
        lines.append(f"**대타** {_mentions(request['members'])}")
        lines.append(f"**출전하지 않는 선수** {', '.join(request['out'])}")
        if request['sanction']:
            lines.append(f"**제재** {request['sanction']}")
    else:
        lines.append(f"**스태프** {_mentions(request['members'])}")
    lines.append(f"**신청** <@{request['applicant']}>")

    children = [TextDisplay(content='\n'.join(lines))]
    if status == PENDING and operators:
        children.insert(0, TextDisplay(content=_mentions(operators)))

    view = LayoutView(timeout=None)
    view.add_item(Container(*children, accent_colour=STATUS_COLOR[status]))
    if status == PENDING:
        actions = [APPROVE, WAIVE, REJECT] if kind == SUB else [APPROVE, REJECT]
        view.add_item(ActionRow(*(DecisionButton(action) for action in actions)))
    return view


class SubModal(Modal):
    def __init__(self, letter: str, team_name: str, players: List[str]):
        super().__init__(title="대타 신청")
        self.letter = letter
        self.team_name = team_name
        self.players = list(players)

        self.sub_select = UserSelect(min_values=1, max_values=2)
        self.add_item(Label(text="대타 선수", component=self.sub_select))

        # 선수 자리가 3개라 대타 1명이면 4인 로스터에서는 2명이 출전하지 않음
        least = len(self.players) - SLOTS + 1
        self.out_select = Select(
            options=[SelectOption(label=player, value=player) for player in self.players],
            min_values=least,
            max_values=least + 1,
        )
        self.add_item(Label(text="출전하지 않는 선수", component=self.out_select))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        subs = list(self.sub_select.values)
        out = [player for player in self.players if player in self.out_select.values]
        if any(user.bot for user in subs):
            await send_response(interaction, error_view("봇은 대타로 선택할 수 없습니다."))
            return
        roster_keys = {normalize_nickname_for_comparison(player) for player in self.players}
        if any(isinstance(user, discord.Member) and member_name_keys(user) & roster_keys for user in subs):
            await send_response(interaction, error_view("이 팀의 로스터 선수는 대타로 선택할 수 없습니다."))
            return
        need = len(self.players) - SLOTS + len(subs)
        if len(out) != need:
            await send_response(interaction, error_view(
                f"대타가 {len(subs)}명이면 출전하지 않는 선수는 {need}명입니다.\n다시 신청해주세요."
            ))
            return

        now = get_current_kst_time()
        await _submit(interaction, {
            'kind': SUB,
            'date': now.date().isoformat(),
            'letter': self.letter,
            'team': self.team_name,
            'applicant': interaction.user.id,
            'members': [user.id for user in subs],
            'out': out,
            'sanction': sanction_kind(now),
            'status': PENDING,
        })


class StaffModal(Modal):
    def __init__(self, letter: str, team_name: str):
        super().__init__(title="스태프 권한 신청")
        self.letter = letter
        self.team_name = team_name

        self.staff_select = UserSelect(min_values=1, max_values=3)
        self.add_item(Label(text="스태프", component=self.staff_select))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        staff = list(self.staff_select.values)
        if any(user.bot for user in staff):
            await send_response(interaction, error_view("봇은 스태프로 선택할 수 없습니다."))
            return
        await _submit(interaction, {
            'kind': STAFF,
            'date': get_current_kst_time().date().isoformat(),
            'letter': self.letter,
            'team': self.team_name,
            'applicant': interaction.user.id,
            'members': [user.id for user in staff],
            'out': [],
            'sanction': None,
            'status': PENDING,
        })


async def _open(interaction: discord.Interaction, kind: str) -> None:
    manager = BotManager.get_instance().get_team_data_manager()
    if not manager.is_team_assignment_started or not manager.groups:
        await send_response(interaction, error_view(NOT_ASSIGNED_TEXT))
        return
    found = find_team(manager, interaction.user) if isinstance(interaction.user, discord.Member) else None
    if found is None:
        await send_response(interaction, error_view(NOT_IN_TEAM_TEXT))
        return
    letter, team_name, team = found
    modal = SubModal(letter, team_name, team.players) if kind == SUB else StaffModal(letter, team_name)
    await interaction.response.send_modal(modal)


async def _submit(interaction: discord.Interaction, request: dict) -> None:
    channel = interaction.channel
    try:
        # 신청 카드가 채널에 올라오므로 신청자에게 따로 답하지 않음
        await interaction.response.defer()
        operators = _operator_ids(request['letter'])
        message = await channel.send(
            view=request_view(request, operators=operators),
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=[discord.Object(id=uid) for uid in operators],
            ),
        )
    except discord.HTTPException as e:
        logger.error(f"[권한신청] 신청 게시 실패: {e}", exc_info=True)
        await send_response(interaction, error_view("신청을 올리지 못했습니다.\n다시 신청해주세요."))
        return
    role_requests.add(message.id, request)
    logger.info(
        f"[권한신청] {TITLES[request['kind']]} 신청 - {request['letter']}조 {request['team']}, "
        f"신청: {interaction.user.display_name}, 대상: {request['members']}, 제외: {request['out']}"
    )
    await move_panel(channel)


async def _decide(interaction: discord.Interaction, action: str) -> None:
    member = interaction.user
    guild = interaction.guild
    if guild is None or not isinstance(member, discord.Member) or not is_admin(member):
        await send_response(interaction, permission_error_view())
        return

    async with _decision_lock:
        # 다음 스크림 신청이 열리면 조 역할이 초기화되므로 그 뒤에는 승인하지 않음
        started = BotManager.get_instance().get_team_data_manager().is_team_assignment_started
        pending = role_requests.get(interaction.message.id) if started else None
        request = pending and role_requests.close(
            interaction.message.id,
            REJECTED if action == REJECT else APPROVED,
            sanction=pending['sanction'] if action == APPROVE else None,
        )
    if not request:
        await send_response(interaction, error_view(CLOSED_TEXT))
        return

    logger.info(
        f"[권한신청] {TITLES[request['kind']]} {STATUS_TEXT[request['status']]} - {request['letter']}조 "
        f"{request['team']}, 운영진: {member.display_name}, 제재: {request['sanction']}"
    )
    await interaction.response.edit_message(
        view=request_view(request), allowed_mentions=discord.AllowedMentions.none(),
    )
    if request['status'] == REJECTED:
        return

    notes = []
    failed = await _grant_roles(guild, request)
    if failed:
        notes.append(f"조 역할을 부여하지 못했습니다.\n직접 부여해주세요: {_mentions(failed)}")
    if request['sanction']:
        reason = "대타" if request['sanction'] == '주의' else LATE_REASON
        notes.append(await apply_roster_warnings(
            interaction, request['team'], request['out'], reason, kind=request['sanction'],
        ))
    if notes:
        await send_response(interaction, info_view('\n'.join(notes), title=f"{TITLES[request['kind']]} 승인"))


async def _grant_roles(guild: discord.Guild, request: dict) -> List[int]:
    """반환: 역할을 부여하지 못한 사용자 ID."""
    role = discord.utils.get(guild.roles, name=f"{request['letter']}조")
    failed = []
    for user_id in request['members']:
        target = guild.get_member(user_id)
        if role is None or target is None:
            failed.append(user_id)
            continue
        try:
            await target.add_roles(role, reason="권한 신청 승인")
        except discord.HTTPException as e:
            logger.error(f"[권한신청] 역할 부여 실패 - 대상: {user_id}, 오류: {e}")
            failed.append(user_id)
    return failed


async def move_panel(channel: discord.abc.Messageable) -> None:
    """신청 버튼이 채널 맨 아래에 남도록 새로 올리고 앞선 메시지를 지움"""
    async with _panel_lock:
        old_id = role_requests.panel_id()
        try:
            message = await channel.send(view=panel_view())
        except discord.HTTPException as e:
            logger.error(f"[권한신청] 신청 버튼 게시 실패: {e}")
            return
        role_requests.set_panel_id(message.id)
        if old_id:
            try:
                await channel.get_partial_message(old_id).delete()
            except discord.HTTPException as e:
                logger.warning(f"[권한신청] 앞선 신청 버튼 삭제 실패: {e}")


async def setup_role_request(client: discord.Client) -> None:
    channel = client.get_channel(settings.ROLE_REQUEST_CHANNEL_ID)
    if channel is None:
        logger.warning(f"[권한신청] 채널 없음: {settings.ROLE_REQUEST_CHANNEL_ID}")
        return
    async with _panel_lock:
        role_requests.set_panel_id(
            await upsert_persistent_message(channel, role_requests.panel_id(), panel_view())
        )
