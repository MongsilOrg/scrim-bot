import unittest
from unittest import mock

import pandas as pd

from services import score_aggregation
from utils.validators import normalize_team_name


class EventsLogicTest(unittest.TestCase):
    def test_is_csv_filename(self):
        self.assertTrue(score_aggregation.is_csv_filename("result.csv"))
        self.assertTrue(score_aggregation.is_csv_filename("RESULT.CSV"))
        self.assertFalse(score_aggregation.is_csv_filename("result.txt"))

    def test_extract_game_id(self):
        df_valid = pd.DataFrame({"gameId": ["12345"]})
        df_invalid = pd.DataFrame({"gameId": ["abc"]})
        df_empty = pd.DataFrame({"gameId": []})

        self.assertEqual(score_aggregation._extract_game_id(df_valid, "ok.csv"), 12345)
        self.assertIsNone(score_aggregation._extract_game_id(df_invalid, "invalid.csv"))
        self.assertIsNone(score_aggregation._extract_game_id(df_empty, "empty.csv"))

    def test_aggregate_team_scores(self):
        round1 = pd.DataFrame(
            {
                "teamName": ["A", "A", "B"],
                "tournament total score": [10, 12, 9],
                "tournament kill score": [4, 5, 3],
            }
        )
        round2 = pd.DataFrame(
            {
                "teamName": ["A", "B", "B"],
                "tournament total score": [8, 7, 11],
                "tournament kill score": [2, 1, 4],
            }
        )

        csv_rows = [(1001, round1, "r1.csv"), (1002, round2, "r2.csv")]
        team_data = score_aggregation.aggregate_team_scores(csv_rows)

        self.assertEqual(len(team_data), 2)

        # A와 B가 동점이라 순위 배정 순서 미정
        score_map = {
            item["teamName"]: (
                item["tournament total score"],
                item["tournament kill score"],
                item["rank"],
            )
            for item in team_data
        }

        self.assertEqual(score_map["A"][0], 20.0)
        self.assertEqual(score_map["A"][1], 7.0)
        self.assertEqual(score_map["B"][0], 20.0)
        self.assertEqual(score_map["B"][1], 7.0)

        ranks = sorted(item["rank"] for item in team_data)
        self.assertEqual(ranks, [1, 2])

    def test_extract_ban_list(self):
        df = pd.DataFrame(
            {
                "character": [
                    "Aya",
                    "Aya",
                    "Aya",
                    "Jackie",
                    "Jackie",
                    "Nadine",
                    "",
                    None,
                ]
            }
        )

        ban_list = score_aggregation._extract_ban_list(df)
        self.assertEqual(ban_list, ["Aya"])
        self.assertEqual(score_aggregation._extract_ban_list(None), [])

    def test_extract_ban_list_missing_column(self):
        df = pd.DataFrame({"teamName": ["A", "B"]})
        self.assertEqual(score_aggregation._extract_ban_list(df), [])

    def test_normalize_team_name(self):
        self.assertEqual(normalize_team_name(" DM "), normalize_team_name("dm"))
        self.assertEqual(normalize_team_name("Team  Alpha"), normalize_team_name("team alpha"))
        self.assertNotEqual(normalize_team_name("DM"), normalize_team_name("VGX"))
        self.assertEqual(normalize_team_name(""), "")

    def test_aggregate_team_scores_normalizes_team_names(self):
        round1 = pd.DataFrame(
            {
                "teamName": ["DM ", "DM ", "VGX"],
                "tournament total score": [10, 12, 9],
                "tournament kill score": [4, 5, 3],
            }
        )
        round2 = pd.DataFrame(
            {
                "teamName": ["dm", "dm", "vgx "],
                "tournament total score": [8, 7, 11],
                "tournament kill score": [2, 1, 4],
            }
        )

        csv_rows = [(1001, round1, "r1.csv"), (1002, round2, "r2.csv")]
        team_data = score_aggregation.aggregate_team_scores(csv_rows)

        self.assertEqual(len(team_data), 2)
        score_map = {item["teamName"]: item for item in team_data}
        self.assertIn("DM", score_map)
        self.assertIn("VGX", score_map)
        self.assertEqual(score_map["DM"]["tournament total score"], 20.0)
        self.assertEqual(score_map["VGX"]["tournament total score"], 20.0)

    def test_aggregate_team_scores_default_team_name_resolved(self):
        round1 = pd.DataFrame(
            {
                "teamName": ["DM", "DM", "DM", "VGX", "VGX", "VGX"],
                "nickname": ["player1", "player2", "player3", "player4", "player5", "player6"],
                "tournament total score": [10, 10, 10, 9, 9, 9],
                "tournament kill score": [4, 4, 4, 3, 3, 3],
            }
        )
        round2 = pd.DataFrame(
            {
                "teamName": ["Team 1", "Team 1", "Team 1", "VGX", "VGX", "VGX"],
                "nickname": ["player1", "player2", "player3", "player4", "player5", "player6"],
                "tournament total score": [8, 8, 8, 11, 11, 11],
                "tournament kill score": [2, 2, 2, 4, 4, 4],
            }
        )

        csv_rows = [(1001, round1, "r1.csv"), (1002, round2, "r2.csv")]
        team_data = score_aggregation.aggregate_team_scores(csv_rows)

        self.assertEqual(len(team_data), 2)
        team_names = {item["teamName"] for item in team_data}
        self.assertIn("DM", team_names)
        self.assertNotIn("Team 1", team_names)

    def test_aggregate_team_scores_default_team_not_resolved_single_match(self):
        round1 = pd.DataFrame(
            {
                "teamName": ["DM", "DM", "DM"],
                "nickname": ["player1", "player2", "player3"],
                "tournament total score": [10, 10, 10],
                "tournament kill score": [4, 4, 4],
            }
        )
        round2 = pd.DataFrame(
            {
                "teamName": ["Team 1", "Team 1", "Team 1"],
                "nickname": ["player1", "newplayer1", "newplayer2"],
                "tournament total score": [8, 8, 8],
                "tournament kill score": [2, 2, 2],
            }
        )

        csv_rows = [(1001, round1, "r1.csv"), (1002, round2, "r2.csv")]
        team_data = score_aggregation.aggregate_team_scores(csv_rows)

        team_names = {item["teamName"] for item in team_data}
        self.assertIn("Team 1", team_names)
        self.assertIn("DM", team_names)

    def test_is_default_team_name(self):
        self.assertTrue(score_aggregation._is_default_team_name("Team 1"))
        self.assertTrue(score_aggregation._is_default_team_name("team 8"))
        self.assertTrue(score_aggregation._is_default_team_name("TEAM3"))
        self.assertTrue(score_aggregation._is_default_team_name(" Team 5 "))
        self.assertFalse(score_aggregation._is_default_team_name("DM"))
        self.assertFalse(score_aggregation._is_default_team_name("TeamAlpha"))


class ComputeBanListForChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_csv_returns_empty(self):
        async def fake_collect(channel, start_utc, limit=200):
            return []

        with mock.patch.object(score_aggregation, "collect_today_csv_data", fake_collect):
            result = await score_aggregation.compute_ban_list_for_channel(object())
        self.assertEqual(result, [])

    async def test_uses_latest_round_by_game_id(self):
        r1 = pd.DataFrame({"character": ["Aya", "Aya", "Aya"]})
        r2 = pd.DataFrame({"character": ["Hart", "Hart", "Hart"]})
        # 일부러 gameId 역순으로 둔 입력
        rows = [(1002, r2, "r2.csv"), (1001, r1, "r1.csv")]

        async def fake_collect(channel, start_utc, limit=200):
            return rows

        with mock.patch.object(score_aggregation, "collect_today_csv_data", fake_collect):
            result = await score_aggregation.compute_ban_list_for_channel(object())
        self.assertEqual(result, ["Hart"])


if __name__ == "__main__":
    unittest.main()
