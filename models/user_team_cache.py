"""
유저별 팀 신청 캐시

신청 완료 시 자동 저장, 다음 신청 시 프리필용 데이터 제공.
user_id당 최근 1건만 유지 (덮어쓰기).
"""
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

from config.logging_config import get_logger

logger = get_logger('user_team_cache')

KST = timezone(timedelta(hours=9))


class UserTeamCache:
    """유저별 최근 신청 데이터 캐시"""

    def __init__(self, cache_path: str = "data/user_team_cache.json"):
        self._cache_path = cache_path
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        """파일에서 캐시 로드. 실패 시 빈 dict."""
        if not os.path.exists(self._cache_path):
            return
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[캐시] 캐시 파일 로드 실패, 초기화: {e}")
            self._data = {}

    def _save(self) -> None:
        """캐시를 파일에 저장."""
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"[캐시] 캐시 파일 저장 실패: {e}")

    def get(self, user_id: str) -> Optional[dict]:
        """user_id로 캐시 조회. 없으면 None."""
        return self._data.get(user_id)

    def set(self, user_id: str, data: dict) -> None:
        """캐시 저장 (덮어쓰기). cached_at 자동 추가."""
        self._data[user_id] = {
            "team_name": data["team_name"],
            "players": list(data["players"]),
            "staff": list(data.get("staff", [])),
            "cached_at": datetime.now(KST).isoformat(),
        }
        self._save()

    def delete(self, user_id: str) -> None:
        """캐시 삭제."""
        self._data.pop(user_id, None)
        self._save()
