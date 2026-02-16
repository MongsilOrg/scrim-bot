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


if __name__ == "__main__":
    unittest.main()
