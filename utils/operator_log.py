"""
운영진이 조를 진행한 기록. 방 코드 입력, 서브 날씨 선택, 조 채널 CSV 업로드마다 한 줄씩 쌓음
"""
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import discord

from config.logging_config import get_logger
from utils.helpers import get_current_kst_time, is_admin

logger = get_logger('operator_log')

LOG_FILE = os.getenv('OPERATOR_LOG_PATH', 'data/operator_log.jsonl')
# 자정을 넘긴 경기도 시작한 날의 기록으로 셈
DAY_SHIFT = timedelta(hours=6)


def scrim_day(now: Optional[datetime] = None) -> date:
    return ((now or get_current_kst_time()) - DAY_SHIFT).date()


def record(member, group_letter: str, round_number: int, kind: str) -> None:
    if not isinstance(member, discord.Member) or not is_admin(member):
        return
    now = get_current_kst_time()
    entry = {
        'date': scrim_day(now).isoformat(),
        'at': now.isoformat(timespec='seconds'),
        'group': group_letter.upper(),
        'round': round_number,
        'kind': kind,
        'user_id': str(member.id),
    }
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError as e:
        logger.warning(f"[투입 기록] 저장 실패: {e}")


def deployed(day: date) -> Dict[str, List[str]]:
    """그날 조마다 라운드 기록이 하나라도 있는 운영진"""
    seen = set()
    target = day.isoformat()
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get('date') == target:
                    seen.add((entry.get('group'), entry.get('user_id')))
    except FileNotFoundError:
        return {}
    except OSError as e:
        logger.warning(f"[투입 기록] 읽기 실패: {e}")
        return {}
    result: Dict[str, List[str]] = defaultdict(list)
    for group, user_id in sorted(seen, key=str):
        if group and user_id:
            result[group].append(user_id)
    return dict(result)
