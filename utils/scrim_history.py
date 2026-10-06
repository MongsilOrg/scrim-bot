"""
날짜별 조편성 기록. 시즌 통계용으로 조편성마다 한 줄씩 쌓음
"""
import json
import os

from config.logging_config import get_logger
from utils.helpers import get_current_kst_time

logger = get_logger('scrim_history')

HISTORY_FILE = os.getenv('SCRIM_HISTORY_PATH', 'data/scrim_history.jsonl')


def _team(entry) -> dict:
    name, data, mmr = entry
    return {
        'team': name,
        'players': list(data.players),
        'staff': list(data.staff),
        'mmr': round(float(mmr or 0), 2),
        'seed': bool(data.is_seed),
    }


def record_assignment(groups, reserve) -> None:
    """같은 날 다시 편성하면 줄이 하나 더 생기고, 통계는 그날 마지막 줄을 씀"""
    now = get_current_kst_time()
    row = {
        'date': now.date().isoformat(),
        'at': now.isoformat(timespec='seconds'),
        'groups': [[_team(t) for t in group] for group in groups],
        'reserve': [_team(t) for t in reserve],
    }
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE) or '.', exist_ok=True)
        with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    except (OSError, TypeError, ValueError) as e:
        logger.error(f"[조편성기록] 저장 실패: {e}", exc_info=True)
