"""
권한 신청: 대타와 스태프의 조 역할을 버튼으로 신청받고, 운영진이 승인하면 역할과 대타 제재를 부여
"""
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import discord
from discord import ButtonStyle, SelectOption
from discord.ui import (
    ActionRow, Button, Container, DynamicItem, Label, LayoutView, Modal, Select, TextDisplay, TextInput, UserSelect,
)

from bot.manager import BotManager
from commands.team_pipeline import apply_roster_warnings
from config.logging_config import get_logger
from config.settings import settings
from models import role_requests
from models.role_requests import APPROVED, PENDING, REJECTED
from models.team_data import TeamData
from services import alerts
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
REASON_MAX = 100
LOG_KIND = "권한"

TITLES = {SUB: "대타", STAFF: "스태프 권한"}
STATUS_TEXT = {PENDING: "신청", APPROVED: "승인", REJECTED: "거절"}
STATUS_COLOR = {
    PENDING: discord.Color.blue(),
    APPROVED: discord.Color.green(),
    REJECTED: discord.Color.light_grey(),
}

NOT_ASSIGNED_TEXT = "조편성 뒤에 신청할 수 있습니다."
NOT_IN_TEAM_TEXT = "오늘 조에 편성된 팀의 선수와 스태프만 신청할 수 있습니다."
ROSTER_CHANGED_TEXT = "로스터가 바뀌었습니다.\n다시 신청해주세요."
CLOSED_TEXT = "이미 처리했거나 지난 신청입니다."

_decision_lock = asyncio.Lock()
_submit_lock = asyncio.Lock()
_panel_lock = asyncio.Lock()


def sanction_kind(now: datetime) -> str:
    start = now.replace(hour=settings.SCRIM_START_HOUR, minute=0, second=0, microsecond=0)
    return '주의' if now <= start - timedelta(minutes=LATE_MINUTES) else '경고'


def sanction_notice(now: datetime) -> str:
    """고른 선수가 제재를 받는다는 것을 신청자가 제출 전에 알게 함"""
    return f"선택한 선수는 {sanction_kind(now)}를 받습니다."


def _reason_input(*, required: bool) -> TextInput:
    return TextInput(required=required, max_length=REASON_MAX)


def _one_line(text: Optional[str]) -> str:
    return ' '.join((text or '').split())


def _plain(text: str) -> str:
    """사람이 적은 글이 카드의 서식이나 멘션으로 바뀌지 않게 함"""
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def find_team(manager, member: discord.Member) -> Optional[Tuple[str, str, TeamData]]:
    """반환: 조, 팀 이름, 팀. 조에 편성된 팀에 없으면 None."""
    name = manager.find_user_team(str(member.id), member=member)
    for index, group in enumerate(manager.groups):
        for team_name, team, _ in group:
            if team_name == name:
                return chr(ord('A') + index), team_name, team
    return None


def _applicant_team(interaction: discord.Interaction) -> Tuple[Optional[Tuple[str, str, TeamData]], str]:
    """반환: 신청자의 조와 팀, 신청할 수 없으면 그 이유."""
    manager = BotManager.get_instance().get_team_data_manager()
    if not manager.is_team_assignment_started or not manager.groups:
        return None, NOT_ASSIGNED_TEXT
    found = find_team(manager, interaction.user) if isinstance(interaction.user, discord.Member) else None
    return found, '' if found else NOT_IN_TEAM_TEXT


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
        if self.action == APPROVE:
            await _decide(interaction, APPROVE)
        else:
            await _ask_note(interaction, self.action)


def panel_view() -> LayoutView:
    view = LayoutView(timeout=None)
    view.add_item(Container(TextDisplay(content="## 권한 신청"), accent_colour=discord.Color.blue()))
    view.add_item(ActionRow(RequestButton(SUB), RequestButton(STAFF)))
    return view


def _facts(request: dict, text=str) -> List[Tuple[str, str]]:
    """카드와 Labs 기록에 함께 쓰는 줄. text는 사람이 적은 글에 적용"""
    kind, status = request['kind'], request['status']
    note = text(request.get('note') or '')
    facts = []
    if kind == SUB:
        facts.append(("출전하지 않는 선수", ', '.join(request['out'])))
    if request.get('reason'):
        facts.append(("사유", text(request['reason'])))
    if request['sanction']:
        facts.append(("제재", request['sanction']))
    elif kind == SUB and status == APPROVED:
        facts.append(("제재 없음", note))
    if status == REJECTED and note:
        facts.append(("거절 사유", note))
    return facts


def request_view(request: dict, *, operators: Optional[List[int]] = None) -> LayoutView:
    kind, status = request['kind'], request['status']
    lines = [
        f"## {TITLES[kind]} {STATUS_TEXT[status]}",
        f"**{request['letter']}조** `{request['team']}`",
        f"**{'대타' if kind == SUB else '스태프'}** {_mentions(request['members'])}",
    ]
    lines += [f"**{label}** {value}".rstrip() for label, value in _facts(request, _plain)]
    if status == PENDING and request.get('others'):
        lines.append("**다른 팀** " + ', '.join(
            f"<@{uid}> {letter}조 `{team}`" for uid, letter, team in request['others']
        ))
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

        # UserSelect는 required 기본값이 False라 비운 채 제출됨
        self.sub_select = UserSelect(min_values=1, max_values=2, required=True)
        self.add_item(Label(text="대타 선수", component=self.sub_select))

        # 선수 자리가 3개라 대타 1명이면 4인 로스터에서는 2명이 출전하지 않음
        least = len(self.players) - SLOTS + 1
        self.out_select = Select(
            options=[SelectOption(label=player, value=player) for player in self.players],
            min_values=least,
            max_values=least + 1,
        )
        now = get_current_kst_time()
        self.sanction = sanction_kind(now)
        self.add_item(Label(text="출전하지 않는 선수", description=sanction_notice(now), component=self.out_select))

        self.reason = _reason_input(required=False)
        self.add_item(Label(text="사유", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        subs = list(self.sub_select.values)
        out = [player for player in self.players if player in self.out_select.values]
        # 창을 열어 둔 사이 운영진이 로스터를 바꾸면 로스터에 없는 선수가 제재를 받음
        found, reason = _applicant_team(interaction)
        if found and (found[1] != self.team_name or found[2].players != self.players):
            reason = ROSTER_CHANGED_TEXT
        if reason:
            await send_response(interaction, error_view(reason))
            return
        blocked = await _blocked_reason(subs)
        if blocked:
            await send_response(interaction, error_view(blocked))
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
        if sanction_kind(now) != self.sanction:
            await send_response(interaction, error_view(
                f"시작 {LATE_MINUTES}분 전이 지나 선택한 선수가 경고를 받습니다.\n다시 신청해주세요."
            ))
            return

        manager = BotManager.get_instance().get_team_data_manager()
        others = []
        for user in subs:
            found = find_team(manager, user) if isinstance(user, discord.Member) else None
            if found and found[1] != self.team_name:
                others.append([user.id, found[0], found[1]])

        await _submit(interaction, {
            'kind': SUB,
            'date': now.date().isoformat(),
            'letter': self.letter,
            'team': self.team_name,
            'applicant': interaction.user.id,
            'members': [user.id for user in subs],
            'out': out,
            'others': others,
            'reason': _one_line(self.reason.value),
            'sanction': self.sanction,
            'status': PENDING,
        })


class StaffModal(Modal):
    def __init__(self, letter: str, team_name: str):
        super().__init__(title="스태프 권한 신청")
        self.letter = letter
        self.team_name = team_name

        self.staff_select = UserSelect(min_values=1, max_values=3, required=True)
        self.add_item(Label(text="스태프", component=self.staff_select))

        self.reason = _reason_input(required=False)
        self.add_item(Label(text="사유", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        staff = list(self.staff_select.values)
        found, reason = _applicant_team(interaction)
        if found and found[1] != self.team_name:
            reason = ROSTER_CHANGED_TEXT
        if reason:
            await send_response(interaction, error_view(reason))
            return
        blocked = await _blocked_reason(staff)
        if blocked:
            await send_response(interaction, error_view(blocked))
            return
        await _submit(interaction, {
            'kind': STAFF,
            'date': get_current_kst_time().date().isoformat(),
            'letter': self.letter,
            'team': self.team_name,
            'applicant': interaction.user.id,
            'members': [user.id for user in staff],
            'out': [],
            'reason': _one_line(self.reason.value),
            'sanction': None,
            'status': PENDING,
        })


class NoteModal(Modal):
    """제재를 빼거나 거절한 근거를 카드와 기록에 남김"""

    def __init__(self, action: str, kind: str):
        super().__init__(title="제재 없이 승인" if action == WAIVE else f"{TITLES[kind]} 거절")
        self.action = action

        self.note = _reason_input(required=True)
        self.add_item(Label(text="사유", component=self.note))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        note = _one_line(self.note.value)
        if not note:
            await send_response(interaction, error_view("사유를 입력해주세요."))
            return
        await _decide(interaction, self.action, note)


async def _blocked_reason(users: list) -> Optional[str]:
    """반환: 선택한 사람으로 신청할 수 없는 이유."""
    if any(user.bot for user in users):
        return "봇은 선택할 수 없습니다."
    manager = BotManager.get_instance().get_team_data_manager()
    restricted = await manager.find_restricted_members(
        get_current_kst_time(), [user.display_name for user in users],
    )
    if restricted:
        return "경고로 참가가 제한된 사람은 선택할 수 없습니다.\n" + "\n".join(
            f"**{name}**: {until}까지" for name, until in restricted
        )
    return None


async def _open(interaction: discord.Interaction, kind: str) -> None:
    found, reason = _applicant_team(interaction)
    if found is None:
        await send_response(interaction, error_view(reason))
        return
    letter, team_name, team = found
    modal = SubModal(letter, team_name, team.players) if kind == SUB else StaffModal(letter, team_name)
    await interaction.response.send_modal(modal)


async def _submit(interaction: discord.Interaction, request: dict) -> None:
    channel = interaction.channel
    try:
        # 신청 카드가 채널에 올라오므로 신청자에게 따로 답하지 않음. 앞 신청을 기다리다 응답 기한을 넘기지 않게 먼저 응답
        await interaction.response.defer()
    except discord.HTTPException as e:
        logger.warning(f"[권한신청] 신청 응답 실패: {e}")
        return
    # 같은 사람의 신청 두 건이 함께 올라와 둘 다 승인되면 제재가 두 번 부여됨
    async with _submit_lock:
        if role_requests.is_requested(request['team'], request['kind'], set(request['members'])):
            await send_response(interaction, error_view("이미 신청한 사람은 다시 선택할 수 없습니다."))
            return
        try:
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
    _record(interaction, request)
    logger.info(
        f"[권한신청] {TITLES[request['kind']]} 신청 - {request['letter']}조 {request['team']}, "
        f"신청: {interaction.user.display_name}, 대상: {request['members']}, 제외: {request['out']}"
    )
    await move_panel(channel)


def _record(interaction: discord.Interaction, request: dict) -> None:
    """신청과 처리 결과를 Labs 운영 화면의 기록에 남김"""
    guild = interaction.guild
    found = [guild.get_member(uid) for uid in request['members']] if guild else []
    names = [member.display_name for member in found if member]
    detail = [f"{TITLES[request['kind']]} {STATUS_TEXT[request['status']]}"]
    detail += [f"{label} {value}".rstrip() for label, value in _facts(request)]
    BotManager.get_instance().get_team_data_manager().log_action(
        LOG_KIND, interaction.user, request['team'],
        players=names if request['kind'] == SUB else (),
        staff=names if request['kind'] == STAFF else (),
        change='\n'.join(detail),
        applicant_id=None if request['status'] == PENDING else request['applicant'],
    )


def _is_operator(interaction: discord.Interaction) -> bool:
    member = interaction.user
    return interaction.guild is not None and isinstance(member, discord.Member) and is_admin(member)


def _pending(message_id: int) -> Optional[dict]:
    # 다음 스크림 신청이 열리면 조 역할이 초기화되므로 그 뒤에는 승인하지 않음
    if not BotManager.get_instance().get_team_data_manager().is_team_assignment_started:
        return None
    request = role_requests.get(message_id)
    return request if request and request.get('status') == PENDING else None


async def _ask_note(interaction: discord.Interaction, action: str) -> None:
    if not _is_operator(interaction):
        await send_response(interaction, permission_error_view())
        return
    request = _pending(interaction.message.id)
    if not request:
        await send_response(interaction, error_view(CLOSED_TEXT))
        return
    await interaction.response.send_modal(NoteModal(action, request['kind']))


async def _decide(interaction: discord.Interaction, action: str, note: str = '') -> None:
    member = interaction.user
    guild = interaction.guild
    if not _is_operator(interaction):
        await send_response(interaction, permission_error_view())
        return

    async with _decision_lock:
        pending = _pending(interaction.message.id)
        request = pending and role_requests.close(
            interaction.message.id,
            REJECTED if action == REJECT else APPROVED,
            sanction=pending['sanction'] if action == APPROVE else None,
            note=note,
        )
    if not request:
        await send_response(interaction, error_view(CLOSED_TEXT))
        return

    logger.info(
        f"[권한신청] {TITLES[request['kind']]} {STATUS_TEXT[request['status']]} - {request['letter']}조 "
        f"{request['team']}, 운영진: {member.display_name}, 제재: {request['sanction']}"
    )
    _record(interaction, request)
    await _update_card(interaction, request)
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
        view = info_view('\n'.join(notes), title=f"{TITLES[request['kind']]} 승인")
        if interaction.response.is_done():
            await send_response(interaction, view)
        else:
            # 카드 응답이 실패하면 후속 메시지도 보낼 수 없음
            await alerts.send(interaction.client, view)


async def _update_card(interaction: discord.Interaction, request: dict) -> None:
    view = request_view(request)
    try:
        await interaction.response.edit_message(view=view, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        # 이미 승인으로 기록했으므로 응답이 실패해도 역할과 제재는 이어서 처리
        logger.warning(f"[권한신청] 카드 응답 실패, 메시지를 직접 고침: {e}")
        try:
            await interaction.message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            logger.error(f"[권한신청] 카드 수정 실패: {e}")


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
