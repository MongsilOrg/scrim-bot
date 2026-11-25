"""팀 신청, 수정, 취소와 조별 게임 ID 기록을 Labs 운영 화면에 남김. 보내지 못한 기록은 파일에 두었다가 동기화 주기마다 다시 보냄."""
import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

import discord

from config.logging_config import get_logger
from config.settings import settings
from services import labs_api
from utils.helpers import save_json_atomic
from utils.validators import member_name_keys, normalize_nickname_for_comparison

logger = get_logger('labs_logs')
KST = timezone(timedelta(hours=9))

BATCH = 500
# Labs가 오래 닫혀 있어도 파일이 끝없이 커지지 않게 오래된 기록부터 버림
QUEUE_MAX = 5000
# Labs가 한 기록에 받는 이름 수
NAMES_MAX = 10

_queue: Optional[List[dict]] = None
_lock = asyncio.Lock()


def _load() -> List[dict]:
    global _queue
    if _queue is None:
        try:
            with open(settings.LABS_LOGS_PATH, encoding='utf-8') as f:
                data = json.load(f)
            _queue = [r for r in data if isinstance(r, dict) and r.get('key')] if isinstance(data, list) else []
        except (OSError, ValueError):
            _queue = []
    return _queue


def _save() -> None:
    try:
        save_json_atomic(settings.LABS_LOGS_PATH, _queue or [])
    except OSError as e:
        logger.error(f"[기록] 대기열 저장 실패: {e}")


def _names(names: Iterable[str]) -> List[str]:
    return [str(n) for n in names if str(n).strip()][:NAMES_MAX]


def _ids(guild, names: List[str]) -> dict:
    """이름을 조편성 기록처럼 서버 별명, 표시 이름, 사용자명으로 찾음. 같은 이름이 둘 이상이면 비움"""
    wanted: dict = {}
    for n in names:
        wanted.setdefault(normalize_nickname_for_comparison(n), []).append(n)
    found: dict = {}
    try:
        for member in getattr(guild, 'members', None) or []:
            for key in member_name_keys(member) & wanted.keys():
                found.setdefault(key, set()).add(str(member.id))
    except Exception as e:
        # ID를 못 찾아도 기록은 남김
        logger.warning(f"[기록] 디스코드 ID 찾기 실패: {e}")
        return {}
    return {n: next(iter(v)) for k, v in found.items() if len(v) == 1 for n in wanted[k]}


def record(kind: str, user: discord.abc.User, team: str, *, players: Iterable[str] = (), staff: Iterable[str] = (),
           change: str = '', applicant_id: Optional[object] = None) -> None:
    if not labs_api.enabled():
        return
    # 형식이 다른 ID 하나로 Labs가 묶음 전체를 거절하면 대기열이 막힘
    applicant = str(applicant_id or '')
    if not (applicant.isascii() and applicant.isdigit() and len(applicant) <= 20):
        applicant = ''
    players, staff = _names(players), _names(staff)
    _push({
        'key': uuid.uuid4().hex,
        'at': int(time.time() * 1000),
        'kind': kind,
        'team': team,
        'userId': str(user.id),
        'userName': getattr(user, 'display_name', '') or '',
        'applicantId': applicant,
        'players': players,
        'staff': staff,
        'detail': change,
        'ids': _ids(getattr(user, 'guild', None), players + staff),
    })


def record_games(code: str, day: str, group_letter: str, replays: list) -> None:
    """조의 라운드별 게임 ID를 남김. 다음 날 보내는 조도 그날 기록으로 찾게 지난 날이면 그날 23:59로 적음"""
    at = int(time.time() * 1000)
    end = int((datetime.fromisoformat(day).replace(tzinfo=KST) + timedelta(days=1)).timestamp() * 1000) - 60_000
    # 상태 저장에 실패해 다시 남겨도 Labs가 같은 key를 한 번만 넣음
    _push({
        'key': f'g:{code}:{group_letter}',
        'at': min(at, end),
        'kind': '게임 ID',
        'team': f'{group_letter}조',
        'detail': '\n'.join(f"{r['roundNo']}R {r['gameId']}" for r in replays),
    })


def _push(entry: dict) -> None:
    queue = _load()
    queue.append(entry)
    if len(queue) > QUEUE_MAX:
        logger.error(f"[기록] Labs에 보내지 못한 기록이 {QUEUE_MAX}건을 넘어 오래된 {len(queue) - QUEUE_MAX}건을 버림")
        # 게임 ID는 대회를 지우면 다른 사본이 없어 남김
        extra = len(queue) - QUEUE_MAX
        drop = {id(r) for r in queue if not r['key'].startswith('g:')}
        queue[:] = [r for r in queue if id(r) not in drop or (extra := extra - 1) < 0]
    _save()


def pending(prefix: str) -> bool:
    """key가 prefix로 시작하는 기록이 아직 대기열에 있는지"""
    return any(r['key'].startswith(prefix) for r in _load())


async def flush() -> bool:
    """대기열을 Labs에 보냄. 모두 보냈으면 True"""
    if not labs_api.enabled():
        return False
    async with _lock:
        queue = _load()
        while queue:
            batch = queue[:BATCH]
            data = await labs_api.request('POST', '/api/bot/scrim-logs', {'logs': batch})
            if not data or data.get('status') != 'ok':
                logger.warning(f"[기록] Labs에 보내지 못해 다음 주기에 다시 보냄 - 남은 기록 {len(queue)}건")
                return False
            sent = {r['key'] for r in batch}
            queue[:] = [r for r in queue if r['key'] not in sent]
            _save()
        return True
