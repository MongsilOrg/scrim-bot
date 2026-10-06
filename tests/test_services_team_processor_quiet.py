import asyncio
import unittest
from unittest import mock

from models.team_data import TeamData
from models.team_processor import TeamProcessor


def make_processor():
    proc = TeamProcessor.__new__(TeamProcessor)
    proc.test_accounts_data = {}
    proc._test_accounts_by_key = {}
    return proc


class FailingAPI:
    def __init__(self, quiet):
        self.quiet = quiet

    async def get_user_uid(self, nickname):
        return None

    async def get_user_mmr(self, uid):
        return None


class QuietFetchTest(unittest.TestCase):
    def _fetch(self, quiet, team_name):
        team = TeamData(name=team_name, players=[f'{team_name}1', f'{team_name}2', f'{team_name}3'])
        with self.assertLogs('scrim-bot.team_processor', level='DEBUG') as logs:
            asyncio.run(make_processor().fetch_team_mmr(team_name, team, api_client=FailingAPI(quiet)))
        return {r.levelname for r in logs.records}

    def test_quiet_cycle_logs_debug_only(self):
        self.assertEqual(self._fetch(True, '점검팀'), {'DEBUG'})

    def test_normal_cycle_still_warns(self):
        self.assertIn('WARNING', self._fetch(False, '평상팀'))


if __name__ == '__main__':
    unittest.main()
