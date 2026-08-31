"""신청 완료 시 자동 저장, 다음 신청 시 프리필용 데이터 제공. user_id당 최근 1건만 유지 (덮어쓰기)."""
import json
import os
from datetime import datetime
from typing import Optional, Dict

from config.logging_config import get_logger
from utils.helpers import KST, save_json_atomic

logger = get_logger('user_team_cache')


class UserTeamCache:
    _instance: Optional["UserTeamCache"] = None

    def __new__(cls, cache_path: str = "data/user_team_cache.json"):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, cache_path: str = "data/user_team_cache.json"):
        if hasattr(self, "_initialized") and self._initialized:
            return
        self._cache_path = cache_path
        self._data: Dict[str, dict] = {}
        self._load()
        self._initialized = True

    def _load(self) -> None:
        if not os.path.exists(self._cache_path):
            return
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[캐시] 캐시 파일 로드 실패, 초기화: {e}")
            self._data = {}

    def _save(self) -> None:
        try:
            save_json_atomic(self._cache_path, self._data, indent=2)
        except OSError as e:
            logger.error(f"[캐시] 캐시 파일 저장 실패: {e}")

    def get(self, user_id: str) -> Optional[dict]:
        return self._data.get(user_id)

    def set(self, user_id: str, data: dict) -> None:
        self._data[user_id] = {
            "team_name": data["team_name"],
            "players": list(data["players"]),
            "staff": list(data.get("staff", [])),
            "cached_at": datetime.now(KST).isoformat(),
        }
        self._save()

    def delete(self, user_id: str) -> None:
        self._data.pop(user_id, None)
        self._save()
