"""
팀 처리 모델

팀 MMR 조회, 조편성 알고리즘, 시드/테스트 계정 관리를 담당합니다.
BSER API를 통해 팀 MMR을 조회하고, 시드 데이터를 기반으로 조편성을 수행합니다.
Discord API 관련 작업은 services.discord_service에 위임합니다.
"""
import asyncio
import heapq
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple, Union

import discord
import gspread
from discord.ext import commands
from config.logging_config import get_logger
from config.settings import settings
from services.bser_api import BSERAPIClient
from utils.gsheet_client import create_gspread_client
from utils.helpers import extract_players_only

from .team_data import TeamData
from services.discord_service import DiscordService

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
        self._seeds_loaded_at: float = 0.0
        # 테스트 계정 데이터 (닉네임 -> MMR 매핑)
        self.test_accounts_data: Dict[str, float] = {}
        self._test_accounts_loaded_at: float = 0.0
        self._test_accounts_attempted_at: float = 0.0
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

        # Discord 서비스 위임
        self._discord_service = DiscordService(self)
    
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
        self.gspread_client, self.gspread_spreadsheet = create_gspread_client(caller='구글시트')
    
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
    
    def _load_test_accounts_data_sync(self) -> bool:
        """구글 시트에서 테스트 계정 데이터를 동기적으로 로드합니다.

        실패해도 test_accounts_data 를 비우지 않습니다. 캐시를 비우면 시트에
        등록된 테스트 계정이 미등록으로 취급되어 신청이 반려됩니다.

        Returns:
            로드 성공 여부
        """
        if not self.gspread_spreadsheet:
            self._initialize_gspread_client()
            if not self.gspread_spreadsheet:
                logger.warning("[구글시트] 스프레드시트를 열 수 없음")
                return False

        try:
            worksheet = self.gspread_spreadsheet.worksheet(settings.GOOGLE_SHEETS_TEST_ACCOUNTS_WORKSHEET_NAME)
            all_values = worksheet.get_all_records()
        except gspread.exceptions.WorksheetNotFound:
            logger.warning(f"[구글시트] 테스트 계정 시트를 찾을 수 없음 - 시트명: {settings.GOOGLE_SHEETS_TEST_ACCOUNTS_WORKSHEET_NAME}")
            return False
        except Exception as e:
            logger.error(f"[구글시트] 테스트 계정 데이터 로드 실패: {e}", exc_info=True)
            return False

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
        return True

    TEST_ACCOUNTS_TTL_SECONDS = 300  # 테스트 계정 시트 캐시 TTL (5분)
    TEST_ACCOUNTS_RETRY_COOLDOWN_SECONDS = 30

    async def ensure_test_accounts_loaded(self, force: bool = False) -> bool:
        """테스트 계정 시트를 재로드합니다 (TTL 캐시).

        test_accounts_data 는 __init__ 에서 한 번만 로드되므로, 봇 실행 중
        '테스트' 시트에 추가된 계정은 기본적으로 인식되지 않습니다. 그 경우
        해당 계정이 MMR 평균에서 탈락해 팀 MMR 이 2인/1인 평균으로 잘못
        계산되고, 신청 시에는 일반 계정으로 취급되어 반려됩니다.

        직전 시도가 실패했으면 쿨다운 동안 재시도하지 않습니다. 신청이 몰릴 때
        매 호출이 재시도 지연을 그대로 물지 않도록 합니다.

        시트 I/O 는 blocking 이므로 스레드로 오프로딩해 이벤트 루프를 막지 않습니다.

        Returns:
            캐시가 최신 시트 내용인지 여부
        """
        now = time.monotonic()
        is_fresh = bool(
            self._test_accounts_loaded_at
            and (now - self._test_accounts_loaded_at) < self.TEST_ACCOUNTS_TTL_SECONDS
        )
        in_cooldown = bool(
            self._test_accounts_attempted_at
            and (now - self._test_accounts_attempted_at) < self.TEST_ACCOUNTS_RETRY_COOLDOWN_SECONDS
        )
        if not force and (is_fresh or in_cooldown):
            return is_fresh

        self._test_accounts_attempted_at = now
        try:
            loaded = await asyncio.to_thread(self._load_test_accounts_data_sync)
        except Exception as e:
            logger.error(f"[테스트계정] 시트 재로드 실패 (기존 데이터 유지): {e}", exc_info=True)
            loaded = False

        if loaded:
            self._test_accounts_loaded_at = now
        return loaded

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
    
    SEEDS_TTL_SECONDS = 3600  # 시드 시트 캐시 TTL (1시간)

    async def ensure_seeds_marked(self, teams: Dict[str, TeamData]) -> None:
        """시드 데이터 (1시간 TTL 캐시) 로드 후 팀들에 is_seed/seed_name을 마킹합니다."""
        import time
        now = time.monotonic()
        if self.seeds_data is None or (now - self._seeds_loaded_at) >= self.SEEDS_TTL_SECONDS:
            await self._load_seeds_data()
            self._seeds_loaded_at = now
        await self._identify_seeded_teams(teams)

    async def _identify_seeded_teams(self, teams: Dict[str, TeamData]) -> Dict[str, int]:
        """시드팀을 식별하고 우선순위를 부여합니다.
        
        Returns:
            Dict[str, int]: 팀명을 키로 하고 우선순위(1, 2)를 값으로 하는 딕셔너리
            - 1순위: 시드팀 (시드 데이터에 있는 모든 팀)
            - 2순위: 시드가 없는 팀
        """
        team_priorities = {}

        # 이전 식별 결과 초기화 (시드 데이터 갱신 또는 팀 변경 반영)
        for team_data in teams.values():
            team_data.is_seed = False
            team_data.seed_name = None

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
                    team_data.is_seed = True
                    team_data.seed_name = seed_team.get("team_name") or None
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
                        # 실패 시 기존 MMR 유지, 0 반환으로 호출처에 실패 알림
                        return team_name, team_data, 0.0

                    # TeamData 객체에 MMR 저장 (성공 시에만)
                    if isinstance(team_data, dict):
                        team_data['mmr'] = avg_mmr
                    else:
                        team_data.mmr = avg_mmr

                    return team_name, team_data, avg_mmr
            except Exception as e:
                logger.error(f"[MMR조회] API 클라이언트 사용 실패: {e}", exc_info=True)
                return team_name, team_data, 0.0

        except Exception as e:
            logger.error(f"[MMR조회] 팀 MMR 조회 실패: {e}", exc_info=True)
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
        # 시트에 새로 추가된 테스트 계정도 인식되도록 조회 직전 재로드
        await self.ensure_test_accounts_loaded()

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
            excluded_teams = []

            if len(all_teams) > max_teams:
                # 우선순위별로 팀 분류
                priority_1_teams = [team for team in all_teams if team_priorities.get(team[0], 2) == 1]  # 시드팀
                priority_2_teams = [team for team in all_teams if team_priorities.get(team[0], 2) == 2]  # 비시드팀

                # 각 우선순위 그룹 내에서 MMR 순으로 정렬
                priority_1_teams.sort(key=lambda x: x[2], reverse=True)
                priority_2_teams.sort(key=lambda x: x[2], reverse=True)

                final_teams = []
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
                    logger.warning(f"[조편성] 예비팀 - {len(excluded_team_names)}개: {', '.join(excluded_team_names[:10])}{'...' if len(excluded_team_names) > 10 else ''}")
            else:
                final_teams = all_teams

            # 팀을 그룹으로 분배
            groups, unmatched_teams = self._distribute_teams_to_groups(final_teams, team_priorities)

            # 제외된 팀(예비팀)을 unmatched_teams에 합침
            unmatched_teams.extend(excluded_teams)

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
    
    
    # ──────────────────────────────────────────────
    # Discord 서비스 위임 (DiscordService)
    # ──────────────────────────────────────────────

    async def _send_global_announcement(self, guild, groups, unmatched_teams=None):
        await self._discord_service.send_global_announcement(guild, groups, unmatched_teams)

    async def _send_notices(self, guild, groups, unmatched_teams=None):
        await self._discord_service.send_notices(guild, groups, unmatched_teams)

    async def update_group_roles(self, guild, group_letter, group_teams):
        await self._discord_service.update_group_roles(guild, group_letter, group_teams)

    def _create_group_announcement_message(self, group_letter, group):
        return self._discord_service.create_group_announcement_message(group_letter, group)

    async def _get_group_role_mention(self, guild, group_letter):
        return await self._discord_service._get_group_role_mention(guild, group_letter)


