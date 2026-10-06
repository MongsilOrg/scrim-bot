"""
날짜별 조편성 기록. 시즌 통계용으로 조편성마다 한 줄씩 쌓음

사람마다 이름, 서버 별명으로 찾은 디스코드 ID, 게임 계정 ID, 선수는 그때 MMR을 남김.
게임 계정 ID는 닉네임을 바꾸면 달라져 같은 사람 묶기는 디스코드 ID로 함
"""
import asyncio
import json
import os
from datetime import datetime

from config.logging_config import get_logger
from services.bser_api import BSERAPIClient
from utils.helpers import get_current_kst_time
from utils.validators import member_name_keys, normalize_nickname_for_comparison

logger = get_logger('scrim_history')

HISTORY_FILE = os.getenv('SCRIM_HISTORY_PATH', 'data/scrim_history.jsonl')
# 스태프는 캐시에 없어 API를 새로 부름. BSER 클라이언트에 동시 요청 제한이 없어 여기서 묶음
_lookups = asyncio.Semaphore(4)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


async def _person(api, members: dict, name: str, is_test, with_mmr: bool) -> dict:
    info = {'name': name, 'discord_id': members.get(normalize_nickname_for_comparison(name))}
    if is_test(name):
        info['test'] = True
        return info
    try:
        # 직전 MMR 갱신이 채운 캐시라 대부분 API를 다시 부르지 않음
        async with _lookups:
            uid = await api.get_user_uid(name)
            info['uid'] = uid
            if uid and with_mmr:
                info['mmr'] = await api.get_user_mmr(uid)
    except Exception as e:
        logger.warning(f"[조편성기록] 계정 조회 실패 - {name}: {e}")
    return info


async def _team(api, members: dict, is_test, entry, group, order) -> dict:
    name, data, mmr = entry
    players, staff = await asyncio.gather(
        asyncio.gather(*[_person(api, members, p, is_test, True) for p in data.players]),
        asyncio.gather(*[_person(api, members, s, is_test, False) for s in data.staff]),
    )
    return {
        'team': name,
        'group': group,
        'order': order,
        'mmr': round(float(mmr or 0), 2),
        'seed': bool(data.is_seed),
        'seed_name': data.seed_name,
        'registrant': data.user_id,
        'created_at': _iso(data.created_at),
        'updated_at': _iso(data.updated_at),
        'players': list(players),
        'staff': list(staff),
    }


def _last_row(date: str):
    try:
        with open(HISTORY_FILE, encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        row = json.loads(line)
        if row.get('date') == date:
            return row
    return None


async def record_assignment(groups, reserve, *, registered, guild=None, is_test=lambda name: False,
                            source: str = 'assignment', changed_team: str = None) -> None:
    """같은 날 다시 편성하거나 로스터를 바꾸면 줄이 하나 더 생기고, 통계는 그날 마지막 줄을 씀

    reserve가 None이면 로스터 변경이라 그날 앞 줄의 예비팀과 신청 수를 그대로 씀
    """
    now = get_current_kst_time()
    previous = _last_row(now.date().isoformat()) if reserve is None else None
    members = {}
    for member in (guild.members if guild else []):
        for key in member_name_keys(member):
            members.setdefault(key, str(member.id))
    try:
        async with BSERAPIClient() as api:
            letters = [chr(65 + i) for i in range(len(groups))]
            row = {
                'date': now.date().isoformat(),
                'at': now.isoformat(timespec='seconds'),
                'source': source,
                'registered': registered if previous is None else previous.get('registered'),
                'groups': [
                    list(await asyncio.gather(*[_team(api, members, is_test, t, letter, i + 1) for i, t in enumerate(group)]))
                    for letter, group in zip(letters, groups)
                ],
                'reserve': (previous or {}).get('reserve', []) if reserve is None else list(await asyncio.gather(
                    *[_team(api, members, is_test, t, None, i + 1) for i, t in enumerate(reserve)])),
            }
            if changed_team:
                row['changed_team'] = changed_team
        os.makedirs(os.path.dirname(HISTORY_FILE) or '.', exist_ok=True)
        with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        logger.info(f"[조편성기록] 저장 - {source}, 조 {len(groups)}개, 예비 {len(row['reserve'])}팀")
    except Exception as e:
        logger.error(f"[조편성기록] 저장 실패: {e}", exc_info=True)
