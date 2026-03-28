"""
팀 처리 모델

팀 MMR 조회, 조편성, 공지 전송, 음성채널 관리 등의 기능을 담당합니다.
BSER API를 통해 팀 MMR을 조회하고, 시드 데이터를 기반으로 조편성을 수행하며,
Discord 채널에 공지를 전송하고 음성채널 이름을 업데이트합니다.
"""
import asyncio
import heapq
import os
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple, Union

import discord
import gspread
import pandas as pd
from discord.ext import commands
from google.oauth2.service_account import Credentials

from commands.ui.layout_helpers import error_view, FOOTER_TEXT
from config.logging_config import get_logger
from config.settings import settings
from services.bser_api import BSERAPIClient
from utils.helpers import extract_players_only

from .team_data import TeamData

logger = get_logger('team_processor')


class TeamProcessor:
    """
    팀 처리 클래스
    
    팀 MMR 조회, 조편성, 공지 전송, 음성채널 관리 등의 핵심 기능을 담당합니다.
    BSER API를 통해 팀 MMR을 조회하고, 시드 데이터를 기반으로 조편성을 수행하며,
    Discord 채널에 공지를 전송하고 음성채널 이름을 업데이트합니다.
    
    Attributes:
        client: Discord 봇 클라이언트
        api_key: BSER API 키
        seeds_data: 시드 데이터
        group_image_cache: 조별 이미지 캐시 (LRU 패턴)
        max_cache_size: 최대 캐시 크기 (바이트)
        max_cache_items: 최대 캐시 항목 수
        current_cache_size: 현재 캐시 크기 (바이트)
    """
    
    def __init__(self, client: Optional[commands.Bot]):
        self.client = client
        self.api_key = settings.BSER_API_KEY
        if not self.api_key:
            raise ValueError("API 키가 설정되지 않았습니다.")
        
        self.seeds_data = None
        # 테스트 계정 데이터 (닉네임 -> MMR 매핑)
        self.test_accounts_data: Dict[str, float] = {}
        # 구글 시트 클라이언트
        self.gspread_client: Optional[gspread.Client] = None
        self.gspread_spreadsheet: Optional[gspread.Spreadsheet] = None
        # 조별 이미지 캐시 (LRU 패턴 적용, 크기 제한)
        self.group_image_cache: OrderedDict[str, bytes] = OrderedDict()
        self.max_cache_size = 50 * 1024 * 1024  # 50MB 제한
        self.max_cache_items = 10  # 최대 캐시 항목 수
        self.current_cache_size = 0
        
        # 구글 시트 클라이언트 초기화
        self._initialize_gspread_client()
        
        # 테스트 계정 데이터 로드 (동기적으로)
        self._load_test_accounts_data_sync()
    
    def update_client(self, client: Optional[commands.Bot]) -> None:
        """클라이언트를 업데이트합니다."""
        self.client = client
    
    def _cleanup_image_cache(self):
        """이미지 캐시 크기 제한 관리 (LRU 패턴)"""
        # 항목 수 제한 확인
        while len(self.group_image_cache) > self.max_cache_items:
            # 가장 오래된 항목 제거
            oldest_key, oldest_value = self.group_image_cache.popitem(last=False)
            # 대략적인 크기 추정 (BytesIO 객체)
            estimated_size = len(oldest_value) if isinstance(oldest_value, bytes) else 1024 * 1024
            self.current_cache_size -= estimated_size
        
        # 크기 제한 확인
        while self.current_cache_size > self.max_cache_size and self.group_image_cache:
            # 가장 오래된 항목 제거
            oldest_key, oldest_value = self.group_image_cache.popitem(last=False)
            estimated_size = len(oldest_value) if isinstance(oldest_value, bytes) else 1024 * 1024
            self.current_cache_size -= estimated_size
    
    def _generate_group_image(self, group_letter: str, group_teams: dict, group_mmr_averages: dict):
        """조별 이미지를 생성하고 캐시에 저장합니다 (LRU 패턴)."""
        try:
            # 캐시에서 먼저 확인 (LRU: 접근한 항목을 맨 뒤로 이동)
            if group_letter in self.group_image_cache:
                # 캐시 히트: 항목을 맨 뒤로 이동 (최근 사용)
                img_data = self.group_image_cache.pop(group_letter)
                self.group_image_cache[group_letter] = img_data
                # BytesIO로 변환하여 반환
                from io import BytesIO
                return BytesIO(img_data)
            
            # 캐시 미스: 새로 생성
            from services.image_generator import ImageGenerator
            img_io = ImageGenerator.generate_mmr_image(group_teams)
            
            if img_io:
                # BytesIO를 bytes로 변환하여 캐시에 저장
                img_data = img_io.getvalue()
                actual_size = len(img_data)
                
                # 캐시 크기 관리 (새 항목 추가 전)
                self._cleanup_image_cache()
                
                # 기존 항목이 있으면 제거 (크기 계산)
                if group_letter in self.group_image_cache:
                    old_data = self.group_image_cache.pop(group_letter)
                    old_size = len(old_data) if isinstance(old_data, bytes) else 1024 * 1024
                    self.current_cache_size -= old_size
                
                # 새 항목 추가 (맨 뒤에 추가 = 최근 사용)
                self.group_image_cache[group_letter] = img_data
                self.current_cache_size += actual_size
                
                # 캐시 크기 관리 (추가 후)
                self._cleanup_image_cache()
                
                # BytesIO로 변환하여 반환
                img_io.seek(0)
                return img_io
            
            return None
        except Exception as e:
            logger.error(f"[이미지생성] 조별 이미지 생성 실패 - 조: {group_letter}조: {e}", exc_info=True)
            return None
    
    def _initialize_gspread_client(self) -> None:
        """구글 시트 클라이언트 초기화"""
        try:
            credentials_path = settings.GOOGLE_SHEETS_CREDENTIALS_PATH
            
            if not credentials_path:
                logger.warning("[구글시트] 인증 정보 경로가 설정되지 않음")
                return
            
            # 파일 존재 확인
            if not os.path.exists(credentials_path):
                logger.warning(f"[구글시트] 인증 정보 파일을 찾을 수 없음 - 경로: {credentials_path}")
                return
            
            # 서비스 계정 인증
            scope = [
                'https://spreadsheets.google.com/feeds',
                'https://www.googleapis.com/auth/drive'
            ]
            creds = Credentials.from_service_account_file(
                credentials_path,
                scopes=scope
            )
            self.gspread_client = gspread.authorize(creds)
            
            # 스프레드시트 열기
            if settings.GOOGLE_SHEETS_MAIN_SPREADSHEET_ID:
                self.gspread_spreadsheet = self.gspread_client.open_by_key(
                    settings.GOOGLE_SHEETS_MAIN_SPREADSHEET_ID
                )
            else:
                logger.warning("[구글시트] 메인 스프레드시트 ID가 설정되지 않음")
                
        except Exception as e:
            logger.error(f"[구글시트] 클라이언트 초기화 실패: {e}", exc_info=True)
    
    async def _load_seeds_data(self) -> None:
        """구글 시트에서 시드 데이터를 로드합니다."""
        try:
            if not self.gspread_spreadsheet:
                # 클라이언트가 없으면 다시 초기화 시도
                self._initialize_gspread_client()
                if not self.gspread_spreadsheet:
                    logger.warning("[구글시트] 스프레드시트를 열 수 없음")
                    self.seeds_data = {"seeds": []}
                    return
            
            # 시드팀 시트 열기
            try:
                worksheet = self.gspread_spreadsheet.worksheet(settings.GOOGLE_SHEETS_SEEDS_WORKSHEET_NAME)
            except gspread.exceptions.WorksheetNotFound:
                logger.warning(f"[구글시트] 시드팀 시트를 찾을 수 없음 - 시트명: {settings.GOOGLE_SHEETS_SEEDS_WORKSHEET_NAME}")
                self.seeds_data = {"seeds": []}
                return
            
            # 모든 데이터 가져오기
            all_values = worksheet.get_all_records()
            
            # 모든 시드팀을 하나의 리스트로 처리 (토너먼트 타입 구분 없음)
            all_seeds = []
            
            for row in all_values:
                team_name = str(row.get('team_name', '')).strip()
                
                # 플레이어 목록 생성 (빈 값 제외)
                players = []
                for i in range(1, 5):  # player1 ~ player4
                    player_col = f'player{i}'
                    if player_col in row and row[player_col] and str(row[player_col]).strip():
                        players.append(str(row[player_col]).strip())
                
                if team_name and players:  # 팀명과 최소 1명의 플레이어가 있는 경우만 처리
                    team_data = {
                        "team_name": team_name,
                        "players": players
                    }
                    all_seeds.append(team_data)
            
            self.seeds_data = {
                "seeds": all_seeds
            }
            
            
        except Exception as e:
            logger.error(f"[조편성] 시드 데이터 로드 실패: {e}", exc_info=True)
            # 오류 발생 시 빈 데이터로 초기화
            self.seeds_data = {"seeds": []}
    
    def _load_test_accounts_data_sync(self) -> None:
        """구글 시트에서 테스트 계정 데이터를 동기적으로 로드합니다."""
        try:
            if not self.gspread_spreadsheet:
                # 클라이언트가 없으면 다시 초기화 시도
                self._initialize_gspread_client()
                if not self.gspread_spreadsheet:
                    logger.warning("[구글시트] 스프레드시트를 열 수 없음")
                    self.test_accounts_data = {}
                    return
            
            # 테스트 계정 시트 열기
            try:
                worksheet = self.gspread_spreadsheet.worksheet(settings.GOOGLE_SHEETS_TEST_ACCOUNTS_WORKSHEET_NAME)
            except gspread.exceptions.WorksheetNotFound:
                logger.warning(f"[구글시트] 테스트 계정 시트를 찾을 수 없음 - 시트명: {settings.GOOGLE_SHEETS_TEST_ACCOUNTS_WORKSHEET_NAME}")
                self.test_accounts_data = {}
                return
            
            # 모든 데이터 가져오기
            all_values = worksheet.get_all_records()
            
            # 테스트 계정 데이터 딕셔너리 초기화
            test_accounts = {}
            
            for row in all_values:
                nickname = str(row.get('nickname', '')).strip()
                mmr_str = str(row.get('mmr', '0')).strip()
                
                if nickname:
                    try:
                        mmr = float(mmr_str) if mmr_str else 0.0
                        test_accounts[nickname] = mmr
                    except (ValueError, TypeError):
                        logger.warning(f"[구글시트] 테스트 계정 MMR 파싱 실패 - 닉네임: {nickname}, MMR: {mmr_str}")
                        test_accounts[nickname] = 0.0
            
            self.test_accounts_data = test_accounts
            
        except Exception as e:
            logger.error(f"[구글시트] 테스트 계정 데이터 로드 실패: {e}", exc_info=True)
            # 오류 발생 시 빈 데이터로 초기화
            self.test_accounts_data = {}
    
    async def _load_test_accounts_data(self) -> None:
        """구글 시트에서 테스트 계정 데이터를 비동기적으로 로드합니다 (재로드용)."""
        self._load_test_accounts_data_sync()
    
    def _is_test_account(self, nickname: str) -> bool:
        """닉네임이 테스트 계정인지 확인합니다."""
        from utils.validators import normalize_nickname_for_comparison
        
        normalized_nickname = normalize_nickname_for_comparison(nickname)
        for test_nickname in self.test_accounts_data.keys():
            if normalize_nickname_for_comparison(test_nickname) == normalized_nickname:
                return True
        return False
    
    def _get_test_account_mmr(self, nickname: str) -> Optional[float]:
        """테스트 계정의 MMR을 반환합니다."""
        from utils.validators import normalize_nickname_for_comparison
        
        normalized_nickname = normalize_nickname_for_comparison(nickname)
        for test_nickname, mmr in self.test_accounts_data.items():
            if normalize_nickname_for_comparison(test_nickname) == normalized_nickname:
                return mmr
        return None
    
    def _calculate_test_team_mmr(self, players: List[str]) -> float:
        """테스트 계정 팀의 MMR을 계산합니다 (상위 3명 평균)."""
        mmr_list = []
        for player in players:
            if self._is_test_account(player):
                mmr = self._get_test_account_mmr(player)
                if mmr and mmr > 0:
                    mmr_list.append(mmr)
        
        if mmr_list:
            # 상위 3명의 MMR 추출
            top_3_mmr = heapq.nlargest(3, mmr_list)
            avg_mmr = sum(top_3_mmr) / len(top_3_mmr)
            return avg_mmr
        
        return 0.0
    
    def _extract_players_only(self, team_data: TeamData) -> List[str]:
        """팀 데이터에서 플레이어만 추출합니다 (스태프 제외)."""
        from utils.helpers import extract_players_only
        
        # MMR 조회를 위해 원본 닉네임 그대로 사용 (대소문자 구분)
        players = extract_players_only(team_data)
        # 공백만 제거하고 원본 대소문자 유지
        return [player.strip() for player in players if player and player.strip()]
    
    def _are_players_matching(self, players1: List[str], players2: List[str]) -> bool:
        """두 선수 리스트가 매칭되는지 확인합니다 (순서 무관, 스태프 제외).

        시드 적용 규칙:
        - 시드 데이터가 3명: 신청 팀도 정확히 3명이고 전원 일치해야 함
        - 시드 데이터가 4명: 신청 팀도 정확히 4명이고 전원 일치해야 함
        - 인원수가 다르거나 전원 일치하지 않으면 시드 미적용
        """
        if not players1 or not players2:
            return False

        from utils.helpers import normalize_player_list

        # 정규화된 플레이어 리스트 생성
        norm_players1 = set(normalize_player_list(players1))
        norm_players2 = set(normalize_player_list(players2))

        # 인원수가 다르면 시드 미적용
        if len(norm_players1) != len(norm_players2):
            return False

        # 교집합 계산
        common_players = norm_players1.intersection(norm_players2)

        # 매칭 조건: 인원수가 같고, 전원 일치해야 함
        if len(norm_players1) == 3:
            # 3명 팀: 3명 전부 일치해야 함
            return len(common_players) == 3
        elif len(norm_players1) == 4:
            # 4명 팀: 4명 전부 일치해야 함
            return len(common_players) == 4
        else:
            # 3명 또는 4명이 아닌 경우 시드 미적용
            return False
    
    async def _identify_seeded_teams(self, teams: Dict[str, TeamData]) -> Dict[str, int]:
        """시드팀을 식별하고 우선순위를 부여합니다.
        
        Returns:
            Dict[str, int]: 팀명을 키로 하고 우선순위(1, 2)를 값으로 하는 딕셔너리
            - 1순위: 시드팀 (시드 데이터에 있는 모든 팀)
            - 2순위: 시드가 없는 팀
        """
        team_priorities = {}
        
        if not self.seeds_data or not self.seeds_data.get("seeds"):
            # 시드 데이터가 없으면 모든 팀을 2순위로 설정
            for team_name in teams.keys():
                team_priorities[team_name] = 2
            return team_priorities
        
        # 모든 시드팀을 하나의 리스트로 처리
        all_seeds = self.seeds_data.get("seeds", [])
        
        # 모든 팀을 먼저 2순위로 초기화
        for team_name in teams.keys():
            team_priorities[team_name] = 2
        
        # 각 팀에 대해 시드 매칭 확인
        for team_name, team_data in teams.items():
            team_players = self._extract_players_only(team_data)
            
            # 시드팀 확인 (시드 데이터에 있는 팀을 1순위로 처리)
            for seed_team in all_seeds:
                seed_players = seed_team.get("players", [])
                if self._are_players_matching(team_players, seed_players):
                    team_priorities[team_name] = 1
                    break
        
        # 우선순위별 통계
        priority_1_count = sum(1 for priority in team_priorities.values() if priority == 1)
        priority_2_count = sum(1 for priority in team_priorities.values() if priority == 2)
        
        logger.info(f"[조편성] 시드팀 식별 완료 - 시드팀: {priority_1_count}개, 비시드팀: {priority_2_count}개")
        
        return team_priorities
    
    async def fetch_team_mmr(self, team_name: str, team_data: TeamData) -> Tuple[str, TeamData, float]:
        """팀의 MMR을 조회합니다."""
        try:
            players = self._extract_players_only(team_data)
            
            # 팀원 중 테스트 계정이 있는지 확인
            has_test_account = any(self._is_test_account(player) for player in players)
            
            # 테스트 계정만 있는 경우 구글시트에서 MMR 가져오기
            if has_test_account and all(self._is_test_account(player) for player in players):
                avg_mmr = self._calculate_test_team_mmr(players)
                
                # TeamData 객체에 MMR 저장 (dict인 경우 처리)
                if isinstance(team_data, dict):
                    team_data['mmr'] = avg_mmr
                else:
                    team_data.mmr = avg_mmr
                
                return team_name, team_data, avg_mmr
            
            # 일반 계정과 테스트 계정이 섞인 경우 또는 일반 계정만 있는 경우
            try:
                async with BSERAPIClient() as api_client:
                    # 플레이어별 MMR 조회를 병렬로 수행
                    async def _fetch_player_mmr(player: str) -> Optional[float]:
                        try:
                            if self._is_test_account(player):
                                mmr = self._get_test_account_mmr(player)
                                if mmr and mmr > 0:
                                    return mmr
                                logger.warning(f"[MMR조회] 테스트 계정 MMR 조회 실패 또는 0 - 플레이어: {player}, MMR: {mmr}")
                                return None
                            uid = await api_client.get_user_uid(player)
                            if not uid:
                                logger.warning(f"[MMR조회] 플레이어 UID 조회 실패 - 플레이어: {player}")
                                return None
                            mmr = await api_client.get_user_mmr(uid)
                            if mmr and mmr > 0:
                                return mmr
                            logger.warning(f"[MMR조회] 플레이어 MMR 조회 실패 또는 0 - 플레이어: {player}, UID: {uid}, MMR: {mmr}")
                            return None
                        except Exception as e:
                            logger.warning(f"[MMR조회] 플레이어 MMR 조회 실패 - 플레이어: {player}: {e}")
                            return None

                    results = await asyncio.gather(*[_fetch_player_mmr(p) for p in players])
                    mmr_list = [m for m in results if m is not None]

                    if mmr_list:
                        # 상위 3명의 MMR 추출
                        top_3_mmr = heapq.nlargest(3, mmr_list)
                        avg_mmr = sum(top_3_mmr) / len(top_3_mmr)
                    else:
                        logger.warning(f"[MMR조회] 팀의 모든 플레이어 MMR 조회 실패 - 팀명: {team_name}, 플레이어: {players}")
                        avg_mmr = 0.0

                    # TeamData 객체에 MMR 저장 (dict인 경우 처리)
                    if isinstance(team_data, dict):
                        team_data['mmr'] = avg_mmr
                    else:
                        team_data.mmr = avg_mmr

                    return team_name, team_data, avg_mmr
            except Exception as e:
                logger.error(f"[MMR조회] API 클라이언트 사용 실패: {e}", exc_info=True)
                if isinstance(team_data, dict):
                    team_data['mmr'] = 0.0
                else:
                    team_data.mmr = 0.0
                return team_name, team_data, 0.0
                
        except Exception as e:
            logger.error(f"[MMR조회] 팀 MMR 조회 실패: {e}", exc_info=True)
            if isinstance(team_data, dict):
                team_data['mmr'] = 0.0
            else:
                team_data.mmr = 0.0
            return team_name, team_data, 0.0
    
    async def process_teams_background(self, teams: Dict[str, TeamData], 
                                     channel: Union[discord.TextChannel, discord.Interaction, None]) -> None:
        """팀 처리를 백그라운드에서 실행합니다."""
        try:
            # 조편성 시작 시 MMR 캐시만 클리어 (실시간 데이터 보장)
            # 닉네임 캐시는 유지 (변경되지 않는 데이터)
            try:
                async with BSERAPIClient() as api_client:
                    api_client.clear_mmr_cache()
            except Exception as e:
                logger.warning(f"[캐시] MMR 캐시 클리어 중 오류: {e}")
            
            await self._load_seeds_data()
            
            # 시드 데이터 로드 결과 확인
            if not (self.seeds_data and self.seeds_data.get("seeds")):
                logger.warning("[조편성] 시드 데이터 로드 실패")
            
            # 시드팀 식별 및 우선순위 부여
            team_priorities = await self._identify_seeded_teams(teams)
            
            # 모든 팀의 MMR 조회
            team_info = await self._fetch_all_team_mmr(teams)
            
            # 팀 그룹 처리 (우선순위 시스템 적용)
            groups, unmatched_teams = await self._process_team_groups(team_info, team_priorities)
            
            logger.info(f"[조편성] 조편성 완료 - 조 수: {len(groups)}개, 매칭되지 않은 팀: {len(unmatched_teams)}개")
            
            # 조편성 결과 반환
            return groups, unmatched_teams
            
        except Exception as e:
            logger.error(f"[조편성] 팀 처리 실패: {e}", exc_info=True)
            raise
    
    async def _fetch_all_team_mmr(self, teams: Dict[str, TeamData]) -> List[Tuple[str, TeamData, float]]:
        """모든 팀의 MMR을 가져옵니다."""
        tasks = [
            self.fetch_team_mmr(team_name, team_data)
            for team_name, team_data in teams.items()
        ]

        team_info = await asyncio.gather(*tasks)
        team_info.sort(key=lambda x: x[2], reverse=True)

        return team_info
    
    async def _process_team_groups(self, team_info: List[Tuple[str, TeamData, float]], team_priorities: Dict[str, int] = None) -> Tuple[List[List], List]:
        """팀 그룹을 처리합니다. 우선순위 시스템을 적용합니다."""
        try:
            if team_priorities is None:
                team_priorities = {}
            
            # 1. 모든 팀을 MMR 순으로 정렬 (조편성은 MMR 기준)
            all_teams = sorted(team_info, key=lambda x: x[2], reverse=True)
            
            # 우선순위별 통계
            priority_1_count = sum(1 for team in all_teams if team_priorities.get(team[0], 2) == 1)
            priority_2_count = sum(1 for team in all_teams if team_priorities.get(team[0], 2) == 2)
            
            logger.info(f"[조편성] 우선순위 통계 - 시드팀: {priority_1_count}개, 비시드팀: {priority_2_count}개, 전체: {len(all_teams)}개")
            
            # 2. 8배수 제한 적용 시 우선순위에 따른 제외
            max_teams = (len(all_teams) // 8) * 8  # 8의 배수로 제한
            
            if len(all_teams) > max_teams:
                # 초과하는 팀 수 계산
                excess = len(all_teams) - max_teams
                
                # 우선순위별로 팀 분류
                priority_1_teams = [team for team in all_teams if team_priorities.get(team[0], 2) == 1]  # 시드팀
                priority_2_teams = [team for team in all_teams if team_priorities.get(team[0], 2) == 2]  # 비시드팀
                
                # 각 우선순위 그룹 내에서 MMR 순으로 정렬
                priority_1_teams.sort(key=lambda x: x[2], reverse=True)
                priority_2_teams.sort(key=lambda x: x[2], reverse=True)
                
                final_teams = []
                excluded_teams = []
                priority_2_selected = []
                
                # 시드팀부터 처리
                if len(priority_1_teams) <= max_teams:
                    # 시드팀이 8배수 이하면 모두 포함
                    final_teams.extend(priority_1_teams)
                    remaining_slots = max_teams - len(priority_1_teams)
                    
                    # 비시드팀 처리
                    if remaining_slots > 0 and priority_2_teams:
                        priority_2_selected = priority_2_teams[:remaining_slots]
                        final_teams.extend(priority_2_selected)
                    
                    # 제외된 팀들
                    excluded_teams.extend(priority_2_teams[len(priority_2_selected):])
                else:
                    # 시드팀이 8배수보다 많으면 시드팀 내에서 MMR 순으로 선별
                    final_teams = priority_1_teams[:max_teams]
                    excluded_teams.extend(priority_1_teams[max_teams:])  # 제외된 시드팀들
                    excluded_teams.extend(priority_2_teams)  # 모든 비시드팀 제외
                
                # MMR 순으로 다시 정렬 (조편성을 위해)
                final_teams.sort(key=lambda x: x[2], reverse=True)
                
                if excluded_teams:
                    excluded_team_names = [team[0] for team in excluded_teams]
                    logger.warning(f"[조편성] 팀 제외됨 - 제외된 팀 수: {len(excluded_team_names)}개, 팀명: {', '.join(excluded_team_names[:10])}{'...' if len(excluded_team_names) > 10 else ''}")
            else:
                final_teams = all_teams
            
            # 팀을 그룹으로 분배
            groups, unmatched_teams = self._distribute_teams_to_groups(final_teams, team_priorities)
            
            # 스네이크 드래프트 적용
            if len(groups) >= 2:
                groups = self._apply_snake_draft(groups)
            
            return groups, unmatched_teams
        except Exception as e:
            logger.error(f"[조편성] 팀 그룹 처리 실패: {e}", exc_info=True)
            raise
    
    def _distribute_teams_to_groups(self, sorted_teams: List[Tuple[str, TeamData, float]], team_priorities: Dict[str, int] = None) -> Tuple[List[List], List[Tuple[str, TeamData, float]]]:
        """팀을 그룹으로 분배합니다. 우선순위 정보를 로깅에 활용합니다."""
        groups = []
        unmatched_teams = []
        
        if team_priorities is None:
            team_priorities = {}
        
        # 8팀씩 그룹 생성
        for i in range(0, len(sorted_teams), settings.TEAMS_PER_GROUP):
            group = sorted_teams[i:i + settings.TEAMS_PER_GROUP]
            if len(group) == settings.TEAMS_PER_GROUP:
                groups.append(group)
                
                # 그룹 생성 완료
            else:
                unmatched_teams.extend(group)
        
        return groups, unmatched_teams
    
    def _apply_snake_draft(self, groups: List[List[Tuple[str, TeamData, float]]]) -> List[List[Tuple[str, TeamData, float]]]:
        """스네이크 드래프트를 적용합니다."""
        num_groups = len(groups)
        
        if num_groups == 1:
            # 1개 그룹: MMR 순서대로 배정 (스네이크 없음)
            return groups
        
        # 모든 팀을 하나의 리스트로 합치고 MMR 순으로 정렬
        all_teams = []
        for group in groups:
            all_teams.extend(group)
        all_teams.sort(key=lambda x: x[2], reverse=True)
        
        new_groups = [[] for _ in range(num_groups)]
        
        if num_groups % 2 == 0:
            # 짝수 그룹: 2/2/2... 패턴 (2개씩 묶어서 스네이크)
            self._apply_grouped_snake_pattern(all_teams, new_groups, num_groups)
        else:
            # 홀수 그룹: 2/2/2.../1 패턴 (마지막 1개 그룹만 MMR 순)
            self._apply_grouped_snake_pattern(all_teams, new_groups, num_groups)
        
        return new_groups
    
    def _apply_grouped_snake_pattern(self, teams: List[Tuple[str, TeamData, float]], groups: List[List], num_groups: int) -> None:
        """2개씩 묶어서 스네이크 드래프트 패턴을 적용합니다."""
        team_idx = 0
        
        # 2개씩 묶어서 처리
        for group_pair in range(0, num_groups, 2):
            if group_pair + 1 < num_groups:
                # 2개 그룹 쌍: 스네이크 드래프트
                pair_teams = teams[team_idx:team_idx + 16]  # 16팀 (2그룹 × 8팀)
                self._apply_snake_pattern(pair_teams, groups[group_pair:group_pair + 2], 2)
                team_idx += 16
            else:
                # 마지막 1개 그룹 (홀수인 경우): MMR 순
                remaining_teams = teams[team_idx:]
                groups[group_pair] = remaining_teams
    
    def _apply_snake_pattern(self, teams: List[Tuple[str, TeamData, float]], groups: List[List], num_groups: int) -> None:
        """2개 그룹에 스네이크 드래프트 패턴을 적용합니다."""
        for i, team in enumerate(teams):
            # 스네이크 패턴: 1조는 0,3,4,7 / 2조는 1,2,5,6
            if i in [0, 3, 4, 7, 8, 11, 12, 15]:  # 1조
                groups[0].append(team)
            else:  # 2조
                groups[1].append(team)
    
    
    async def _send_global_announcement(self, guild: discord.Guild, groups: List[List], unmatched_teams: List[Tuple[str, TeamData, float]] = None) -> None:
        """전체 공지를 하나의 LayoutView로 전송합니다."""
        try:
            from discord.ui import Container, LayoutView, MediaGallery, Separator, TextDisplay

            notice_channel = guild.get_channel(settings.NOTICE_CHANNEL_ID)
            if not notice_channel:
                logger.warning("[Discord] 전체 공지 채널을 찾을 수 없음")
                return

            from utils.helpers import get_current_kst_time
            date_str = get_current_kst_time().strftime('%m.%d')

            # LayoutView 구성: 헤더 + 조별 이미지들
            view = LayoutView()
            view.add_item(Container(
                TextDisplay(content=f"## 📢 {date_str} 20시 스크림 조편성입니다"),
                accent_colour=discord.Color.green(),
            ))

            files = []
            for group_index, group in enumerate(groups):
                if not group:
                    continue

                group_letter = chr(65 + group_index)
                group_teams = {}
                group_mmr_averages = {}
                for team_name, team_data, team_mmr in group:
                    group_teams[team_name] = team_data
                    group_mmr_averages[team_name] = team_mmr

                img_io = self._generate_group_image(group_letter, group_teams, group_mmr_averages)
                filename = f"{group_letter}조_mmr_table.png"

                children = [TextDisplay(content=f"### {group_letter}조")]
                if img_io:
                    children.append(MediaGallery(discord.MediaGalleryItem(media=f"attachment://{filename}")))
                    files.append(discord.File(img_io, filename=filename))
                else:
                    logger.warning(f"[Discord] 전체 공지 이미지 생성 실패 - {group_letter}조")

                view.add_item(Container(*children, accent_colour=discord.Color.blue()))

            # 푸터
            view.add_item(Container(
                Separator(),
                TextDisplay(content=FOOTER_TEXT),
            ))

            await notice_channel.send(view=view, files=files if files else None)

        except Exception as e:
            logger.error(f"[Discord] 전체 공지 전송 실패: {e}", exc_info=True)
    
    async def _send_group_announcement_with_image(self, channel: discord.TextChannel, message: str, group: List[Tuple[str, TeamData, float]]) -> None:
        """조별 MMR 이미지와 함께 공지를 전송합니다."""
        try:
            # 채널 ID로부터 조 이름 추출
            group_letter = None
            for letter, channel_id in settings.GROUP_CHANNEL_IDS.items():
                if channel_id == channel.id:
                    group_letter = letter
                    break
            
            if not group_letter:
                logger.warning(f"[Discord] 채널에 해당하는 조를 찾을 수 없음 - 채널 ID: {channel.id}")
                group_letter = "A"  # 기본값
            
            # 캐시된 이미지가 있는지 확인 (LRU: 접근한 항목을 맨 뒤로 이동)
            img_io = None
            if group_letter in self.group_image_cache:
                # 캐시 히트: 항목을 맨 뒤로 이동 (최근 사용)
                img_data = self.group_image_cache.pop(group_letter)
                self.group_image_cache[group_letter] = img_data
                # BytesIO로 변환
                from io import BytesIO
                img_io = BytesIO(img_data)
            else:
                # 캐시에 없으면 새로 생성
                group_teams = {}
                group_mmr_averages = {}
                
                for team_name, team_data, team_mmr in group:
                    group_teams[team_name] = team_data
                    group_mmr_averages[team_name] = team_mmr
                
                img_io = self._generate_group_image(group_letter, group_teams, group_mmr_averages)
            
            # 조별 역할 멘션 추가
            role_mention = await self._get_group_role_mention(channel.guild, group_letter)
            full_message = role_mention + "\n" + message if role_mention else message

            # 조별 로스터 관리 뷰 생성 (공지 텍스트 + 이미지 + 버튼 포함)
            from commands.ui.views import GroupRosterView
            roster_view = GroupRosterView(
                group_letter, group,
                message_text=full_message, has_image=bool(img_io),
            )

            if img_io:
                sent_message = await channel.send(
                    view=roster_view,
                    file=discord.File(img_io, filename='group_mmr_table.png'),
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            else:
                sent_message = await channel.send(
                    view=roster_view,
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
                logger.warning(f"[Discord] 이미지 생성 실패 - 채널: {channel.name}, 메시지만 전송")

            # message_id와 텍스트를 TeamDataManager에 저장
            from bot.manager import BotManager
            team_data_manager = BotManager.get_instance().get_team_data_manager()
            team_data_manager.group_message_ids[group_letter] = sent_message.id
            team_data_manager.group_message_texts[group_letter] = full_message
            team_data_manager._save_backup()

        except Exception as e:
            logger.error(f"[Discord] 조별 공지 전송 실패 - 채널: {channel.name}: {e}", exc_info=True)
            try:
                await channel.send(view=error_view(f"조별 공지 전송 중 오류가 발생했습니다.\n{message}"))
            except Exception as e2:
                logger.error(f"[Discord] 에러 메시지 전송 실패 - 채널: {channel.name}: {e2}", exc_info=True)
    
    async def _send_notices(self, guild: discord.Guild, groups: List[List], unmatched_teams: List[Tuple[str, TeamData, float]] = None) -> None:
        """공지를 전송합니다."""
        try:
            # 모든 조별 채널에 대해 처리 (팀이 있는 조와 없는 조 모두)
            for group_letter in settings.GROUP_CHANNEL_IDS.keys():
                try:
                    channel_id = settings.GROUP_CHANNEL_IDS.get(group_letter)
                    
                    if channel_id:
                        channel = guild.get_channel(channel_id)
                        if channel:
                            # 조별 채널의 모든 메시지 삭제 (팀이 있든 없든)
                            await self._clear_channel_messages(channel)
                            
                            # 해당 조에 팀이 있는지 확인
                            group_index = ord(group_letter) - ord('A')  # A=0, B=1, ...
                            # groups 리스트의 범위를 안전하게 체크하고, 해당 인덱스에 팀이 있는지 확인
                            if group_index < len(groups) and len(groups[group_index]) > 0:
                                # 팀이 있는 경우: 조별 공지 전송
                                group = groups[group_index]
                                message = self._create_group_announcement_message(group_letter, group)
                                await self._send_group_announcement_with_image(channel, message, group)
                            else:
                                # 팀이 없는 경우: 메시지만 삭제하고 공지는 전송하지 않음
                                pass
                        else:
                            logger.warning(f"[Discord] 조별 채널을 찾을 수 없음 - 조: {group_letter}조, 채널 ID: {channel_id}")
                    else:
                        logger.warning(f"[Discord] 조별 채널 ID가 설정되지 않음 - 조: {group_letter}조")
                except Exception as e:
                    # 각 조별 처리 중 오류가 발생해도 다른 조에 영향을 주지 않도록 개별 처리
                    logger.error(f"[Discord] 조별 공지 전송 실패 - 조: {group_letter}조: {e}", exc_info=True)
                    continue
            
            
            # 디스코드 역할 처리
            await self._handle_discord_roles(guild, groups)
            
            # 음성채널 이름 변경
            await self._rename_voice_channels(guild, groups)
                    
        except Exception as e:
            logger.error(f"[Discord] 공지 전송 실패: {e}", exc_info=True)
    
    async def _handle_discord_roles(self, guild: discord.Guild, groups: List[List]) -> None:
        """디스코드 역할을 처리합니다."""
        try:
            if not guild and self.client:
                guild = self.client.get_guild(settings.GUILD_ID)
                if not guild:
                    raise ValueError(f"서버 정보를 찾을 수 없습니다. (ID: {settings.GUILD_ID})")

            # 1. 모든 조 역할 가져오기 (A조 ~ F조)
            group_roles = {}
            for group_letter in ['A', 'B', 'C', 'D', 'E', 'F']:
                role_name = f"{group_letter}조"
                role = discord.utils.get(guild.roles, name=role_name)
                if role:
                    group_roles[group_letter] = role
                else:
                    logger.warning(f"[Discord] 역할을 찾을 수 없음 - 역할: {role_name}")

            # 2. 오늘 경기에 참여하는 팀 정보 정리
            team_info = []
            for group in groups:
                for team_name, team_data, mmr in group:
                    team_info.append((team_name, team_data, mmr))

            # 3. 오늘 경기 참여자 명단 생성 (그룹별) - normalize_nickname_for_comparison 사용
            from utils.validators import normalize_nickname_for_comparison
            
            today_participants = {letter: set() for letter in group_roles.keys()}
            
            for group_idx, group in enumerate(groups):
                if not group:  # 빈 그룹은 건너뛰기
                    continue
                group_letter = chr(65 + group_idx)  # A=65, B=66, ...
                
                for team_name, team_data, _ in group:
                    if isinstance(team_data, dict):
                        members = team_data["players"] + team_data.get("staff", [])
                    else:
                        # TeamData 객체인 경우
                        members = team_data.all_members
                    # normalize_nickname_for_comparison을 사용하여 정규화
                    today_participants[group_letter].update(normalize_nickname_for_comparison(member) for member in members)
            
            # 오늘 경기 참여자 수집 완료

            # 4. 역할 변경이 필요한 멤버 목록 생성
            role_updates = []
            for member in guild.members:
                # 디스코드 닉네임과 유저 닉네임 모두 확인 (normalize_nickname_for_comparison 사용)
                member_display_name = normalize_nickname_for_comparison(member.display_name)
                member_global_name = normalize_nickname_for_comparison(member.global_name) if member.global_name else ""
                member_name = normalize_nickname_for_comparison(member.name)
                
                current_group_roles = set(role for role in member.roles if role in group_roles.values())
                roles_to_add = set()
                
                # 각 조별로 확인하여 추가할 역할 수집
                for group_letter, participants in today_participants.items():
                    # 디스코드 닉네임, 유저 닉네임, 글로벌 닉네임 모두 확인
                    if (member_display_name in participants or 
                        member_global_name in participants or 
                        member_name in participants):
                        role = group_roles.get(group_letter)
                        if role:
                            roles_to_add.add(role)
                
                # 제거해야 할 역할 찾기
                roles_to_remove = current_group_roles - roles_to_add
                roles_to_add = roles_to_add - current_group_roles

                # 변경이 필요한 경우만 목록에 추가
                if roles_to_remove or roles_to_add:
                    role_updates.append((member, roles_to_remove, roles_to_add))

            # 5. 역할 업데이트를 배치로 처리 (Rate Limiting 고려)
            async def update_member_roles(member, roles_to_remove, roles_to_add):
                try:
                    if roles_to_remove:
                        max_retries = 3  # 재시도 횟수 최적화
                        for retry in range(max_retries):
                            try:
                                await member.remove_roles(*roles_to_remove)
                                break
                            except (discord.HTTPException, discord.Forbidden) as e:
                                if retry == max_retries - 1:
                                    logger.error(f"[Discord] 역할 제거 실패 - 멤버: {member.display_name}: {e}", exc_info=True)
                                else:
                                    await asyncio.sleep(0.2)  # 대기 시간 증가

                    if roles_to_add:
                        max_retries = 3  # 재시도 횟수 최적화
                        for retry in range(max_retries):
                            try:
                                await member.add_roles(*roles_to_add)
                                break
                            except (discord.HTTPException, discord.Forbidden) as e:
                                if retry == max_retries - 1:
                                    logger.error(f"[Discord] 역할 추가 실패 - 멤버: {member.display_name}: {e}", exc_info=True)
                                else:
                                    await asyncio.sleep(0.2)  # 대기 시간 증가

                except Exception as e:
                    logger.error(f"[Discord] 멤버 역할 업데이트 실패 - 멤버: {member.display_name}: {e}", exc_info=True)

            # 배치로 역할 업데이트 실행 (Rate Limiting 방지)
            if role_updates:
                # 배치 크기 제한 (동시 처리 수 제한)
                batch_size = 10
                for i in range(0, len(role_updates), batch_size):
                    batch = role_updates[i:i + batch_size]
                    tasks = [update_member_roles(member, roles_to_remove, roles_to_add) 
                            for member, roles_to_remove, roles_to_add in batch]
                    await asyncio.gather(*tasks, return_exceptions=True)
                    
                    # 배치 간 대기 (Rate Limiting 방지)
                    if i + batch_size < len(role_updates):
                        await asyncio.sleep(0.5)

        except Exception as e:
            logger.error(f"[Discord] 역할 처리 실패: {e}", exc_info=True)
    
    async def update_group_roles(self, guild: discord.Guild, group_letter: str, group_teams: List[Tuple[str, 'TeamData', float]]) -> None:
        """특정 조의 역할만 업데이트합니다 (로스터 변경 시 사용)."""
        try:
            from utils.validators import normalize_nickname_for_comparison
            
            if not guild and self.client:
                guild = self.client.get_guild(settings.GUILD_ID)
                if not guild:
                    raise ValueError(f"서버 정보를 찾을 수 없습니다. (ID: {settings.GUILD_ID})")

            # 해당 조의 역할 가져오기
            role_name = f"{group_letter}조"
            group_role = discord.utils.get(guild.roles, name=role_name)
            if not group_role:
                logger.warning(f"[Discord] 역할을 찾을 수 없음 - 역할: {role_name}")
                return

            # 해당 조의 참여자 명단 생성
            participants = set()
            for team_name, team_data, _ in group_teams:
                if isinstance(team_data, dict):
                    members = team_data["players"] + team_data.get("staff", [])
                else:
                    members = team_data.all_members
                participants.update(normalize_nickname_for_comparison(member) for member in members)

            # 역할 변경이 필요한 멤버 목록 생성
            role_updates = []
            for member in guild.members:
                # 디스코드 닉네임과 유저 닉네임 모두 확인
                member_display_name = normalize_nickname_for_comparison(member.display_name)
                member_global_name = normalize_nickname_for_comparison(member.global_name) if member.global_name else ""
                member_name = normalize_nickname_for_comparison(member.name)
                
                # 해당 조 역할 보유 여부 확인
                has_group_role = group_role in member.roles
                should_have_role = (member_display_name in participants or 
                                   member_global_name in participants or 
                                   member_name in participants)
                
                # 역할 추가 또는 제거가 필요한 경우
                if should_have_role and not has_group_role:
                    role_updates.append((member, None, [group_role]))
                elif not should_have_role and has_group_role:
                    role_updates.append((member, [group_role], None))

            # 역할 업데이트를 배치로 처리
            async def update_member_roles(member, roles_to_remove, roles_to_add):
                try:
                    if roles_to_remove:
                        max_retries = 3
                        for retry in range(max_retries):
                            try:
                                await member.remove_roles(*roles_to_remove)
                                break
                            except (discord.HTTPException, discord.Forbidden) as e:
                                if retry == max_retries - 1:
                                    logger.error(f"[Discord] 역할 제거 실패 - 멤버: {member.display_name}: {e}", exc_info=True)
                                else:
                                    await asyncio.sleep(0.2)

                    if roles_to_add:
                        max_retries = 3
                        for retry in range(max_retries):
                            try:
                                await member.add_roles(*roles_to_add)
                                break
                            except (discord.HTTPException, discord.Forbidden) as e:
                                if retry == max_retries - 1:
                                    logger.error(f"[Discord] 역할 추가 실패 - 멤버: {member.display_name}: {e}", exc_info=True)
                                else:
                                    await asyncio.sleep(0.2)

                except Exception as e:
                    logger.error(f"[Discord] 멤버 역할 업데이트 실패 - 멤버: {member.display_name}: {e}", exc_info=True)

            # 배치로 역할 업데이트 실행
            if role_updates:
                batch_size = 10
                for i in range(0, len(role_updates), batch_size):
                    batch = role_updates[i:i + batch_size]
                    tasks = [update_member_roles(member, roles_to_remove, roles_to_add) 
                            for member, roles_to_remove, roles_to_add in batch]
                    await asyncio.gather(*tasks, return_exceptions=True)
                    
                    if i + batch_size < len(role_updates):
                        await asyncio.sleep(0.5)
            
            logger.info(f"[Discord] 조별 역할 업데이트 완료 - 조: {group_letter}조, 변경된 멤버: {len(role_updates)}명")

        except Exception as e:
            logger.error(f"[Discord] 조별 역할 업데이트 실패: {e}", exc_info=True)
    
    async def _rename_voice_channels(self, guild: discord.Guild, groups: List[List]) -> None:
        """음성채널 이름을 조별로 변경합니다."""
        try:
            if not guild and self.client:
                guild = self.client.get_guild(settings.GUILD_ID)
                if not guild:
                    raise ValueError(f"서버 정보를 찾을 수 없습니다. (ID: {settings.GUILD_ID})")

            # 모든 조 (A~F)에 대해 처리
            for group_letter in ['A', 'B', 'C', 'D', 'E', 'F']:
                group_index = ord(group_letter) - ord('A')  # A=0, B=1, ...
                category_name = settings.GROUP_CATEGORY_PATTERN.format(letter=group_letter)
                
                if not category_name:
                    logger.warning(f"[Discord] 카테고리 패턴이 설정되지 않음 - 조: {group_letter}조")
                    continue
                
                # 해당 카테고리 찾기
                category = discord.utils.get(guild.categories, name=category_name)
                if not category:
                    logger.warning(f"[Discord] 카테고리를 찾을 수 없음 - 카테고리: {category_name}")
                    continue
                
                # 카테고리 내의 음성채널들을 가져오기 (정렬)
                voice_channels = [ch for ch in category.voice_channels if isinstance(ch, discord.VoiceChannel)]
                voice_channels.sort(key=lambda x: x.position)  # 위치 순으로 정렬
                
                if len(voice_channels) < 8:
                    logger.warning(f"[Discord] 카테고리 음성채널 부족 - 카테고리: {category_name}, 채널 수: {len(voice_channels)}개")
                
                # 해당 조에 팀이 있는지 확인
                changed_count = 0
                error_count = 0
                
                if group_index < len(groups) and groups[group_index]:
                    # 팀이 있는 경우: 1. 팀명, 2. 팀명 형식으로 이름 변경
                    group = groups[group_index]
                    for i, (team_name, team_data, mmr) in enumerate(group):
                        if i < len(voice_channels):
                            voice_channel = voice_channels[i]
                            new_name = f"{i+1}. {team_name}"
                            
                            # 현재 이름과 다를 때만 변경
                            if voice_channel.name != new_name:
                                try:
                                    await voice_channel.edit(name=new_name)
                                    changed_count += 1
                                except discord.HTTPException as e:
                                    logger.error(f"[Discord] 음성채널 이름 변경 실패 - 조: {group_letter}조, 채널: {voice_channel.name}: {e}", exc_info=True)
                                    error_count += 1
                                except discord.Forbidden:
                                    logger.error(f"[Discord] 음성채널 이름 변경 권한 없음 - 조: {group_letter}조, 채널: {voice_channel.name}")
                                    error_count += 1
                        else:
                            logger.warning(f"[Discord] 음성채널 부족 - 조: {group_letter}조, 팀: {i+1}팀, 총 채널: {len(voice_channels)}개")
                    
                    # 남은 채널들을 TBD로 변경
                    for i in range(len(group), len(voice_channels)):
                        voice_channel = voice_channels[i]
                        new_name = "TBD"
                        
                        # 현재 이름과 다를 때만 변경
                        if voice_channel.name != new_name:
                            try:
                                await voice_channel.edit(name=new_name)
                                changed_count += 1
                            except discord.HTTPException as e:
                                logger.error(f"[Discord] 음성채널 이름 변경 실패 - 조: {group_letter}조, 채널: {voice_channel.name}: {e}", exc_info=True)
                                error_count += 1
                            except discord.Forbidden:
                                logger.error(f"[Discord] 음성채널 이름 변경 권한 없음 - 조: {group_letter}조, 채널: {voice_channel.name}")
                                error_count += 1
                
                else:
                    # 팀이 없는 경우: 모든 채널을 TBD로 변경
                    for i, voice_channel in enumerate(voice_channels):
                        new_name = "TBD"
                        
                        # 현재 이름과 다를 때만 변경
                        if voice_channel.name != new_name:
                            try:
                                await voice_channel.edit(name=new_name)
                                changed_count += 1
                            except discord.HTTPException as e:
                                logger.error(f"[Discord] 음성채널 이름 변경 실패 - 조: {group_letter}조, 채널: {voice_channel.name}: {e}", exc_info=True)
                                error_count += 1
                            except discord.Forbidden:
                                logger.error(f"[Discord] 음성채널 이름 변경 권한 없음 - 조: {group_letter}조, 채널: {voice_channel.name}")
                                error_count += 1
                
                if changed_count > 0 or error_count > 0:
                    logger.info(f"[Discord] 음성채널 이름 변경 완료 - 조: {group_letter}조, 변경: {changed_count}개, 오류: {error_count}개")

            # 음성채널 이름 변경 완료
            
        except Exception as e:
            logger.error(f"[Discord] 음성채널 이름 변경 실패: {e}", exc_info=True)
    
    def _create_group_announcement_message(self, group_letter: str, group: List[Tuple[str, TeamData, float]]) -> str:
        """조별 공지 메시지를 생성합니다."""
        from utils.helpers import get_current_kst_time
        from services.notion_api import get_server_info

        current_time = get_current_kst_time()
        date_str = current_time.strftime('%m.%d')

        info = get_server_info()
        message = f"📢 {date_str} 20시 스크림 {group_letter}조 조편성 결과\n{info['operate']}"

        return message
    
    async def _get_group_role_mention(self, guild: discord.Guild, group_letter: str) -> str:
        """조별 역할 멘션을 가져옵니다."""
        # 임시로 모든 조별 멘션 비활성화
        return ""
        
        # try:
        #     # 조별 역할 이름 패턴 (예: "A조", "B조", "C조", ...)
        #     role_name = f"{group_letter}조"
        #     role = discord.utils.get(guild.roles, name=role_name)
        #     
        #     if role:
        #         return f"<@&{role.id}>"
        #     else:
        #         logger.warning(f"조별 역할을 찾을 수 없습니다: {role_name}")
        #         return ""
        #         
        # except Exception as e:
        #     logger.error(f"조별 역할 멘션 가져오기 중 오류 발생: {e}")
        #     return ""
    
    async def _clear_channel_messages(self, channel: discord.TextChannel) -> None:
        """채널의 모든 메시지를 삭제합니다."""
        try:
            # 레이트리밋 부담을 줄이기 위해 최근 메시지 기준 배치 삭제
            if not isinstance(channel, discord.TextChannel):
                return

            max_batches = 5
            batch_size = 200  # Discord bulk delete 한도 내
            total_deleted = 0
            
            for _ in range(max_batches):
                deleted = await channel.purge(limit=batch_size, oldest_first=False, reason="Scrim auto-clean")
                batch_deleted = len(deleted)
                total_deleted += batch_deleted

                if batch_deleted == 0:
                    break

                # Rate limiting 방지
                await asyncio.sleep(0.7)

            if total_deleted > 0:
                logger.info(f"[Discord] 채널 메시지 삭제 완료 - 채널: {channel.name}, 삭제된 메시지: {total_deleted}개")
            else:
                # 삭제할 메시지가 없는 경우는 로그 제거 (불필요한 DEBUG 로그)
                pass
            
        except Exception as e:
            logger.error(f"[Discord] 채널 메시지 삭제 실패 - 채널: {channel.name}: {e}", exc_info=True)
    
    async def _delete_single_message_with_retry(self, message: discord.Message, max_retries: int = 3) -> int:
        """단일 메시지를 재시도 로직과 함께 삭제합니다."""
        for attempt in range(max_retries):
            try:
                await message.delete()
                return 1
            except discord.NotFound:
                # 메시지가 이미 삭제된 경우
                return 0
            except discord.Forbidden:
                # 삭제 권한이 없는 경우
                logger.warning(f"[Discord] 메시지 삭제 권한 없음 - 메시지 ID: {message.id}")
                return 0
            except discord.HTTPException as e:
                # Rate limit 또는 기타 HTTP 오류
                if e.status == 429:  # Rate limit
                    if attempt < max_retries - 1:
                        # Retry-After 헤더를 확인하거나 기본 대기 시간 사용
                        retry_after = getattr(e, 'retry_after', 1.0)
                        await asyncio.sleep(retry_after)
                        continue
                    else:
                        logger.warning(f"[Discord] 메시지 삭제 실패 - Rate limit, 최대 재시도 초과, 메시지 ID: {message.id}")
                        return 0
                else:
                    # HTTP 오류는 재시도 중이므로 로그 제거 (불필요한 DEBUG 로그)
                    if attempt < max_retries - 1:
                        await asyncio.sleep(0.5 * (attempt + 1))  # 지수 백오프
                        continue
                    return 0
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))  # 지수 백오프
                    continue
                return 0
        
        return 0
    
    async def _delete_message_batch(self, messages: List[discord.Message]) -> int:
        """메시지 배치를 삭제합니다. (레거시 메서드, 호환성 유지)"""
        deleted_count = 0
        for message in messages:
            deleted_count += await self._delete_single_message_with_retry(message)
        return deleted_count
    
    async def _delete_single_message(self, message: discord.Message) -> int:
        """단일 메시지를 삭제합니다. (레거시 메서드, 호환성 유지)"""
        return await self._delete_single_message_with_retry(message)
    
