"""
BotManager 싱글톤 모듈

모든 곳에서 동일한 BotManager 인스턴스를 사용하도록 전용 모듈로 분리했습니다.
"""
from typing import Dict, List, Optional

from bot.client import ScrimBot
from config.logging_config import get_logger
from config.settings import settings
from models.team_data_manager import TeamDataManager
from models.team_processor import TeamProcessor
from models.schedule_manager import ScheduleManager

logger = get_logger("bot_manager")


class BotManager:
    """
    봇 관리자 클래스 (싱글톤)

    Discord 봇 클라이언트의 생명주기를 관리합니다.
    클라이언트 재생성, MMR 업데이트 대기열 처리, 명령어 동기화 등의 기능을 제공합니다.
    싱글톤 패턴으로 구현되어 전역에서 하나의 인스턴스만 존재합니다.
    """

    _instance: Optional["BotManager"] = None
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(BotManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized") and self._initialized:
            return

        self._client: Optional[ScrimBot] = None
        self._team_data_manager: Optional[TeamDataManager] = None
        self._team_processor: Optional[TeamProcessor] = None
        self._warning_manager = None
        self._schedule_manager: Optional[ScheduleManager] = None
        self._selected_weathers: Dict[str, List[str]] = {}
        self._initialized = True

    @classmethod
    def get_instance(cls) -> "BotManager":
        """싱글톤 인스턴스 반환"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_client(self, client: ScrimBot) -> None:
        """클라이언트 설정"""
        self._client = client

    def get_client(self) -> Optional[ScrimBot]:
        """클라이언트 반환"""
        return self._client

    def get_team_data_manager(self) -> TeamDataManager:
        """팀 데이터 매니저 반환"""
        if self._team_data_manager is None:
            self._team_data_manager = TeamDataManager(self._client)
        return self._team_data_manager

    async def reset_team_data_manager(self, client: Optional[ScrimBot] = None) -> TeamDataManager:
        """
        팀 데이터 매니저를 새로 생성하여 상태를 초기화합니다.
        기존 매니저의 백그라운드 태스크도 완전히 취소될 때까지 대기합니다.
        """
        if self._team_data_manager:
            try:
                await self._team_data_manager.reset_team_data()
            except Exception as exc:  # pragma: no cover - 최후 안전장치
                logger.warning(f"[봇관리] 팀 데이터 매니저 리셋 중 예외 무시: {exc}")
        
        # ✅ TeamProcessor의 이미지 캐시 클리어
        if self._team_processor:
            try:
                self._team_processor.group_image_cache.clear()
                self._team_processor.current_cache_size = 0
                logger.debug("[봇관리] TeamProcessor 이미지 캐시 클리어 완료")
            except Exception as exc:
                logger.warning(f"[봇관리] 이미지 캐시 클리어 중 예외 무시: {exc}")
        
        self._selected_weathers.clear()
        logger.debug("[봇관리] 날씨 상태 초기화 완료")

        self._team_data_manager = TeamDataManager(client or self._client)
        return self._team_data_manager

    def get_schedule_manager(self) -> ScheduleManager:
        """일정 관리자 반환"""
        if self._schedule_manager is None:
            self._schedule_manager = ScheduleManager()
            self._schedule_manager.load_backup()
        return self._schedule_manager

    def get_warning_manager(self):
        """경고 관리자 반환"""
        if self._warning_manager is None:
            from models.warning_manager import WarningManager

            self._warning_manager = WarningManager()
        return self._warning_manager

    def _trigger_backup(self) -> None:
        """팀 데이터 매니저의 백업을 트리거합니다."""
        if self._team_data_manager:
            self._team_data_manager._save_backup()

    def add_selected_weather(self, group_letter: str, weather: str) -> None:
        """조별 서브 날씨 선택 기록 추가"""
        if group_letter not in self._selected_weathers:
            self._selected_weathers[group_letter] = []
        self._selected_weathers[group_letter].append(weather)
        self._trigger_backup()

    def get_selected_weathers(self, group_letter: str) -> List[str]:
        """조별 선택된 서브 날씨 리스트 반환"""
        return self._selected_weathers.get(group_letter, [])

    def get_team_processor(self) -> TeamProcessor:
        """팀 프로세서 반환 (싱글톤)"""
        if self._team_processor is None:
            self._team_processor = TeamProcessor(self._client)
        else:
            if self._team_processor.client != self._client:
                self._team_processor.update_client(self._client)
        return self._team_processor


# 편의 함수
def get_client() -> Optional[ScrimBot]:
    return BotManager.get_instance().get_client()


def get_team_data_manager() -> TeamDataManager:
    return BotManager.get_instance().get_team_data_manager()


