import io
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from config.logging_config import get_logger
from utils.helpers import get_current_kst_time, get_start_of_day_utc
from utils.validators import normalize_nickname_for_comparison, normalize_team_name

logger = get_logger('score_aggregation')

CSVRow = Tuple[int, pd.DataFrame, str]

# CSV 컬럼명과 결과 dict 키, image_generator와 공유하는 계약
COL_TEAM_NAME = 'teamName'
COL_TOTAL_SCORE = 'tournament total score'
COL_KILL_SCORE = 'tournament kill score'
COL_GAME_ID = 'gameId'
KEY_RANK = 'rank'
REQUIRED_SCORE_COLUMNS = [COL_TEAM_NAME, COL_TOTAL_SCORE, COL_KILL_SCORE, COL_GAME_ID]
DEFAULT_TEAM_PATTERN = re.compile(r'^team\s*\d+$', re.IGNORECASE)


def is_csv_filename(filename: str) -> bool:
    return filename.lower().endswith('.csv')


async def collect_today_csv_data(channel, start_utc: datetime, limit: int = 200) -> List[CSVRow]:
    csv_data_list: List[CSVRow] = []
    async for msg in channel.history(after=start_utc, oldest_first=True, limit=limit):
        for attachment in msg.attachments:
            if not is_csv_filename(attachment.filename):
                continue
            parsed = await _read_and_parse_csv_attachment(attachment)
            if parsed is not None:
                csv_data_list.append(parsed)
    return csv_data_list


async def _read_and_parse_csv_attachment(attachment) -> Optional[CSVRow]:
    try:
        content = await attachment.read()
        df = pd.read_csv(io.BytesIO(content))
        df.columns = [c.strip() for c in df.columns]

        missing_cols = [col for col in REQUIRED_SCORE_COLUMNS if col not in df.columns]
        if missing_cols:
            logger.warning(f"[점수집계] CSV 필수 컬럼 누락 - 파일: {attachment.filename}, 누락된 컬럼: {missing_cols}")
            return None

        game_id = _extract_game_id(df, attachment.filename)
        if game_id is None:
            return None
        return game_id, df, attachment.filename
    except Exception as e:
        logger.error(f"[점수집계] CSV 읽기 실패 - 파일: {attachment.filename}: {e}", exc_info=True)
        return None


def _extract_game_id(df: pd.DataFrame, filename: str) -> Optional[int]:
    if len(df) == 0:
        logger.warning(f"[점수집계] gameId를 찾을 수 없음 - 파일: {filename}")
        return None
    game_id_str = str(df.iloc[0][COL_GAME_ID]).strip()
    try:
        return int(game_id_str)
    except (ValueError, TypeError):
        logger.warning(f"[점수집계] gameId 파싱 실패 - 파일: {filename}, gameId: {game_id_str}")
        return None


def _is_default_team_name(name: str) -> bool:
    return bool(DEFAULT_TEAM_PATTERN.match(name.strip()))


def _build_team_nickname_map(df: pd.DataFrame) -> dict:
    team_nicknames = {}
    if 'nickname' not in df.columns:
        return team_nicknames
    for _, row in df.iterrows():
        team = str(row[COL_TEAM_NAME]).strip()
        nick = str(row.get('nickname', '')).strip()
        if nick:
            team_nicknames.setdefault(team, set()).add(normalize_nickname_for_comparison(nick))
    return team_nicknames


def _resolve_default_team_names(current_df: pd.DataFrame, previous_rounds_nicknames: list) -> pd.DataFrame:
    if 'nickname' not in current_df.columns or not previous_rounds_nicknames:
        return current_df

    current_team_nicks = _build_team_nickname_map(current_df)

    for team_name, nicks in current_team_nicks.items():
        if not _is_default_team_name(team_name):
            continue

        best_match = None
        best_count = 0

        for prev_nick_map in previous_rounds_nicknames:
            for prev_team, prev_nicks in prev_nick_map.items():
                if _is_default_team_name(prev_team):
                    continue
                overlap = len(nicks & prev_nicks)
                if overlap >= 2 and overlap > best_count:
                    best_count = overlap
                    best_match = prev_team

        if best_match:
            current_df.loc[current_df[COL_TEAM_NAME].str.strip() == team_name, COL_TEAM_NAME] = best_match

    return current_df


def aggregate_team_scores(csv_data_list: List[CSVRow]) -> List[dict]:
    team_max_scores = {}
    display_names = {}
    previous_rounds_nicknames = []

    for _, df, _ in csv_data_list:
        round_df = df.copy()
        round_df[COL_TEAM_NAME] = round_df[COL_TEAM_NAME].astype(str).str.strip()

        round_df = _resolve_default_team_names(round_df, previous_rounds_nicknames)

        previous_rounds_nicknames.append(_build_team_nickname_map(round_df))

        for num_col in [COL_TOTAL_SCORE, COL_KILL_SCORE]:
            round_df[num_col] = pd.to_numeric(round_df[num_col], errors='coerce').fillna(0)

        round_team_max = round_df.groupby(COL_TEAM_NAME, as_index=False).agg({
            COL_TOTAL_SCORE: 'max',
            COL_KILL_SCORE: 'max',
        })

        for _, row in round_team_max.iterrows():
            team_name = str(row[COL_TEAM_NAME])
            normalized_key = normalize_team_name(team_name)
            total_score = float(row[COL_TOTAL_SCORE])
            kill_score = float(row[COL_KILL_SCORE])

            if normalized_key not in display_names:
                display_names[normalized_key] = team_name

            if normalized_key not in team_max_scores:
                team_max_scores[normalized_key] = {'total_score': 0.0, 'kill_score': 0.0}
            team_max_scores[normalized_key]['total_score'] += total_score
            team_max_scores[normalized_key]['kill_score'] += kill_score

    team_data: List[dict] = []
    for normalized_key, scores in team_max_scores.items():
        team_data.append({
            COL_TEAM_NAME: display_names[normalized_key],
            COL_TOTAL_SCORE: scores['total_score'],
            COL_KILL_SCORE: scores['kill_score'],
        })

    team_data.sort(
        key=lambda x: (x[COL_TOTAL_SCORE], x[COL_KILL_SCORE]),
        reverse=True,
    )
    for idx, team in enumerate(team_data):
        team[KEY_RANK] = idx + 1

    return team_data


async def compute_ban_list_for_channel(channel) -> List[str]:
    """저장된 상태에서 읽으면 전날 밴이 이월됨."""
    now_kst = get_current_kst_time()
    start_utc = get_start_of_day_utc(now_kst)
    csv_data_list = await collect_today_csv_data(channel, start_utc)
    if not csv_data_list:
        return []
    csv_data_list.sort(key=lambda x: x[0])
    last_csv_df = csv_data_list[-1][1]
    return _extract_ban_list(last_csv_df)


def _extract_ban_list(last_csv_df: Optional[pd.DataFrame]) -> List[str]:
    if last_csv_df is None or 'character' not in last_csv_df.columns:
        return []

    counts: Dict[str, int] = {}
    display_names: Dict[str, str] = {}
    for raw in last_csv_df['character'].fillna('').astype(str):
        name = raw.strip()
        if not name:
            continue
        key = normalize_team_name(name)
        if key not in display_names:
            display_names[key] = name
        counts[key] = counts.get(key, 0) + 1

    return [display_names[key] for key, count in counts.items() if count >= 3]
