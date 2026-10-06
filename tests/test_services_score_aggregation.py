import unittest
from types import SimpleNamespace

from config import logging_config
from services import score_aggregation

HEADER = 'teamName,tournament total score,tournament kill score,gameId,nickname,character\n'


def attachment(att_id, filename, body):
    async def read():
        return body.encode('utf-8')
    return SimpleNamespace(id=att_id, filename=filename, read=read)


class FakeChannel:
    def __init__(self, attachments):
        self.messages = [SimpleNamespace(attachments=[a]) for a in attachments]

    def history(self, **kwargs):
        async def gen():
            for message in self.messages:
                yield message
        return gen()


class CsvDedupTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging_config._log_once_at.clear()

    async def test_same_game_id_is_counted_once(self):
        round1 = HEADER + 'A,10,3,111,a1,x\nB,8,2,111,b1,y\n'
        round2 = HEADER + 'A,5,1,222,a1,x\nB,9,4,222,b1,y\n'
        channel = FakeChannel([
            attachment(1, 'r1.csv', round1),
            attachment(2, 'r1-again.csv', round1),
            attachment(3, 'r2.csv', round2),
        ])

        rows = await score_aggregation.collect_today_csv_data(channel, None)
        self.assertEqual([(gid, name) for gid, _, name in rows], [(111, 'r1.csv'), (222, 'r2.csv')])

        scores = {t['teamName']: t['tournament total score'] for t in score_aggregation.aggregate_team_scores(rows)}
        self.assertEqual(scores, {'A': 15.0, 'B': 17.0})

    async def test_missing_required_column_is_warned(self):
        channel = FakeChannel([attachment(10, 'bad.csv', 'teamName,gameId\nA,1\n')])
        with self.assertLogs('scrim-bot.score_aggregation', level='WARNING') as logs:
            rows = await score_aggregation.collect_today_csv_data(channel, None)
        self.assertEqual(rows, [])
        self.assertIn('bad.csv', logs.output[0])


class SilentColumnSkipTest(unittest.TestCase):
    def setUp(self):
        logging_config._log_once_at.clear()

    def test_default_team_name_without_nickname_column_is_warned(self):
        import pandas as pd
        df = pd.DataFrame({'teamName': ['Team 3', 'B'], 'tournament total score': [1, 2],
                           'tournament kill score': [0, 0]})
        with self.assertLogs('scrim-bot.score_aggregation', level='WARNING') as logs:
            score_aggregation._resolve_default_team_names(df, [{'B': {'b'}}])
        self.assertIn('Team 3', logs.output[0])

    def test_ban_list_without_character_column_is_warned(self):
        import pandas as pd
        with self.assertLogs('scrim-bot.score_aggregation', level='WARNING'):
            self.assertEqual(score_aggregation._extract_ban_list(pd.DataFrame({'teamName': ['A']})), [])


if __name__ == '__main__':
    unittest.main()
