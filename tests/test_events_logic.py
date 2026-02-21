import unittest
from types import SimpleNamespace

import pandas as pd

from bot import events


class EventsLogicTest(unittest.TestCase):
    def test_is_csv_filename(self):
        self.assertTrue(events._is_csv_filename("result.csv"))
        self.assertTrue(events._is_csv_filename("RESULT.CSV"))
        self.assertFalse(events._is_csv_filename("result.txt"))

    def test_should_process_message(self):
        bot_author = SimpleNamespace(bot=True)
        user_author = SimpleNamespace(bot=False)
        csv_attachment = SimpleNamespace(filename="a.csv")
        txt_attachment = SimpleNamespace(filename="a.txt")

        msg_bot = SimpleNamespace(author=bot_author, attachments=[csv_attachment])
        msg_no_csv = SimpleNamespace(author=user_author, attachments=[txt_attachment])
        msg_ok = SimpleNamespace(author=user_author, attachments=[csv_attachment])

        self.assertFalse(events._should_process_message(msg_bot))
        self.assertFalse(events._should_process_message(msg_no_csv))
        self.assertTrue(events._should_process_message(msg_ok))

    def test_extract_game_id(self):
        df_valid = pd.DataFrame({"gameId": ["12345"]})
        df_invalid = pd.DataFrame({"gameId": ["abc"]})
        df_empty = pd.DataFrame({"gameId": []})

        self.assertEqual(events._extract_game_id(df_valid, "ok.csv"), 12345)
        self.assertIsNone(events._extract_game_id(df_invalid, "invalid.csv"))
        self.assertIsNone(events._extract_game_id(df_empty, "empty.csv"))

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
        team_data, last_df = events._aggregate_team_scores(csv_rows)

        self.assertIsNotNone(last_df)
        self.assertEqual(len(team_data), 2)

        # A: 12 + 8 = 20, kill 5 + 2 = 7
        # B: 9 + 11 = 20, kill 3 + 4 = 7 -> 동점, 입력 순서 유지 가능
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
                ]
            }
        )

        ban_list = events._extract_ban_list(df)
        self.assertEqual(ban_list, ["Aya"])
        self.assertEqual(events._extract_ban_list(None), [])

    def test_build_gameid_embed(self):
        rows = [
            (111, pd.DataFrame({"gameId": [111]}), "r1.csv"),
            (222, pd.DataFrame({"gameId": [222]}), "r2.csv"),
        ]
        embed = events._build_gameid_embed(rows, "A조", "02월 16일")
        self.assertIn("GameId 정보", embed.title)
        self.assertIn("**1R**: `111`", embed.description)
        self.assertIn("**2R**: `222`", embed.description)


    def test_aggregate_team_scores_case_insensitive(self):
        """대소문자가 다른 팀명을 같은 팀으로 집계합니다."""
        round1 = pd.DataFrame(
            {
                "teamName": ["DM", "DM", "VGX"],
                "tournament total score": [10, 12, 9],
                "tournament kill score": [4, 5, 3],
            }
        )
        round2 = pd.DataFrame(
            {
                "teamName": ["dm", "dm", "vgx"],
                "tournament total score": [8, 7, 11],
                "tournament kill score": [2, 1, 4],
            }
        )

        csv_rows = [(1001, round1, "r1.csv"), (1002, round2, "r2.csv")]
        team_data, _ = events._aggregate_team_scores(csv_rows)

        self.assertEqual(len(team_data), 2)
        score_map = {item["teamName"]: item for item in team_data}
        # 최초 등장한 원본 팀명("DM", "VGX")이 표시명으로 유지
        self.assertIn("DM", score_map)
        self.assertIn("VGX", score_map)
        self.assertEqual(score_map["DM"]["tournament total score"], 20.0)
        self.assertEqual(score_map["VGX"]["tournament total score"], 20.0)

    def test_aggregate_team_scores_trailing_space(self):
        """trailing space가 있는 팀명을 같은 팀으로 집계합니다."""
        round1 = pd.DataFrame(
            {
                "teamName": ["DM ", "DM ", "VGX"],
                "tournament total score": [10, 12, 9],
                "tournament kill score": [4, 5, 3],
            }
        )
        round2 = pd.DataFrame(
            {
                "teamName": ["DM", "DM", "VGX "],
                "tournament total score": [8, 7, 11],
                "tournament kill score": [2, 1, 4],
            }
        )

        csv_rows = [(1001, round1, "r1.csv"), (1002, round2, "r2.csv")]
        team_data, _ = events._aggregate_team_scores(csv_rows)

        self.assertEqual(len(team_data), 2)
        score_map = {item["teamName"]: item for item in team_data}
        self.assertIn("DM", score_map)
        self.assertEqual(score_map["DM"]["tournament total score"], 20.0)

    def test_aggregate_team_scores_default_team_name_resolved(self):
        """기본 팀명(Team 1)이 이전 라운드 닉네임 기반으로 실제 팀명으로 치환됩니다."""
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
        team_data, _ = events._aggregate_team_scores(csv_rows)

        self.assertEqual(len(team_data), 2)
        team_names = {item["teamName"] for item in team_data}
        # "Team 1"이 "DM"으로 치환되어야 함
        self.assertIn("DM", team_names)
        self.assertNotIn("Team 1", team_names)

    def test_aggregate_team_scores_default_team_not_resolved_single_match(self):
        """닉네임 1명만 일치할 경우 기본 팀명이 치환되지 않습니다."""
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
        team_data, _ = events._aggregate_team_scores(csv_rows)

        team_names = {item["teamName"] for item in team_data}
        # 1명만 일치하므로 치환되지 않아야 함
        self.assertIn("Team 1", team_names)
        self.assertIn("DM", team_names)

    def test_is_default_team_name(self):
        self.assertTrue(events._is_default_team_name("Team 1"))
        self.assertTrue(events._is_default_team_name("team 8"))
        self.assertTrue(events._is_default_team_name("TEAM3"))
        self.assertTrue(events._is_default_team_name(" Team 5 "))
        self.assertFalse(events._is_default_team_name("DM"))
        self.assertFalse(events._is_default_team_name("TeamAlpha"))


if __name__ == "__main__":
    unittest.main()
