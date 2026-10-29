"""Labs 점수 계산 대회 연동. LABS_SCRIM_TOKEN이 없으면 모두 건너뜀."""
import asyncio
import json
import os
from datetime import date
from typing import Dict, List, Optional

import aiohttp
import discord

from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_current_kst_time
from utils.layout_helpers import custom_view, format_kr_date

logger = get_logger('labs_api')

# 어제 F7XV3V 규칙에 금지 실험체만 3-PICK-BAN
SCRIM_RULES = {
    'preset': 'custom',
    'scoreSource': 'table',
    'placementPoints': [10, 7, 5, 4, 3, 2, 1, 0],
    'killMultiplier': 1,
    'tiebreak': 'killScore',
    'deathPenalty': True,
    'subWeather': True,
    'bans': 'basic',
}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
# 마지막 CSV를 몽실봇이 Labs에 넣을 시간을 두고 다시 확인. 라운드 간격보다 긴 마지막 확인은 치른 라운드까지 보냄
BACKUP_CHECK_DELAYS = (20, 60, 180, 600, 3600)

_backup_tasks: Dict[str, asyncio.Task] = {}


def enabled() -> bool:
    return bool(settings.LABS_SCRIM_TOKEN)


def load_state() -> dict:
    try:
        with open(settings.LABS_STATE_PATH, encoding='utf-8') as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    path = settings.LABS_STATE_PATH
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


def linked_today(group_letter: str, today: Optional[date] = None) -> bool:
    """오늘 Labs 대회에 연결된 조. 이 조는 스크림봇이 점수와 금지 실험체를 계산하지 않음"""
    if not enabled():
        return False
    state = load_state()
    today = today or get_current_kst_time().date()
    return state.get('date') == today.isoformat() and group_letter.upper() in state.get('groups', [])


def operator_ids(guild: discord.Guild) -> List[str]:
    names = settings.LABS_OPERATOR_ROLES
    return sorted({str(m.id) for role in guild.roles if role.name in names for m in role.members if not m.bot})


async def _request(method: str, path: str, body: Optional[dict] = None) -> Optional[dict]:
    headers = {'x-bot-token': settings.LABS_SCRIM_TOKEN, 'User-Agent': 'agent-local'}
    url = f"{settings.LABS_URL.rstrip('/')}{path}"
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, headers=headers) as session:
            async with session.request(method, url, json=body) as resp:
                data = await resp.json(content_type=None) if resp.content_length != 0 else None
                if resp.status != 200:
                    logger.warning(f"[Labs] {method} {path} 응답 {resp.status} {data.get('status') if isinstance(data, dict) else ''}")
                    return None
                return data if isinstance(data, dict) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        logger.warning(f"[Labs] {method} {path} 실패: {type(e).__name__}")
        return None


async def create_scrim(guild: discord.Guild, letters: List[str], today: Optional[date] = None) -> Optional[dict]:
    """같은 날 다시 불러도 Labs가 같은 대회를 돌려줌. 실패하면 None"""
    today = today or get_current_kst_time().date()
    links = []
    for letter in letters:
        channel = guild.get_channel(settings.GROUP_CHANNEL_IDS[letter])
        links.append({
            'group': letter,
            'channelId': str(settings.GROUP_CHANNEL_IDS[letter]),
            'guild': guild.name,
            'channel': channel.name if channel else f"{letter}조",
        })
    body = {
        'owner': str(guild.owner_id),
        'operators': operator_ids(guild),
        'name': f"{format_kr_date(today)} 스크림",
        'preset': SCRIM_RULES['preset'],
        'plannedRounds': settings.TOTAL_ROUNDS,
        'rules': SCRIM_RULES,
        'links': links,
    }
    result = await _request('POST', '/api/bot/t', body)
    if not result or result.get('status') != 'ok' or not isinstance(result.get('code'), str):
        return None
    state = load_state()
    posted = state.get('posted', []) if state.get('code') == result['code'] else []
    save_state({'date': today.isoformat(), 'code': result['code'], 'groups': letters, 'posted': posted})
    return result


async def delete_previous(guild: discord.Guild, today: Optional[date] = None) -> None:
    """전날 스크림봇이 만든 대회를 지움. 게임 ID를 다 보내지 못한 조가 있으면 Labs 보관 기간에 맡김"""
    today = today or get_current_kst_time().date()
    state = load_state()
    code = state.get('code')
    if not code or state.get('deleted') or state.get('date') == today.isoformat():
        return
    if any(letter not in state.get('posted', []) for letter in state.get('groups', [])):
        logger.warning(f"[Labs] 게임 ID를 보내지 못한 조가 있어 {code} 대회를 남김")
        return
    result = await _request('DELETE', f"/api/bot/t/{code}", {'owner': str(guild.owner_id)})
    if not result:
        return
    state['deleted'] = True
    save_state(state)
    logger.info(f"[Labs] 전날 대회 {code} 삭제 - {result.get('status')}")


async def fetch_board(code: str, group_letter: str) -> Optional[dict]:
    return await _request('GET', f"/api/bot/t/{code}?group={group_letter}")


def backup_view(day: str, group_letter: str, replays: list) -> discord.ui.LayoutView:
    lines = [f"**{r['roundNo']}R**: `{r['gameId']}`" for r in replays]
    return custom_view(f"🎮 {format_kr_date(day)} {group_letter}조 게임 ID", "\n".join(lines), discord.Color.blue())


async def post_backup(client: discord.Client, group_letter: str, finished_only: bool = True) -> bool:
    """백업 분석 채널에 그 조의 게임 ID를 한 번만 보냄. finished_only면 계획한 라운드를 다 치른 뒤에만"""
    state = load_state()
    code = state.get('code')
    if not code or group_letter not in state.get('groups', []) or group_letter in state.get('posted', []):
        return False
    board = await fetch_board(code, group_letter)
    replays = [r for r in (board or {}).get('replays') or [] if isinstance(r, dict) and r.get('gameId')]
    if not board or not replays:
        return False
    if finished_only and not board.get('ended') and board.get('played', 0) < board.get('plannedRounds', settings.TOTAL_ROUNDS):
        return False
    channel = client.get_channel(settings.BACKUP_ANALYSIS_CHANNEL_ID)
    if not channel:
        logger.error(f"[Labs] 백업 분석 채널을 찾을 수 없음 - 채널 ID: {settings.BACKUP_ANALYSIS_CHANNEL_ID}")
        return False
    # 보드를 읽는 동안 다른 조가 기록했을 수 있어 다시 읽음. 전송 중 취소돼도 다시 보내지 않도록 먼저 기록
    state = load_state()
    if state.get('code') != code or group_letter in state.get('posted', []):
        return False
    state.setdefault('posted', []).append(group_letter)
    save_state(state)
    try:
        await channel.send(view=backup_view(state['date'], group_letter, replays))
    except Exception:
        state = load_state()
        if group_letter in state.get('posted', []):
            state['posted'].remove(group_letter)
            save_state(state)
        raise
    logger.info(f"[Labs] {group_letter}조 게임 ID {len(replays)}개 백업 채널 전송")
    return True


def schedule_backup(client: discord.Client, group_letter: str) -> None:
    """결과 CSV가 올라온 뒤 Labs에 반영되면 확인. 확인 중에 새 CSV가 오면 처음부터 다시 기다림"""
    task = _backup_tasks.get(group_letter)
    if task and not task.done():
        task.cancel()

    async def run() -> None:
        for i, delay in enumerate(BACKUP_CHECK_DELAYS):
            await asyncio.sleep(delay)
            try:
                if await post_backup(client, group_letter, finished_only=i < len(BACKUP_CHECK_DELAYS) - 1):
                    return
            except Exception as e:
                logger.error(f"[Labs] {group_letter}조 게임 ID 백업 실패: {e}", exc_info=True)

    _backup_tasks[group_letter] = asyncio.create_task(run())


def resume_backups(client: discord.Client) -> None:
    """재시작으로 사라진 확인 작업을 오늘 보내지 않은 조마다 다시 시작"""
    if not enabled():
        return
    state = load_state()
    if state.get('date') != get_current_kst_time().date().isoformat():
        return
    for letter in state.get('groups', []):
        if letter not in state.get('posted', []):
            schedule_backup(client, letter)


async def flush_backups(client: discord.Client) -> None:
    """라운드를 다 치르지 못해 남은 조는 다음 대회를 만들기 전에 치른 라운드까지 보냄"""
    state = load_state()
    for letter in state.get('groups', []):
        try:
            await post_backup(client, letter, finished_only=False)
        except Exception as e:
            logger.error(f"[Labs] {letter}조 남은 게임 ID 백업 실패: {e}", exc_info=True)
