"""봇 이벤트 핸들러"""

import io
from datetime import datetime
from typing import List, Optional, Tuple

import discord
import pandas as pd
import pytz

from config.logging_config import get_logger
from config.settings import settings
from services.score_image_generator import ScoreImageGenerator
from utils.helpers import get_current_kst_time

logger = get_logger('events')

CSVRow = Tuple[int, pd.DataFrame, str]
REQUIRED_SCORE_COLUMNS = ['teamName', 'tournament total score', 'tournament kill score', 'gameId']


async def on_message(message: discord.Message) -> None:
    """메시지 이벤트 핸들러 (CSV 업로드 시 점수 합산 이미지 생성)"""
    if not _should_process_message(message):
        return

    try:
        await _process_csv_attachments(message)
    except Exception as e:
        logger.error(f"[이벤트] CSV 처리 실패: {e}", exc_info=True)


def _should_process_message(message: discord.Message) -> bool:
    """메시지 이벤트 처리 대상인지 확인합니다."""
    return (not message.author.bot) and _has_csv_attachment(message)


def _has_csv_attachment(message: discord.Message) -> bool:
    """메시지에 CSV 첨부가 있는지 확인합니다."""
    return any(_is_csv_filename(att.filename) for att in message.attachments)


def _is_csv_filename(filename: str) -> bool:
    return filename.lower().endswith('.csv')


def _get_start_of_day_utc(now_kst: datetime) -> datetime:
    """KST 자정 기준 UTC 시간을 반환합니다."""
    start_of_day_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_of_day_kst.astimezone(pytz.utc)


def _get_group_letter(channel_id: int) -> Optional[str]:
    """채널 ID로 조 문자를 반환합니다."""
    for letter, mapped_channel_id in settings.GROUP_CHANNEL_IDS.items():
        if mapped_channel_id == channel_id:
            return letter
    return None


async def _process_csv_attachments(message: discord.Message) -> None:
    """오늘 업로드된 모든 CSV를 스캔해 점수를 합산하고 이미지를 전송합니다."""
    channel = message.channel
    now_kst = get_current_kst_time()
    start_utc = _get_start_of_day_utc(now_kst)

    csv_data_list = await _collect_today_csv_data(channel, start_utc)
    if not csv_data_list:
        return

    csv_data_list.sort(key=lambda x: x[0])
    current_round_count = len(csv_data_list)

    group_letter = _get_group_letter(channel.id)
    group_info = f"{group_letter}조" if group_letter else "알 수 없음"
    date_str = now_kst.strftime('%m월 %d일')

    team_data, last_csv_df = _aggregate_team_scores(csv_data_list)
    if not team_data:
        logger.warning("[이벤트] 처리할 팀 데이터 없음")
        return

    img_buf = ScoreImageGenerator().generate_score_table_image(team_data)
    if not img_buf:
        logger.error("[이벤트] 점수표 이미지 생성 실패", exc_info=True)
        return

    ban_list = _extract_ban_list(last_csv_df)
    if group_letter:
        from bot.manager import BotManager
        BotManager.get_instance().set_ban_list(group_letter, ban_list)

    score_embed = _build_score_embed(current_round_count, group_info, date_str)
    score_file = discord.File(img_buf, filename='score_table.png')
    score_embed.set_image(url="attachment://score_table.png")
    await channel.send(embed=score_embed, file=score_file)

    if current_round_count == 4:
        gameid_embed = _build_gameid_embed(csv_data_list, group_info, date_str)
        await channel.send(embed=gameid_embed)
        await _send_gameid_to_backup_channel(gameid_embed)


async def _collect_today_csv_data(channel, start_utc: datetime, limit: int = 200) -> List[CSVRow]:
    """해당 채널의 오늘 CSV 데이터 목록을 수집합니다."""
    csv_data_list: List[CSVRow] = []
    async for msg in channel.history(after=start_utc, oldest_first=True, limit=limit):
        for attachment in msg.attachments:
            if not _is_csv_filename(attachment.filename):
                continue
            parsed = await _read_and_parse_csv_attachment(attachment)
            if parsed is not None:
                csv_data_list.append(parsed)
    return csv_data_list


async def _read_and_parse_csv_attachment(attachment) -> Optional[CSVRow]:
    """CSV 첨부 파일을 읽어 (game_id, dataframe, filename) 형태로 반환합니다."""
    try:
        content = await attachment.read()
        df = pd.read_csv(io.BytesIO(content))
        df.columns = [c.strip() for c in df.columns]

        missing_cols = [col for col in REQUIRED_SCORE_COLUMNS if col not in df.columns]
        if missing_cols:
            logger.warning(f"[이벤트] CSV 필수 컬럼 누락 - 파일: {attachment.filename}, 누락된 컬럼: {missing_cols}")
            return None

        game_id = _extract_game_id(df, attachment.filename)
        if game_id is None:
            return None
        return game_id, df, attachment.filename
    except Exception as e:
        logger.error(f"[이벤트] CSV 읽기 실패 - 파일: {attachment.filename}: {e}", exc_info=True)
        return None


def _extract_game_id(df: pd.DataFrame, filename: str) -> Optional[int]:
    """CSV DataFrame에서 gameId를 파싱합니다."""
    if len(df) == 0:
        logger.warning(f"[이벤트] gameId를 찾을 수 없음 - 파일: {filename}")
        return None
    game_id_str = str(df.iloc[0]['gameId']).strip()
    try:
        return int(game_id_str)
    except (ValueError, TypeError):
        logger.warning(f"[이벤트] gameId 파싱 실패 - 파일: {filename}, gameId: {game_id_str}")
        return None


def _aggregate_team_scores(csv_data_list: List[CSVRow]) -> Tuple[List[dict], Optional[pd.DataFrame]]:
    """라운드별 CSV를 누적 집계해 팀 점수표 데이터로 변환합니다."""
    team_max_scores = {}
    last_csv_df: Optional[pd.DataFrame] = None

    for _, df, _ in csv_data_list:
        round_df = df.copy()
        for num_col in ['tournament total score', 'tournament kill score']:
            round_df[num_col] = pd.to_numeric(round_df[num_col], errors='coerce').fillna(0)

        round_team_max = round_df.groupby('teamName', as_index=False).agg({
            'tournament total score': 'max',
            'tournament kill score': 'max',
        })

        for _, row in round_team_max.iterrows():
            team_name = str(row['teamName'])
            total_score = float(row['tournament total score'])
            kill_score = float(row['tournament kill score'])

            if team_name not in team_max_scores:
                team_max_scores[team_name] = {'total_score': 0.0, 'kill_score': 0.0}
            team_max_scores[team_name]['total_score'] += total_score
            team_max_scores[team_name]['kill_score'] += kill_score

        last_csv_df = round_df

    team_data: List[dict] = []
    for team_name, scores in team_max_scores.items():
        team_data.append({
            'teamName': team_name,
            'tournament total score': scores['total_score'],
            'tournament kill score': scores['kill_score'],
        })

    team_data.sort(
        key=lambda x: (x['tournament total score'], x['tournament kill score']),
        reverse=True,
    )
    for idx, team in enumerate(team_data):
        team['rank'] = idx + 1

    return team_data, last_csv_df


def _extract_ban_list(last_csv_df: Optional[pd.DataFrame]) -> List[str]:
    """마지막 라운드 기준 밴 리스트를 추출합니다."""
    if last_csv_df is None or 'character' not in last_csv_df.columns:
        return []
    char_counts = last_csv_df['character'].fillna('').astype(str).value_counts()
    return list(char_counts[char_counts >= 3].index)


def _build_score_embed(current_round_count: int, group_info: str, date_str: str) -> discord.Embed:
    title = f"📊 스크림 결과 - {current_round_count}R - {group_info} {date_str}"
    return discord.Embed(title=title, color=discord.Color.blue())


def _build_gameid_embed(csv_data_list: List[CSVRow], group_info: str, date_str: str) -> discord.Embed:
    lines = [f"**{round_num}R**: `{game_id}`" for round_num, (game_id, _, _) in enumerate(csv_data_list, 1)]
    return discord.Embed(
        title=f"🎮 GameId 정보 - {group_info} {date_str}",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )


async def _send_gameid_to_backup_channel(gameid_embed: discord.Embed) -> None:
    """백업 채널로 gameId 임베드를 전송합니다."""
    try:
        from bot.manager import BotManager

        client = BotManager.get_instance().get_client()
        if not client:
            return
        backup_channel = client.get_channel(settings.BACKUP_ANALYSIS_CHANNEL_ID)
        if backup_channel:
            await backup_channel.send(embed=gameid_embed)
    except Exception as e:
        logger.error(f"[이벤트] 백업 채널 전송 실패: {e}", exc_info=True)
