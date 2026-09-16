import pytest

from models.user_team_cache import UserTeamCache


@pytest.fixture(autouse=True)
def reset_singleton():
    UserTeamCache._instance = None
    yield
    UserTeamCache._instance = None


class TestUserTeamCache:
    def test_persistence(self, tmp_path):
        path = str(tmp_path / "user_team_cache.json")
        cache1 = UserTeamCache(cache_path=path)
        cache1.set("123", {"team_name": "Persist", "players": ["p1", "p2", "p3"], "staff": []})

        cache2 = UserTeamCache(cache_path=path)
        result = cache2.get("123")
        assert result["team_name"] == "Persist"

    def test_corrupted_file(self, tmp_path):
        path = str(tmp_path / "user_team_cache.json")
        with open(path, "w") as f:
            f.write("INVALID JSON{{{")
        cache = UserTeamCache(cache_path=path)
        assert cache.get("123") is None

    def test_missing_file(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        cache = UserTeamCache(cache_path=path)
        assert cache.get("999") is None
