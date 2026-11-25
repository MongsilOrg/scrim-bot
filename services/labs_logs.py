"""팀 신청, 수정, 취소 기록을 Labs 운영 화면에 남김. 보내지 못한 기록은 파일에 두었다가 동기화 주기마다 다시 보냄."""
import asyncio
import json
import time
import uuid
from typing import Iterable, List, Optional

import discord

from config.logging_config import get_logger
from config.settings import settings
from services import labs_api
from utils.helpers import save_json_atomic

logger = get_logger('labs_logs')

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


def record(kind: str, user: discord.abc.User, team: str, *, players: Iterable[str] = (), staff: Iterable[str] = (),
           change: str = '', applicant_id: Optional[object] = None) -> None:
    if not labs_api.enabled():
        return
    # 형식이 다른 ID 하나로 Labs가 묶음 전체를 거절하면 대기열이 막힘
    applicant = str(applicant_id or '')
    if not (applicant.isascii() and applicant.isdigit() and len(applicant) <= 20):
        applicant = ''
    queue = _load()
    queue.append({
        'key': uuid.uuid4().hex,
        'at': int(time.time() * 1000),
        'kind': kind,
        'team': team,
        'userId': str(user.id),
        'userName': getattr(user, 'display_name', '') or '',
        'applicantId': applicant,
        'players': _names(players),
        'staff': _names(staff),
        'detail': change,
    })
    if len(queue) > QUEUE_MAX:
        logger.error(f"[기록] Labs에 보내지 못한 기록이 {QUEUE_MAX}건을 넘어 오래된 {len(queue) - QUEUE_MAX}건을 버림")
        del queue[:-QUEUE_MAX]
    _save()


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
