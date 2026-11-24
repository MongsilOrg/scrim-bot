"""토너먼트 키 시트와 Labs 서버 키 탭 연동. 시트에서 바꾸는 칸은 사용자 칸의 이름뿐"""
import asyncio
import os
import re
import time
from typing import Dict, List, Optional
from urllib.parse import quote

from config.logging_config import get_logger
from config.settings import settings
from services import labs_api

logger = get_logger('key_sheet')

CHECK_SECONDS = 60
# Labs에서 할 일이 없어도 시트에 새로 넣은 키와 손으로 고친 이름을 받아 오는 주기
FULL_SYNC_SECONDS = 600
# 비울 칸이 이보다 많으면 Labs 응답이 잘못된 것으로 보고 원본 시트를 바꾸지 않는 상한
WIPE_MAX = 20
KEY_FORM = re.compile(r'^[A-Z0-9]{5}(-[A-Z0-9]{5}){2}$')
KEY_HEADER = '토너먼트 키'
NAME_HEADER = '사용자'
API = 'https://sheets.googleapis.com/v4/spreadsheets'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
FIELDS = 'sheets(properties(title,index),merges,data(startRow,rowData(values(formattedValue))))'
TIMEOUT = 20

_task: Optional[asyncio.Task] = None
_session = None
_last_full = 0.0


def enabled() -> bool:
    return bool(labs_api.enabled() and settings.GOOGLE_SHEETS_KEYS_SPREADSHEET_ID
                and os.path.exists(settings.GOOGLE_SHEETS_CREDENTIALS_PATH))


def _text(cells: List[dict], col: int) -> str:
    value = cells[col].get('formattedValue') if col < len(cells) else None
    return value if isinstance(value, str) else ''


def parse(sheet: dict) -> Optional[dict]:
    """첫 탭에서 헤더 행을 찾아 그 아래의 키 행만 고름. 날짜 구분 행처럼 키 형식이 아닌 행은 빠짐"""
    data = (sheet.get('data') or [{}])[0]
    offset = data.get('startRow', 0)
    grid = [row.get('values') or [] for row in data.get('rowData') or []]
    head = key_col = name_col = None
    for i, cells in enumerate(grid):
        labels = [_text(cells, c).strip() for c in range(len(cells))]
        if KEY_HEADER in labels and NAME_HEADER in labels:
            head, key_col, name_col = i, labels.index(KEY_HEADER), labels.index(NAME_HEADER)
            break
    if head is None:
        return None
    note_col = name_col + 1
    # 메모 열에서 세로로 합친 칸은 맨 위 칸의 글을 합친 행 모두에 적용
    merged: Dict[int, int] = {}
    for m in sheet.get('merges') or []:
        if m.get('startColumnIndex') == note_col:
            for i in range(m.get('startRowIndex', 0), m.get('endRowIndex', 0)):
                merged[i] = m.get('startRowIndex', 0)
    rows = []
    for i in range(head + 1, len(grid)):
        key = _text(grid[i], key_col).strip()
        if not KEY_FORM.match(key):
            continue
        top = merged.get(i, i) - offset
        note = _text(grid[top], note_col) if 0 <= top < len(grid) else ''
        rows.append({'pos': offset + i + 1, 'key': key, 'name': _text(grid[i], name_col), 'note': note})
    return {'title': (sheet.get('properties') or {}).get('title', ''), 'key_col': key_col, 'name_col': name_col, 'rows': rows}


def _a1(layout: dict, col: int, pos: int) -> str:
    letters = ''
    n = col + 1
    while n:
        n, rest = divmod(n - 1, 26)
        letters = chr(ord('A') + rest) + letters
    title = layout['title'].replace("'", "''")
    return f"'{title}'!{letters}{pos}"


def _open():
    global _session
    if _session is None:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(settings.GOOGLE_SHEETS_CREDENTIALS_PATH, scopes=SCOPES)
        _session = AuthorizedSession(creds)
    return _session


def _base() -> str:
    return f"{API}/{settings.GOOGLE_SHEETS_KEYS_SPREADSHEET_ID}"


def _read() -> Optional[dict]:
    resp = _open().get(_base(), params={'includeGridData': 'true', 'fields': FIELDS}, timeout=TIMEOUT)
    if resp.status_code != 200:
        logger.warning(f"[서버 키] 시트 읽기 응답 {resp.status_code}")
        return None
    sheets = resp.json().get('sheets') or []
    first = next((s for s in sheets if (s.get('properties') or {}).get('index', 0) == 0), None)
    layout = parse(first) if first else None
    if not layout or not layout['rows']:
        logger.error("[서버 키] 시트에서 헤더나 키 행을 찾지 못함")
        return None
    return layout


def _single(value_range: dict) -> str:
    values = value_range.get('values') or [[]]
    return str(values[0][0]) if values[0] else ''


def _set(layout: dict, row: dict, value: str) -> bool:
    """쓰기 직전에 그 행의 키와 사용자 칸을 다시 읽어, 읽은 뒤 행이 밀렸거나 칸이 바뀌었으면 쓰지 않음"""
    session = _open()
    key_cell = _a1(layout, layout['key_col'], row['pos'])
    name_cell = _a1(layout, layout['name_col'], row['pos'])
    resp = session.get(f"{_base()}/values:batchGet", params={'ranges': [key_cell, name_cell]}, timeout=TIMEOUT)
    if resp.status_code != 200:
        logger.warning(f"[서버 키] 칸 확인 응답 {resp.status_code}")
        return False
    ranges = resp.json().get('valueRanges') or []
    if len(ranges) != 2 or _single(ranges[0]).strip() != row['key'] or _single(ranges[1]) != row['name']:
        logger.warning("[서버 키] 읽은 뒤 시트가 바뀌어 쓰지 않음")
        return False
    # RAW라 =로 시작하는 이름도 수식이 되지 않음
    resp = session.put(f"{_base()}/values/{quote(name_cell, safe='')}", params={'valueInputOption': 'RAW'},
                       json={'values': [[value]]}, timeout=TIMEOUT)
    if resp.status_code != 200:
        logger.warning(f"[서버 키] 칸 쓰기 응답 {resp.status_code}")
        return False
    return True


def apply(layout: dict, result: dict) -> int:
    """Labs가 돌려준 칸 가운데 방금 읽은 시트와 키가 맞는 칸만 바꿈. 이름은 빈 칸에만 적음"""
    by_pos = {r['pos']: r for r in layout['rows']}
    wipes = []
    for task in result.get('wipe') or []:
        row = by_pos.get(task.get('pos'))
        if row and row['key'] == task.get('key') and row['name']:
            wipes.append(row)
    if len(wipes) > WIPE_MAX:
        logger.error(f"[서버 키] 비울 칸이 {len(wipes)}개라 시트를 바꾸지 않음")
        return 0
    done = 0
    for task in result.get('write') or []:
        row = by_pos.get(task.get('pos'))
        name = task.get('name')
        if not row or row['key'] != task.get('key') or row['name'].strip() or not isinstance(name, str) or not name.strip():
            continue
        if _set(layout, row, name):
            row['name'] = name
            done += 1
    for row in wipes:
        if _set(layout, row, ''):
            row['name'] = ''
            done += 1
    return done


async def sync(force: bool = False) -> bool:
    """Labs에 할 일이 있거나 force일 때만 시트를 읽음"""
    global _last_full
    state = await labs_api.request('GET', '/api/bot/scrim-keys')
    if state is None or not (force or state.get('pending')):
        return False
    layout = await asyncio.to_thread(_read)
    if not layout:
        return False
    result = await labs_api.request('PUT', '/api/bot/scrim-keys', {'rows': layout['rows']})
    if not result:
        return False
    _last_full = time.monotonic()
    if result.get('added') or result.get('changed'):
        logger.info(f"[서버 키] 시트 반영: 키 {result.get('keys')}개, 추가 {result.get('added')}개, 변경 {result.get('changed')}개")
    done = await asyncio.to_thread(apply, layout, result)
    if done:
        logger.info(f"[서버 키] 시트의 사용자 칸 {done}개를 바꿈")
        # 바꾼 칸을 Labs가 알아야 같은 칸을 다시 돌려주지 않음
        await labs_api.request('PUT', '/api/bot/scrim-keys', {'rows': layout['rows']})
    return True


def start() -> None:
    global _task
    if not enabled() or (_task and not _task.done()):
        return

    async def run() -> None:
        while True:
            try:
                await sync(force=time.monotonic() - _last_full >= FULL_SYNC_SECONDS or not _last_full)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 예외 문구에 시트 주소가 들어 있어 종류만 남김
                logger.error(f"[서버 키] 동기화 실패: {type(e).__name__}")
            await asyncio.sleep(CHECK_SECONDS)

    _task = asyncio.create_task(run())
