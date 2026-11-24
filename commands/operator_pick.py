"""
조편성 뒤 그날 투입 대상 운영진이 진행할 조를 고르는 메시지와 투입 기록 확정
"""
import asyncio
from datetime import date
from typing import List, Optional

import discord
from discord import ButtonStyle
from discord.ui import ActionRow, Button, Container, DynamicItem, LayoutView, Separator, TextDisplay

from bot.manager import BotManager
from commands.ui.schedule_views import refresh_dashboard
from config.logging_config import get_logger
from config.settings import settings
from models.schedule_manager import ACTIVE_DAYS, PICK_CLOSE_HOUR
from services import holidays_api, labs_api
from utils import operator_log
from utils.helpers import get_current_kst_time, is_admin
from utils.layout_helpers import FOOTER_TEXT, error_view, info_view, permission_error_view, send_response

logger = get_logger('operator_pick')

DECLINE = 'decline'
ROLE_REASON = "운영진 조 고르기"

_close_task: Optional[asyncio.Task] = None
_role_lock = asyncio.Lock()


def _mentions(user_ids: List[str]) -> str:
    return ' '.join(f"<@{uid}>" for uid in user_ids)


class PickButton(DynamicItem[Button], template=r"scrim:pick:(?P<key>[A-Z]|decline)"):
    def __init__(self, key: str, *, taken: bool = False):
        self.key = key
        if key == DECLINE:
            button = Button(label="오늘 불가", style=ButtonStyle.secondary, custom_id=f"scrim:pick:{key}")
        else:
            style = ButtonStyle.secondary if taken else ButtonStyle.primary
            button = Button(label=f"{key}조", style=style, custom_id=f"scrim:pick:{key}")
        super().__init__(button)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match) -> "PickButton":
        return cls(match["key"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle(interaction, self.key)


def _name(guild: discord.Guild, schedule_mgr, user_id: str) -> str:
    # 금요일 22시에 주차가 바뀌면 일정에 적힌 이름이 지워져 서버 별명으로 대신함
    member = guild.get_member(int(user_id))
    return schedule_mgr.admin_names.get(user_id) or (member.display_name if member else '알 수 없음')


def pick_view(guild: discord.Guild, schedule_mgr) -> LayoutView:
    pick = schedule_mgr.pick
    choices = pick['choices']
    lines = ["## 조 고르기"]
    for letter in pick['groups']:
        lines.append(f"**{letter}조** <@{choices[letter]}>" if letter in choices else f"**{letter}조**")
    waiting = [uid for uid in pick['targets'] if uid not in choices.values()]
    if waiting:
        lines.append(f"**대상** {_mentions(waiting)}")
    if pick['reserves']:
        names = ', '.join(_name(guild, schedule_mgr, uid) for uid in pick['reserves'])
        lines.append(f"**예비** {names}")
    if not pick['closed']:
        lines.append(f"-# {PICK_CLOSE_HOUR}시 마감")

    view = LayoutView(timeout=None)
    view.add_item(Container(
        TextDisplay(content='\n'.join(lines)),
        Separator(),
        TextDisplay(content=FOOTER_TEXT),
        accent_colour=discord.Color.blue(),
    ))
    buttons = [PickButton(letter, taken=letter in choices) for letter in pick['groups']] + [PickButton(DECLINE)]
    for i in range(0, len(buttons), 5):
        view.add_item(ActionRow(*buttons[i:i + 5]))
    return view


async def _sync_roles(guild: discord.Guild, schedule_mgr) -> None:
    """고른 조의 역할을 붙이고, 봇이 붙였던 역할 가운데 선택이 바뀐 것은 뺌"""
    # 버튼이 동시에 눌려도 한 번에 하나씩 맞춤. 겹치면 기다리는 사이 다른 실행이 적은 기록을 지움
    async with _role_lock:
        pick = schedule_mgr.pick
        desired = {uid: letter for letter, uid in pick['choices'].items()}
        granted = pick['granted']
        try:
            for uid, letter in list(granted.items()):
                if desired.get(uid) == letter:
                    continue
                member = guild.get_member(int(uid))
                role = discord.utils.get(guild.roles, name=f"{letter}조")
                if member and role and role in member.roles:
                    await member.remove_roles(role, reason=ROLE_REASON)
                granted.pop(uid, None)
            for uid, letter in desired.items():
                if granted.get(uid) == letter:
                    continue
                member = guild.get_member(int(uid))
                role = discord.utils.get(guild.roles, name=f"{letter}조")
                if member and role and role not in member.roles:
                    await member.add_roles(role, reason=ROLE_REASON)
                    granted[uid] = letter
        except discord.HTTPException as e:
            logger.warning(f"[조 고르기] 조 역할 변경 실패: {e}")
        schedule_mgr.save_backup()


async def _notify(channel, schedule_mgr, text: str, title: str) -> None:
    # 메시지를 고쳐도 멘션 알림은 가지 않아 새 메시지로 알림
    try:
        message = await channel.send(
            view=info_view(text, title=title),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except discord.HTTPException as e:
        logger.warning(f"[조 고르기] 알림 전송 실패: {e}")
        return
    schedule_mgr.pick['notice_ids'].append(message.id)
    schedule_mgr.save_backup()


async def _handle(interaction: discord.Interaction, key: str) -> None:
    member = interaction.user
    guild = interaction.guild
    if guild is None or not isinstance(member, discord.Member) or not is_admin(member):
        await send_response(interaction, permission_error_view())
        return
    schedule_mgr = BotManager.get_instance().get_schedule_manager()
    pick = schedule_mgr.pick
    if not pick or interaction.message is None or interaction.message.id != pick.get('message_id'):
        await send_response(interaction, error_view("지난 조 고르기입니다.\n가장 최근 메시지에서 골라주세요."))
        return

    uid = str(member.id)
    notice = None
    if key == DECLINE:
        result = schedule_mgr.decline(uid)
        if result['status'] == 'not_target':
            await send_response(interaction, error_view("오늘 투입 대상과 예비만 누를 수 있습니다."))
            return
        if result['assigned']:
            notice = f"<@{result['promoted']}>\n{result['assigned']}조에 배정됐습니다."
        elif result['promoted']:
            notice = f"<@{result['promoted']}>\n오늘 투입 대상이 됐습니다.\n위 메시지에서 조를 골라주세요."
        logger.info(f"[조 고르기] 오늘 불가 - {member.display_name}, 승격: {result['promoted']}")
    else:
        status = schedule_mgr.pick_group(uid, key)
        if status == 'taken':
            await send_response(interaction, error_view("다른 운영진이 먼저 고른 조입니다.\n빈 조를 골라주세요."))
            return
        if status == 'closed':
            await send_response(interaction, error_view("마감 뒤에 빠지려면 오늘 불가를 눌러주세요."))
            return
        if status == 'not_allowed':
            await send_response(interaction, error_view("오늘 투입 대상이 고른 뒤 남는 조를 골라주세요."))
            return
        logger.info(f"[조 고르기] {key}조 {'취소' if status == 'cancelled' else '선택'} - {member.display_name}")

    await interaction.response.edit_message(view=pick_view(guild, schedule_mgr))
    await _sync_roles(guild, schedule_mgr)
    if notice:
        await _notify(interaction.channel, schedule_mgr, notice, "조 고르기")


async def _delete_messages(channel, pick: Optional[dict]) -> None:
    if not pick:
        return
    for message_id in [pick.get('message_id'), *pick.get('notice_ids', [])]:
        if not message_id:
            continue
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.warning(f"[조 고르기] 지난 메시지 삭제 실패 - 메시지 ID: {message_id}: {e}")
    pick['message_id'] = None
    pick['notice_ids'] = []


async def refresh_count_since(schedule_mgr) -> None:
    start = await holidays_api.regular_season_start()
    since = start.date().isoformat() if start else None
    # 시즌 목록을 받지 못한 날에는 전에 받은 시작일을 그대로 씀
    if since and since != schedule_mgr.count_since:
        schedule_mgr.count_since = since
        schedule_mgr.save_backup()


def _confirm(schedule_mgr, day: date, letter: str, found: dict) -> bool:
    user_ids = found.get(letter, [])
    if not schedule_mgr.confirm_deployment(day, letter, user_ids):
        return False
    logger.info(f"[투입] {day.isoformat()} {letter}조 확정 - {', '.join(user_ids) or '기록 없음'}")
    return True


def confirm_pending(schedule_mgr) -> bool:
    """게임 ID 백업을 보내지 못해 확정하지 못한 지난 날의 조를 확정"""
    today = operator_log.scrim_day()
    pairs = []
    if schedule_mgr.pick:
        pairs.append((schedule_mgr.pick.get('date'), schedule_mgr.pick.get('groups')))
    state = labs_api.load_state()
    pairs.append((state.get('date'), state.get('groups')))
    changed = False
    for iso, letters in pairs:
        try:
            day = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        if day >= today or not letters:
            continue
        found = operator_log.deployed(day)
        for letter in letters:
            changed = _confirm(schedule_mgr, day, letter, found) or changed
    return changed


async def confirm_group(client: discord.Client, day_iso: str, letter: str) -> None:
    schedule_mgr = BotManager.get_instance().get_schedule_manager()
    day = date.fromisoformat(day_iso)
    if not _confirm(schedule_mgr, day, letter, operator_log.deployed(day)):
        return
    guild = client.get_guild(settings.GUILD_ID)
    if guild:
        await refresh_dashboard(guild, schedule_mgr=schedule_mgr)


async def post_pick(client: discord.Client, guild: discord.Guild, letters: List[str]) -> None:
    schedule_mgr = BotManager.get_instance().get_schedule_manager()
    channel = guild.get_channel(settings.SCHEDULE_CHANNEL_ID)
    if not channel:
        logger.error(f"[조 고르기] 채널을 찾을 수 없음 - 채널 ID: {settings.SCHEDULE_CHANNEL_ID}")
        return
    confirm_pending(schedule_mgr)
    await _delete_messages(channel, schedule_mgr.pick)
    day = operator_log.scrim_day()
    if not letters or day.weekday() not in ACTIVE_DAYS:
        schedule_mgr.save_backup()
        await refresh_dashboard(guild, schedule_mgr=schedule_mgr)
        return
    await refresh_count_since(schedule_mgr)
    schedule_mgr.open_pick(day, letters)
    await _sync_roles(guild, schedule_mgr)
    message = await channel.send(
        view=pick_view(guild, schedule_mgr),
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    schedule_mgr.pick['message_id'] = message.id
    schedule_mgr.save_backup()
    logger.info(f"[조 고르기] 게시 - 조: {', '.join(letters)}, 대상: {', '.join(schedule_mgr.pick['targets'])}")
    start_close_task(client)
    await refresh_dashboard(guild, schedule_mgr=schedule_mgr)


async def close_pick(client: discord.Client) -> None:
    schedule_mgr = BotManager.get_instance().get_schedule_manager()
    pick = schedule_mgr.pick
    if not pick or pick['closed'] or not pick.get('message_id'):
        return
    guild = client.get_guild(settings.GUILD_ID)
    channel = guild.get_channel(settings.SCHEDULE_CHANNEL_ID) if guild else None
    if not channel:
        return
    assigned = schedule_mgr.close_pick()
    await _sync_roles(guild, schedule_mgr)
    try:
        await channel.get_partial_message(pick['message_id']).edit(view=pick_view(guild, schedule_mgr))
    except discord.HTTPException as e:
        logger.warning(f"[조 고르기] 마감 표시 실패: {e}")
    if assigned:
        lines = [f"<@{uid}> {letter}조" for uid, letter in assigned]
        lines.append(f"{PICK_CLOSE_HOUR}시까지 고르지 않아 남은 조에 배정했습니다.")
        await _notify(channel, schedule_mgr, '\n'.join(lines), "조 배정")
    logger.info(f"[조 고르기] 마감 - 자동 배정 {len(assigned)}명")


async def _close_at_deadline(client: discord.Client) -> None:
    await client.wait_until_ready()
    pick = BotManager.get_instance().get_schedule_manager().pick
    now = get_current_kst_time()
    if not pick or pick['closed'] or pick.get('date') != operator_log.scrim_day(now).isoformat():
        return
    deadline = now.replace(hour=PICK_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    await asyncio.sleep(max(0.0, (deadline - now).total_seconds()))
    try:
        await close_pick(client)
    except Exception as e:
        logger.error(f"[조 고르기] 마감 처리 실패: {e}", exc_info=True)


def start_close_task(client: discord.Client) -> None:
    # on_ready는 재-IDENTIFY 때 재발화하고 같은 날 조편성도 다시 돌 수 있어 앞선 태스크를 취소
    global _close_task
    if _close_task is not None:
        _close_task.cancel()
    _close_task = asyncio.create_task(_close_at_deadline(client))
