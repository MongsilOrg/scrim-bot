"""봇 이벤트 핸들러"""

import io

import discord
import pandas as pd
import pytz

from config.logging_config import get_logger
from services.score_image_generator import ScoreImageGenerator
from utils.helpers import get_current_kst_time

logger = get_logger('events')


async def on_message(message: discord.Message) -> None:
    """메시지 이벤트 핸들러 (CSV 업로드 시 점수 합산 이미지 생성)"""
    # 봇 메시지는 무시
    if message.author.bot:
        return

    # CSV 첨부가 없으면 패스
    has_csv = any(att.filename.lower().endswith('.csv') for att in message.attachments)
    if not has_csv:
        return

    try:
        await _process_csv_attachments(message)
    except Exception as e:
        logger.error(f"[이벤트] CSV 처리 실패: {e}", exc_info=True)


async def _process_csv_attachments(message: discord.Message) -> None:
    """오늘 업로드된 모든 CSV를 스캔해 점수를 합산하고 이미지를 전송합니다."""
    channel = message.channel
    kst_now = get_current_kst_time()
    start_of_day_kst = kst_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_of_day_kst.astimezone(pytz.utc)

    # CSV 파일별로 데이터와 gameId 저장
    csv_data_list = []  # [(gameId, df, attachment_filename), ...]
    
    async for msg in channel.history(after=start_utc, oldest_first=True, limit=None):
        for attachment in msg.attachments:
            if not attachment.filename.lower().endswith('.csv'):
                continue
            try:
                content = await attachment.read()
                df = pd.read_csv(io.BytesIO(content))
                # 컬럼 공백 제거
                df.columns = [c.strip() for c in df.columns]
                
                # 필수 컬럼 확인
                required_for_calc = ['teamName', 'tournament total score', 'tournament kill score', 'gameId']
                missing = [c for c in required_for_calc if c not in df.columns]
                if missing:
                    logger.warning(f"[이벤트] CSV 필수 컬럼 누락 - 파일: {attachment.filename}, 누락된 컬럼: {missing}")
                    continue
                
                # gameId 추출 (첫 번째 행의 gameId 사용, 모든 행이 같은 gameId를 가짐)
                game_id = None
                if 'gameId' in df.columns and len(df) > 0:
                    game_id_str = str(df.iloc[0]['gameId']).strip()
                    try:
                        game_id = int(game_id_str)
                    except (ValueError, TypeError):
                        logger.warning(f"[이벤트] gameId 파싱 실패 - 파일: {attachment.filename}, gameId: {game_id_str}")
                        continue
                
                if game_id is None:
                    logger.warning(f"[이벤트] gameId를 찾을 수 없음 - 파일: {attachment.filename}")
                    continue
                
                csv_data_list.append((game_id, df, attachment.filename))
            except Exception as e:
                logger.error(f"[이벤트] CSV 읽기 실패 - 파일: {attachment.filename}: {e}", exc_info=True)
                continue

    # CSV가 없으면 처리하지 않음
    if len(csv_data_list) == 0:
        return

    # gameId를 숫자 순으로 정렬 (낮은 순부터)
    csv_data_list.sort(key=lambda x: x[0])
    
    # 현재까지의 라운드 수
    current_round_count = len(csv_data_list)
    
    # 채널 정보로 조 추출
    from config.settings import settings
    group_letter = None
    for letter, channel_id in settings.GROUP_CHANNEL_IDS.items():
        if channel_id == channel.id:
            group_letter = letter
            break
    
    # 날짜 정보
    date_str = kst_now.strftime('%m월 %d일')
    group_info = f"{group_letter}조" if group_letter else "알 수 없음"
    
    # 현재까지의 모든 라운드 점수 누적 계산
    team_max_scores = {}  # {teamName: {'total_score': sum, 'kill_score': sum}}
    last_csv_df = None
    
    for round_num, (game_id, df, filename) in enumerate(csv_data_list, 1):
        # 수치 컬럼 안전 변환
        for num_col in ['tournament total score', 'tournament kill score']:
            df[num_col] = pd.to_numeric(df[num_col], errors='coerce').fillna(0)
        
        # 각 라운드에서 팀별 최대값만 추출
        round_team_max = (
            df
            .groupby('teamName', as_index=False)
            .agg({
                'tournament total score': 'max',
                'tournament kill score': 'max'
            })
        )
        
        # 누적 점수 계산
        for _, row in round_team_max.iterrows():
            team_name = str(row['teamName'])
            total_score = float(row['tournament total score'])
            kill_score = float(row['tournament kill score'])
            
            if team_name not in team_max_scores:
                team_max_scores[team_name] = {
                    'total_score': 0.0,
                    'kill_score': 0.0
                }
            
            team_max_scores[team_name]['total_score'] += total_score
            team_max_scores[team_name]['kill_score'] += kill_score
        
        last_csv_df = df

    if not team_max_scores:
        logger.warning("[이벤트] 처리할 팀 데이터 없음")
        return

    # 누적 팀별 점수를 리스트로 변환하고 정렬
    team_data = []
    for team_name, scores in team_max_scores.items():
        team_data.append({
            'teamName': team_name,
            'tournament total score': scores['total_score'],
            'tournament kill score': scores['kill_score'],
        })
    
    # 총 점수 → 킬 점수 내림차순 정렬
    team_data.sort(
        key=lambda x: (x['tournament total score'], x['tournament kill score']),
        reverse=True
    )
    
    # 순위 추가
    for idx, team in enumerate(team_data):
        team['rank'] = idx + 1

    # 누적 점수표 이미지 생성
    image = ScoreImageGenerator().generate_score_table_image(team_data)
    if not image:
        logger.error("[이벤트] 점수표 이미지 생성 실패", exc_info=True)
        return

    img_buf = io.BytesIO()
    image.save(img_buf, format='PNG')
    img_buf.seek(0)

    # 밴 리스트: 3회 이상 등장한 캐릭터 (마지막 라운드 기준)
    ban_list = []
    if last_csv_df is not None and 'character' in last_csv_df.columns:
        char_counts = (
            last_csv_df['character']
            .fillna('')
            .astype(str)
            .value_counts()
        )
        ban_candidates = char_counts[char_counts >= 3]
        ban_list = list(ban_candidates.index)
    
    # 누적 점수표 임베드 생성
    title = f"📊 Scrim Result - {current_round_count}R - {group_info} {date_str}"
    
    embed = discord.Embed(
        title=title,
        color=discord.Color.blue()
    )

    if ban_list:
        embed.add_field(
            name="🚫 밴 목록",
            value=" • " + "\n • ".join(ban_list),
            inline=False
        )

    file = discord.File(img_buf, filename='score_table.png')
    embed.set_image(url="attachment://score_table.png")

    # 조별 채널에 누적 점수표 전송
    await channel.send(embed=embed, file=file)
    
    # 4번째 CSV일 때만 gameId embed 전송
    if current_round_count == 4:
        gameid_info_lines = []
        for round_num, (game_id, df, filename) in enumerate(csv_data_list, 1):
            gameid_info_lines.append(f"**{round_num}R**: `{game_id}`")
        
        gameid_embed = discord.Embed(
            title=f"🎮 GameId 정보 - {group_info} {date_str}",
            description="\n".join(gameid_info_lines),
            color=discord.Color.blue()
        )
        
        # 조별 채널에 gameId embed 전송
        await channel.send(embed=gameid_embed)
        
        # 백업 채널에도 gameId embed 전송
        try:
            from bot.manager import BotManager
            client = BotManager.get_instance().get_client()
            if client:
                backup_channel = client.get_channel(settings.BACKUP_ANALYSIS_CHANNEL_ID)
                if backup_channel:
                    await backup_channel.send(embed=gameid_embed)
        except Exception as e:
            logger.error(f"[이벤트] 백업 채널 전송 실패: {e}", exc_info=True)
