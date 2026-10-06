import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from models.team_data import TeamData
from utils import scrim_history


class _FakeAPI:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_user_uid(self, name):
        return f"uid-{name}"

    async def get_user_mmr(self, uid):
        return 9000.0


class ScrimHistoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_roster_change_keeps_reserve_and_adds_discord_ids(self):
        path = os.path.join(tempfile.mkdtemp(), "history.jsonl")
        guild = SimpleNamespace(members=[SimpleNamespace(id=7, display_name="대타", global_name=None, name="sub")])
        team = TeamData(name="A팀", players=["p1", "p2", "p3"], user_id="1")
        reserve = TeamData(name="B팀", players=["q1", "q2", "q3"])
        with patch.object(scrim_history, "HISTORY_FILE", path), patch.object(scrim_history, "BSERAPIClient", _FakeAPI):
            await scrim_history.record_assignment([[("A팀", team, 9000)]], [("B팀", reserve, 8000)], registered=2, guild=guild)
            team.players = ["p1", "p2", "대타"]
            await scrim_history.record_assignment([[("A팀", team, 9000)]], None, registered=None, guild=guild,
                                                  source="roster_change", changed_team="A팀")
        first, second = [json.loads(line) for line in open(path, encoding="utf-8")]
        self.assertEqual(second["source"], "roster_change")
        self.assertEqual(second["changed_team"], "A팀")
        self.assertEqual(second["registered"], 2)
        self.assertEqual(second["reserve"], first["reserve"])
        sub = second["groups"][0][0]["players"][2]
        self.assertEqual((sub["name"], sub["discord_id"]), ("대타", "7"))


if __name__ == "__main__":
    unittest.main()
