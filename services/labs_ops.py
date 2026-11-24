"""Labs 운영 화면 연동. 운영진과 투입 기록을 보내고 시드팀, 테스트 계정, 제재 사본을 받음."""
import asyncio
import json
import uuid
from datetime import timedelta
from typing import Dict, List, Optional

import discord

from bot.manager import BotManager
from config.logging_config import get_logger
from config.settings import settings
from services import labs_api
from utils import operator_log
from utils.helpers import is_admin, save_json_atomic
from utils.layout_helpers import custom_view, success_view, warning_view

logger = get_logger('labs_ops')

# Labs는 5분 안에 받은 운영진만 운영 화면에 들여보냄
SYNC_SECONDS = 60
FAILURES_BEFORE_NOTICE = 5
DEPLOY_DAYS = 7
UNKNOWN_NAME = '알 수 없음'

_copy: Optional[dict] = None
_copy_loaded = False
_client: Optional[discord.Client] = None
_task: Optional[asyncio.Task] = None
_lock = asyncio.Lock()
_failures = 0
_notified = False


def copy() -> Optional[dict]:
    """마지막으로 받은 사본. 한 번도 받지 못했으면 None"""
    global _copy, _copy_loaded
    if not _copy_loaded:
        _copy_loaded = True
        try:
            with open(settings.LABS_OPS_PATH, encoding='utf-8') as f:
                data = json.load(f)
            _copy = data if isinstance(data, dict) else None
        except (OSError, ValueError):
            _copy = None
    return _copy


def _rows(key: str) -> Optional[List[dict]]:
    data = copy()
    rows = data.get(key) if data else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None


def seeds() -> Optional[List[dict]]:
    return _rows('seeds')


def tests() -> Optional[Dict[str, float]]:
    rows = _rows('tests')
    if rows is None:
        return None
    return {str(r.get('nickname')): float(r.get('mmr') or 0) for r in rows if r.get('nickname')}


def sanctions() -> Optional[List[dict]]:
    return _rows('sanctions')


def _deploys(guild: discord.Guild, history: Dict[str, List[str]]) -> Dict[str, List[dict]]:
    today = operator_log.scrim_day()
    result: Dict[str, List[dict]] = {}
    for back in range(DEPLOY_DAYS):
        day = today - timedelta(days=back)
        uids = history.get(day.isoformat(), [])
        groups = {uid: letter for letter, found in operator_log.deployed(day).items() for uid in found} if uids else {}
        rows = []
        for uid in uids:
            # 형식이 다른 값 하나로 Labs가 요청 전체를 거절하지 않게 걸러 보냄
            if not (uid.isascii() and uid.isdigit() and len(uid) <= 20):
                continue
            member = guild.get_member(int(uid))
            group = groups.get(uid, '')
            rows.append({
                'id': uid,
                'name': member.display_name if member else UNKNOWN_NAME,
                'group': group if len(group) == 1 and 'A' <= group <= 'Z' else '',
            })
        result[day.isoformat()] = rows
    return result


def _body(guild: discord.Guild) -> Optional[dict]:
    ids = labs_api.operator_ids(guild)
    if not ids:
        return None
    members = {uid: guild.get_member(int(uid)) for uid in ids}
    schedule = BotManager.get_instance().get_schedule_manager()
    body = {
        'ids': ids,
        'ops': [uid for uid, m in members.items() if m and is_admin(m)],
        'names': {uid: m.display_name for uid, m in members.items() if m},
        'deploys': _deploys(guild, schedule.deploy_history),
        # Labs 투입 화면의 시즌 합계가 봇의 순위와 같은 날부터 세도록 전달
        'since': schedule.count_since,
    }
    data = copy()
    if data and isinstance(data.get('rev'), int):
        body['rev'] = data['rev']
    return body


def changes_view(changes: List[dict]) -> discord.ui.LayoutView:
    lines = [f"**{c.get('by')}**: {c.get('text')}" for c in changes]
    return custom_view("운영툴 변경", "\n".join(lines), discord.Color.blue())


async def _post(client: discord.Client, view: discord.ui.LayoutView) -> bool:
    """다시 보내도 소용없는 실패는 True"""
    channel = client.get_channel(settings.LOG_CHANNEL_ID)
    if channel is None:
        logger.warning("[운영툴] 로그 채널을 찾지 못해 게시를 건너뜀")
        return True
    try:
        await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.NotFound) as e:
        logger.warning(f"[운영툴] 로그 채널 게시 실패: {type(e).__name__}")
    except discord.HTTPException as e:
        logger.warning(f"[운영툴] 로그 채널 게시 실패, 다음 주기에 다시 보냄: {type(e).__name__}")
        return False
    return True


async def _note(client: discord.Client, ok: bool) -> None:
    global _failures, _notified
    if ok:
        _failures = 0
        if _notified:
            _notified = False
            await _post(client, success_view("Labs 연결이 복구되었습니다."))
        return
    _failures += 1
    if _failures < FAILURES_BEFORE_NOTICE or _notified:
        return
    _notified = True
    text = "Labs에 연결하지 못하고 있습니다."
    if sanctions() is None:
        text += "\n등록한 팀이 제재를 받았는지 직접 확인해주세요."
    await _post(client, warning_view(text))


async def sync(client: Optional[discord.Client] = None) -> bool:
    """운영진과 투입 기록을 보내고 사본을 갱신. 웹에서 바뀐 내용은 로그 채널에 게시"""
    global _copy
    client = client or _client
    guild = client.get_guild(settings.GUILD_ID) if client else None
    # 구성원 목록을 다 받기 전에 보내면 운영진 일부가 빠진 목록이 전달됨
    if not labs_api.enabled() or guild is None or not guild.chunked:
        return False
    async with _lock:
        body = _body(guild)
        if body is None:
            return False
        data = await labs_api.request('PUT', '/api/bot/scrim-staff', body)
        if not data or not isinstance(data.get('rev'), int):
            await _note(client, False)
            return False
        await _note(client, True)

        changes = [c for c in data.get('changes') or [] if isinstance(c, dict)]
        posted = not changes or await _post(client, changes_view(changes))
        fresh = {key: data[key] for key in ('seeds', 'tests', 'sanctions') if isinstance(data.get(key), list)}
        previous = copy() or {}
        # 게시하지 못한 변경은 번호를 올리지 않아 다음 주기에 다시 받음
        rev = data['rev'] if posted else previous.get('rev')
        if not fresh and rev == previous.get('rev'):
            return True
        updated = {**previous, **fresh, 'rev': rev}
        try:
            save_json_atomic(settings.LABS_OPS_PATH, updated)
        except OSError as e:
            logger.error(f"[운영툴] 사본 저장 실패: {e}")
        _copy = updated
        return True


def start_sync(client: discord.Client) -> None:
    global _client, _task
    _client = client
    if not labs_api.enabled() or (_task and not _task.done()):
        return

    async def run() -> None:
        while True:
            try:
                await sync(client)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[운영툴] 동기화 실패: {e}", exc_info=True)
            await asyncio.sleep(SYNC_SECONDS)

    _task = asyncio.create_task(run())


def is_operator(member: discord.Member) -> bool:
    return is_admin(member) or any(role.name in settings.LABS_OPERATOR_ROLES for role in member.roles)


async def grant(*, target: str, target_id: str, kind: str, reason: str, admin: str,
                today: str, warning_day: str) -> Optional[dict]:
    """제재 기록과 기간 계산은 Labs 담당. 실패하면 None"""
    body = {
        # 응답을 못 받았을 때의 재전송용, 같은 key는 Labs가 한 번만 기록
        'key': uuid.uuid4().hex,
        'target': target,
        'targetId': target_id,
        'kind': kind,
        'reason': reason,
        'admin': admin,
        'today': today,
        'warningDay': warning_day,
    }
    for _ in range(2):
        result = await labs_api.request('POST', '/api/bot/scrim-sanctions', body)
        if result:
            # 방금 준 제재가 팀 등록 검사에 바로 반영되게 사본을 다시 받음
            await sync()
            return result
    return None


async def extend_masters(day: str) -> Optional[int]:
    """그날 걸려 있던 제한을 하루씩 늘림. 같은 날을 다시 보내도 Labs가 한 번만 늘림"""
    result = await labs_api.request('POST', '/api/bot/scrim-sanctions', {'masters': day})
    return int(result.get('extended') or 0) if result else None
