"""
BotManager 싱글톤 모듈
"""
from typing import Optional, TYPE_CHECKING

from bot.client import ScrimBot
from config.logging_config import get_logger

if TYPE_CHECKING:
    from models.schedule_manager import ScheduleManager
    from models.team_data_manager import TeamDataManager
    from models.team_processor import TeamProcessor
    from models.warning_manager import WarningManager

logger = get_logger("bot_manager")


class BotManager:
    """클라이언트와 매니저 인스턴스를 보관하는 싱글톤 컨테이너"""

    _instance: Optional["BotManager"] = None

    def __init__(self):
        self._client: Optional[ScrimBot] = None
        self._team_data_manager: Optional["TeamDataManager"] = None
        self._team_processor: Optional["TeamProcessor"] = None
        self._warning_manager: Optional["WarningManager"] = None
        self._schedule_manager: Optional["ScheduleManager"] = None

    @classmethod
    def get_instance(cls) -> "BotManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_client(self, client: ScrimBot) -> None:
        self._client = client

    def get_client(self) -> Optional[ScrimBot]:
        return self._client

    def get_team_data_manager(self) -> "TeamDataManager":
        if self._team_data_manager is None:
            from models.team_data_manager import TeamDataManager

            self._team_data_manager = TeamDataManager(self._client)
        return self._team_data_manager

    async def reset_team_data_manager(self, client: Optional[ScrimBot] = None) -> "TeamDataManager":
        """
        팀 데이터 매니저의 상태를 초기화합니다. 인스턴스는 유지되므로
        위임 모듈이 보관한 참조가 계속 유효합니다.
        기존 매니저의 백그라운드 태스크도 완전히 취소될 때까지 대기합니다.
        """
        if self._team_data_manager:
            try:
                await self._team_data_manager.reset_team_data()
            except Exception as exc:  # pragma: no cover - 최후 안전장치
                logger.warning(f"[봇관리] 팀 데이터 매니저 리셋 중 예외 무시: {exc}")
            self._team_data_manager.client = client or self._client
        else:
            from models.team_data_manager import TeamDataManager

            self._team_data_manager = TeamDataManager(client or self._client)

        if self._team_processor:
            try:
                self._team_processor.group_image_cache.clear()
                logger.debug("[봇관리] TeamProcessor 이미지 캐시 클리어 완료")
            except Exception as exc:
                logger.warning(f"[봇관리] 이미지 캐시 클리어 중 예외 무시: {exc}")

        return self._team_data_manager

    def get_schedule_manager(self) -> "ScheduleManager":
        if self._schedule_manager is None:
            from models.schedule_manager import ScheduleManager

            self._schedule_manager = ScheduleManager()
            self._schedule_manager.load_backup()
        return self._schedule_manager

    def get_warning_manager(self):
        if self._warning_manager is None:
            from models.warning_manager import WarningManager

            self._warning_manager = WarningManager()
        return self._warning_manager

    def get_team_processor(self) -> "TeamProcessor":
        """팀 프로세서 반환 (싱글톤)"""
        if self._team_processor is None:
            from models.team_processor import TeamProcessor

            self._team_processor = TeamProcessor(self._client, self.get_team_data_manager())
        else:
            if self._team_processor.client != self._client:
                self._team_processor.update_client(self._client)
        return self._team_processor

