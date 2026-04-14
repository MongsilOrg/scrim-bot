"""UserTeamCache 단위 테스트"""
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta

import pytest

from models.user_team_cache import UserTeamCache

KST = timezone(timedelta(hours=9))


@pytest.fixture(autouse=True)
def reset_singleton():
    """테스트마다 싱글턴 리셋"""
    UserTeamCache._instance = None
    yield
    UserTeamCache._instance = None


@pytest.fixture
def cache(tmp_path):
    path = str(tmp_path / "user_team_cache.json")
    return UserTeamCache(cache_path=path)


class TestUserTeamCache:
    def test_get_empty(self, cache):
        """캐시 없을 때 None 반환"""
        assert cache.get("123") is None

    def test_set_and_get(self, cache):
        """저장 후 조회"""
        data = {
            "team_name": "TestTeam",
            "players": ["p1", "p2", "p3"],
            "staff": ["s1"],
        }
        cache.set("123", data)
        result = cache.get("123")
        assert result["team_name"] == "TestTeam"
        assert result["players"] == ["p1", "p2", "p3"]
        assert result["staff"] == ["s1"]
        assert "cached_at" in result

    def test_overwrite(self, cache):
        """같은 user_id로 덮어쓰기"""
        cache.set("123", {"team_name": "Old", "players": ["p1", "p2", "p3"], "staff": []})
        cache.set("123", {"team_name": "New", "players": ["p4", "p5", "p6"], "staff": []})
        result = cache.get("123")
        assert result["team_name"] == "New"
        assert result["players"] == ["p4", "p5", "p6"]

    def test_multiple_users(self, cache):
        """여러 유저 독립 저장"""
        cache.set("111", {"team_name": "A", "players": ["a1", "a2", "a3"], "staff": []})
        cache.set("222", {"team_name": "B", "players": ["b1", "b2", "b3"], "staff": []})
        assert cache.get("111")["team_name"] == "A"
        assert cache.get("222")["team_name"] == "B"

    def test_persistence(self, tmp_path):
        """파일 기반 영속성 - 새 인스턴스에서도 조회 가능"""
        path = str(tmp_path / "user_team_cache.json")
        cache1 = UserTeamCache(cache_path=path)
        cache1.set("123", {"team_name": "Persist", "players": ["p1", "p2", "p3"], "staff": []})

        cache2 = UserTeamCache(cache_path=path)
        result = cache2.get("123")
        assert result["team_name"] == "Persist"

    def test_corrupted_file(self, tmp_path):
        """손상된 파일 → 빈 캐시로 초기화"""
        path = str(tmp_path / "user_team_cache.json")
        with open(path, "w") as f:
            f.write("INVALID JSON{{{")
        cache = UserTeamCache(cache_path=path)
        assert cache.get("123") is None

    def test_missing_file(self, tmp_path):
        """파일 없음 → 빈 캐시로 시작"""
        path = str(tmp_path / "nonexistent.json")
        cache = UserTeamCache(cache_path=path)
        assert cache.get("999") is None

    def test_delete(self, cache):
        """캐시 삭제"""
        cache.set("123", {"team_name": "Del", "players": ["p1", "p2", "p3"], "staff": []})
        cache.delete("123")
        assert cache.get("123") is None

    def test_delete_nonexistent(self, cache):
        """없는 항목 삭제 시 에러 없음"""
        cache.delete("999")  # no error
