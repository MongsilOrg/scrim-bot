import asyncio
import heapq
import time
from io import BytesIO
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import gspread
from discord.ext import commands
from config.logging_config import get_logger
from config.settings import settings
from services.bser_api import BSERAPIClient
from services.image_generator import ImageGenerator
from utils.gsheet_client import create_gspread_client
from utils.helpers import normalize_player_list
from utils.validators import normalize_nickname_for_comparison

from .team_data import TeamData
from services.discord_service import DiscordService

if TYPE_CHECKING:
    from .team_data_manager import TeamDataManager

logger = get_logger('team_processor')


class TeamProcessor:

    def __init__(self, client: Optional[commands.Bot], team_data_manager: "TeamDataManager"):
        self.client = client
        self.api_key = settings.BSER_API_KEY
        if not self.api_key:
            raise ValueError("API 키가 설정되지 않았습니다.")

        self.seeds_data = None
        self._seeds_loaded_at: float = 0.0
        self.test_accounts_data: Dict[str, float] = {}
        self._test_accounts_by_key: Dict[str, float] = {}
        self._test_accounts_loaded_at: float = 0.0
        self._test_accounts_attempted_at: float = 0.0
        self.gspread_client: Optional[gspread.Client] = None
        self.gspread_spreadsheet: Optional[gspread.Spreadsheet] = None
        self.group_image_cache: Dict[str, bytes] = {}

        self._initialize_gspread_client()

        self._load_test_accounts_data_sync()

        self._discord_service = DiscordService(self, team_data_manager)
    
    def update_client(self, client: Optional[commands.Bot]) -> None:
        self.client = client
    
    async def generate_group_image(
        self, group_letter: str, group_teams: dict,
        *, sort_by_mmr: bool = True, refresh: bool = False,
    ) -> Optional[BytesIO]:
        try:
            cache_key = f"{group_letter}:{'mmr' if sort_by_mmr else 'num'}"
            if refresh:
                self.group_image_cache.pop(f"{group_letter}:mmr", None)
                self.group_image_cache.pop(f"{group_letter}:num", None)

            img_data = self.group_image_cache.get(cache_key)
            if img_data is not None:
                return BytesIO(img_data)

            img_io = await ImageGenerator.generate_mmr_image_async(group_teams, sort_by_mmr=sort_by_mmr)
            if img_io:
                self.group_image_cache[cache_key] = img_io.getvalue()
                img_io.seek(0)
                return img_io

            return None
        except Exception as e:
            logger.error(f"[이미지생성] 조별 이미지 생성 실패 - 조: {group_letter}조: {e}", exc_info=True)
            return None
    
    def _initialize_gspread_client(self) -> None:
        self.gspread_client, self.gspread_spreadsheet = create_gspread_client(caller='구글시트')
    
    async def _load_seeds_data(self) -> bool:
        return await asyncio.to_thread(self._load_seeds_data_sync)

    def _load_seeds_data_sync(self) -> bool:
        """실패 시 seeds_data를 비우면 TTL 동안 시드팀 우선 선발이 빠짐."""
        try:
            if not self.gspread_spreadsheet:
                self._initialize_gspread_client()
                if not self.gspread_spreadsheet:
                    logger.warning("[구글시트] 스프레드시트를 열 수 없음")
                    return False

            try:
                worksheet = self.gspread_spreadsheet.worksheet(settings.GOOGLE_SHEETS_SEEDS_WORKSHEET_NAME)
            except gspread.exceptions.WorksheetNotFound:
                logger.warning(f"[구글시트] 시드팀 시트를 찾을 수 없음 - 시트명: {settings.GOOGLE_SHEETS_SEEDS_WORKSHEET_NAME}")
                return False

            all_values = worksheet.get_all_records()
            
            all_seeds = []
            
            for row in all_values:
                team_name = str(row.get('team_name', '')).strip()
                
                players = []
                for i in range(1, 5):
                    player_col = f'player{i}'
                    if player_col in row and row[player_col] and str(row[player_col]).strip():
                        players.append(str(row[player_col]).strip())
                
                if team_name and players:
                    team_data = {
                        "team_name": team_name,
                        "players": players
                    }
                    all_seeds.append(team_data)
            
            self.seeds_data = {
                "seeds": all_seeds
            }
            return True

        except Exception as e:
            logger.error(f"[조편성] 시드 데이터 로드 실패 (기존 데이터 유지): {e}", exc_info=True)
            return False
    
    def _load_test_accounts_data_sync(self) -> bool:
        """실패 시 test_accounts_data를 비우면 등록된 테스트 계정의 신청이 반려됨."""
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

        self._set_test_accounts(test_accounts)
        return True

    def _set_test_accounts(self, accounts: Dict[str, float]) -> None:
        self.test_accounts_data = accounts
        self._test_accounts_by_key = {
            normalize_nickname_for_comparison(nickname): mmr
            for nickname, mmr in accounts.items()
        }

    TEST_ACCOUNTS_TTL_SECONDS = 300
    TEST_ACCOUNTS_RETRY_COOLDOWN_SECONDS = 30

    async def ensure_test_accounts_loaded(self, force: bool = False) -> bool:
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

    def is_test_account(self, nickname: str) -> bool:
        return normalize_nickname_for_comparison(nickname) in self._test_accounts_by_key

    def _get_test_account_mmr(self, nickname: str) -> Optional[float]:
        return self._test_accounts_by_key.get(normalize_nickname_for_comparison(nickname))
    
    def _calculate_test_team_mmr(self, players: List[str]) -> float:
        mmr_list = []
        for player in players:
            mmr = self._get_test_account_mmr(player)
            if mmr is None:
                logger.warning(f"[MMR조회] 테스트 계정 시트에 없음 - 플레이어: {player}")
                continue
            mmr_list.append(mmr)

        return self._average_top_three(mmr_list, len(players))

    @staticmethod
    def _average_top_three(mmr_list: List[float], expected_count: int) -> float:
        """값을 모르는 인원이 있으면 0.0."""
        if not mmr_list or len(mmr_list) < expected_count:
            return 0.0
        top_3_mmr = heapq.nlargest(3, mmr_list)
        return sum(top_3_mmr) / len(top_3_mmr)
    
    def _extract_players_only(self, team_data: TeamData) -> List[str]:
        """MMR 조회용이라 원본 대소문자 유지."""
        return [player.strip() for player in team_data.players if player and player.strip()]

    def _are_players_matching(self, players1: List[str], players2: List[str]) -> bool:
        norm_players1 = set(normalize_player_list(players1))
        norm_players2 = set(normalize_player_list(players2))
        return norm_players1 == norm_players2 and len(norm_players1) in (3, 4)
    
    SEEDS_TTL_SECONDS = 3600

    async def ensure_seeds_marked(self, teams: Dict[str, TeamData]) -> None:
        now = time.monotonic()
        if self.seeds_data is None or (now - self._seeds_loaded_at) >= self.SEEDS_TTL_SECONDS:
            if await self._load_seeds_data():
                self._seeds_loaded_at = now
        await self._identify_seeded_teams(teams)

    async def _identify_seeded_teams(self, teams: Dict[str, TeamData]) -> Dict[str, int]:
        """반환 팀명별 우선순위, 1은 시드팀, 2는 비시드팀."""
        team_priorities = {}

        for team_data in teams.values():
            team_data.is_seed = False
            team_data.seed_name = None

        if not self.seeds_data or not self.seeds_data.get("seeds"):
            for team_name in teams.keys():
                team_priorities[team_name] = 2
            return team_priorities

        all_seeds = self.seeds_data.get("seeds", [])

        for team_name in teams.keys():
            team_priorities[team_name] = 2

        for team_name, team_data in teams.items():
            team_players = self._extract_players_only(team_data)

            for seed_team in all_seeds:
                seed_players = seed_team.get("players", [])
                if self._are_players_matching(team_players, seed_players):
                    team_priorities[team_name] = 1
                    team_data.is_seed = True
                    team_data.seed_name = seed_team.get("team_name") or None
                    break
        
        priority_1_count = sum(1 for priority in team_priorities.values() if priority == 1)
        priority_2_count = sum(1 for priority in team_priorities.values() if priority == 2)
        
        logger.info(f"[조편성] 시드팀 식별 완료 - 시드팀: {priority_1_count}개, 비시드팀: {priority_2_count}개")
        
        return team_priorities
    
    async def fetch_team_mmr(self, team_name: str, team_data: TeamData) -> Tuple[str, TeamData, float]:
        """실패 시 0.0. 여기서 team_data.mmr을 바꾸면 set_team_mmr의 _mmr_dirty 감지 무력화."""
        try:
            players = self._extract_players_only(team_data)

            has_test_account = any(self.is_test_account(player) for player in players)

            if has_test_account and all(self.is_test_account(player) for player in players):
                avg_mmr = self._calculate_test_team_mmr(players)
                return team_name, team_data, avg_mmr
            
            try:
                async with BSERAPIClient() as api_client:
                    async def _fetch_player_mmr(player: str) -> Optional[float]:
                        try:
                            if self.is_test_account(player):
                                mmr = self._get_test_account_mmr(player)
                                if mmr is None:
                                    logger.warning(f"[MMR조회] 테스트 계정 시트에 없음 - 플레이어: {player}")
                                return mmr
                            uid = await api_client.get_user_uid(player)
                            if not uid:
                                logger.warning(f"[MMR조회] 플레이어 UID 조회 실패 - 플레이어: {player}")
                                return None
                            mmr = await api_client.get_user_mmr(uid)
                            if mmr is None:
                                logger.warning(f"[MMR조회] 플레이어 MMR 조회 실패 - 플레이어: {player}, UID: {uid}")
                            return mmr
                        except Exception as e:
                            logger.warning(f"[MMR조회] 플레이어 MMR 조회 실패 - 플레이어: {player}: {e}")
                            return None

                    results = await asyncio.gather(*[_fetch_player_mmr(p) for p in players])
                    mmr_list = [m for m in results if m is not None]

                    avg_mmr = self._average_top_three(mmr_list, len(players))
                    if avg_mmr == 0.0:
                        missing = [p for p, m in zip(players, results) if m is None]
                        logger.warning(f"[MMR조회] 일부 플레이어 MMR 조회 실패로 팀 MMR 미확정 - 팀명: {team_name}, 대상: {missing}")

                    return team_name, team_data, avg_mmr
            except Exception as e:
                logger.error(f"[MMR조회] API 클라이언트 사용 실패: {e}", exc_info=True)
                return team_name, team_data, 0.0

        except Exception as e:
            logger.error(f"[MMR조회] 팀 MMR 조회 실패: {e}", exc_info=True)
            return team_name, team_data, 0.0
    
    async def build_groups(self, teams: Dict[str, TeamData]) -> Tuple[List[List], List]:
        try:
            # 닉네임 캐시는 불변 데이터라 유지
            BSERAPIClient.clear_mmr_cache()
            self.group_image_cache.clear()

            if await self._load_seeds_data():
                self._seeds_loaded_at = time.monotonic()

            if not (self.seeds_data and self.seeds_data.get("seeds")):
                logger.warning("[조편성] 시드 데이터 없음")

            team_priorities = await self._identify_seeded_teams(teams)

            team_info = await self._fetch_all_team_mmr(teams)

            groups, unmatched_teams = await self._process_team_groups(team_info, team_priorities)

            logger.info(f"[조편성] 조편성 완료 - 조 수: {len(groups)}개, 매칭되지 않은 팀: {len(unmatched_teams)}개")

            return groups, unmatched_teams

        except Exception as e:
            logger.error(f"[조편성] 팀 처리 실패: {e}", exc_info=True)
            raise

    async def _fetch_all_team_mmr(self, teams: Dict[str, TeamData]) -> List[Tuple[str, TeamData, float]]:
        await self.ensure_test_accounts_loaded()

        tasks = [
            self.fetch_team_mmr(team_name, team_data)
            for team_name, team_data in teams.items()
        ]

        team_info = []
        for team_name, team_data, mmr in await asyncio.gather(*tasks):
            if mmr <= 0 and team_data.mmr > 0:
                logger.warning(f"[조편성] MMR 조회 실패, 마지막 확정값 사용 - 팀명: {team_name}, MMR: {team_data.mmr:.2f}")
                mmr = team_data.mmr
            team_info.append((team_name, team_data, mmr))

        team_info.sort(key=lambda x: x[2], reverse=True)

        return team_info
    
    async def _process_team_groups(self, team_info: List[Tuple[str, TeamData, float]], team_priorities: Dict[str, int] = None) -> Tuple[List[List], List]:
        try:
            if team_priorities is None:
                team_priorities = {}

            all_teams = sorted(team_info, key=lambda x: x[2], reverse=True)
            
            priority_1_count = sum(1 for team in all_teams if team_priorities.get(team[0], 2) == 1)
            priority_2_count = sum(1 for team in all_teams if team_priorities.get(team[0], 2) == 2)
            
            logger.info(f"[조편성] 우선순위 통계 - 시드팀: {priority_1_count}개, 비시드팀: {priority_2_count}개, 전체: {len(all_teams)}개")

            per_group = settings.TEAMS_PER_GROUP
            max_teams = (len(all_teams) // per_group) * per_group
            excluded_teams = []

            if len(all_teams) > max_teams:
                priority_1_teams = [team for team in all_teams if team_priorities.get(team[0], 2) == 1]
                priority_2_teams = [team for team in all_teams if team_priorities.get(team[0], 2) == 2]

                priority_1_teams.sort(key=lambda x: x[2], reverse=True)
                priority_2_teams.sort(key=lambda x: x[2], reverse=True)

                final_teams = []
                priority_2_selected = []

                if len(priority_1_teams) <= max_teams:
                    final_teams.extend(priority_1_teams)
                    remaining_slots = max_teams - len(priority_1_teams)

                    if remaining_slots > 0 and priority_2_teams:
                        priority_2_selected = priority_2_teams[:remaining_slots]
                        final_teams.extend(priority_2_selected)

                    excluded_teams.extend(priority_2_teams[len(priority_2_selected):])
                else:
                    final_teams = priority_1_teams[:max_teams]
                    excluded_teams.extend(priority_1_teams[max_teams:])
                    excluded_teams.extend(priority_2_teams)

                final_teams.sort(key=lambda x: x[2], reverse=True)

                if excluded_teams:
                    excluded_team_names = [team[0] for team in excluded_teams]
                    logger.warning(f"[조편성] 예비팀 - {len(excluded_team_names)}개: {', '.join(excluded_team_names[:10])}{'...' if len(excluded_team_names) > 10 else ''}")
            else:
                final_teams = all_teams

            groups, unmatched_teams = self._distribute_teams_to_groups(final_teams)

            unmatched_teams.extend(excluded_teams)

            if len(groups) >= 2:
                groups = self._apply_snake_draft(groups)

            return groups, unmatched_teams
        except Exception as e:
            logger.error(f"[조편성] 팀 그룹 처리 실패: {e}", exc_info=True)
            raise
    
    def _distribute_teams_to_groups(self, sorted_teams: List[Tuple[str, TeamData, float]]) -> Tuple[List[List], List[Tuple[str, TeamData, float]]]:
        groups = []
        unmatched_teams = []

        for i in range(0, len(sorted_teams), settings.TEAMS_PER_GROUP):
            group = sorted_teams[i:i + settings.TEAMS_PER_GROUP]
            if len(group) == settings.TEAMS_PER_GROUP:
                groups.append(group)
            else:
                unmatched_teams.extend(group)

        return groups, unmatched_teams

    def _apply_snake_draft(self, groups: List[List[Tuple[str, TeamData, float]]]) -> List[List[Tuple[str, TeamData, float]]]:
        num_groups = len(groups)
        
        if num_groups == 1:
            return groups
        
        all_teams = []
        for group in groups:
            all_teams.extend(group)
        all_teams.sort(key=lambda x: x[2], reverse=True)
        
        new_groups = [[] for _ in range(num_groups)]
        self._apply_grouped_snake_pattern(all_teams, new_groups, num_groups)
        return new_groups
    
    def _apply_grouped_snake_pattern(self, teams: List[Tuple[str, TeamData, float]], groups: List[List], num_groups: int) -> None:
        team_idx = 0
        pair_size = 2 * settings.TEAMS_PER_GROUP

        for group_pair in range(0, num_groups, 2):
            if group_pair + 1 < num_groups:
                pair_teams = teams[team_idx:team_idx + pair_size]
                self._apply_snake_pattern(pair_teams, groups[group_pair:group_pair + 2], 2)
                team_idx += pair_size
            else:
                remaining_teams = teams[team_idx:]
                groups[group_pair] = remaining_teams
    
    def _apply_snake_pattern(self, teams: List[Tuple[str, TeamData, float]], groups: List[List], num_groups: int) -> None:
        for i, team in enumerate(teams):
            if i in [0, 3, 4, 7, 8, 11, 12, 15]:
                groups[0].append(team)
            else:
                groups[1].append(team)
    

    @property
    def discord_service(self) -> DiscordService:
        return self._discord_service



