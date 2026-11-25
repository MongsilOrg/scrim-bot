"""
권한 신청 버튼 메시지의 ID와 그날 받은 신청 기록
"""
import json
import os
from typing import Dict, Optional, Set

from config.logging_config import get_logger
from utils.helpers import get_current_kst_time, save_json_atomic

logger = get_logger('role_requests')

PATH = os.getenv('ROLE_REQUEST_PATH', 'data/role_requests.json')

PENDING, APPROVED, REJECTED = 'pending', 'approved', 'rejected'

_state: Optional[dict] = None


def _today() -> str:
    return get_current_kst_time().date().isoformat()


def _load() -> dict:
    global _state
    if _state is None:
        _state = {'panel_id': None, 'requests': {}}
        try:
            with open(PATH, encoding='utf-8') as f:
                saved = json.load(f)
            _state['panel_id'] = saved.get('panel_id')
            _state['requests'] = dict(saved.get('requests') or {})
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as e:
            logger.error(f"[권한신청] 기록을 읽지 못함: {e}")
    return _state


def _save() -> None:
    try:
        save_json_atomic(PATH, _load())
    except OSError as e:
        logger.error(f"[권한신청] 기록을 저장하지 못함: {e}")


def panel_id() -> Optional[int]:
    return _load()['panel_id']


def set_panel_id(message_id: Optional[int]) -> None:
    _load()['panel_id'] = message_id
    _save()


def add(message_id: int, request: Dict) -> None:
    state = _load()
    today = _today()
    state['requests'] = {key: row for key, row in state['requests'].items() if row.get('date') == today}
    state['requests'][str(message_id)] = request
    _save()


def get(message_id: int) -> Optional[Dict]:
    """조 역할은 22시에 초기화되므로 지난 날의 신청은 처리하지 않음"""
    request = _load()['requests'].get(str(message_id))
    return request if request and request.get('date') == _today() else None


def close(message_id: int, status: str, *, sanction: Optional[str]) -> Optional[Dict]:
    """대기 중인 신청만 닫음. 두 운영진이 함께 눌러도 제재가 한 번만 부여됨"""
    request = get(message_id)
    if not request or request.get('status') != PENDING:
        return None
    request['status'] = status
    request['sanction'] = sanction
    _save()
    return request


def granted_ids(letter: str) -> Set[int]:
    today = _today()
    return {
        member_id
        for row in _load()['requests'].values()
        if row.get('date') == today and row.get('status') == APPROVED and row.get('letter') == letter
        for member_id in row.get('members') or []
    }
