"""팀 MMR 집계의 부분 실패 처리 테스트."""
import asyncio
import unittest
from unittest import mock

from models.team_data import TeamData
from models.team_processor import TeamProcessor


def make_processor(mmr_by_nick: dict, test_accounts: dict = None) -> TeamProcessor:
    """API/시트 초기화를 건너뛴 프로세서."""
    proc = TeamProcessor.__new__(TeamProcessor)
    proc.test_accounts_data = dict(test_accounts or {})
    proc._test_accounts_by_key = {k.lower(): v for k, v in (test_accounts or {}).items()}
    proc._test_accounts_loaded_at = 0.0
    proc._test_accounts_attempted_at = 0.0
    proc.seeds_data = None
    proc._seeds_loaded_at = 0.0

    class FakeAPI:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get_user_uid(self, nickname):
            return f"uid:{nickname}"

        async def get_user_mmr(self, uid):
            return mmr_by_nick.get(uid.split(":", 1)[1])

    return proc, FakeAPI


class TeamMMRPartialFailureTest(unittest.TestCase):
    PLAYERS = ['고통의삶', '네무리히메', '적을찾는피5라', '주사위의모험']
    FULL = {'고통의삶': 8297, '네무리히메': 8522, '적을찾는피5라': 7666, '주사위의모험': 7130}

    def _fetch(self, mmr_by_nick, players=None, test_accounts=None):
        proc, fake_api = make_processor(mmr_by_nick, test_accounts)
        team = TeamData(name='윌슨조아', players=list(players or self.PLAYERS))
        with mock.patch('models.team_processor.BSERAPIClient', fake_api):
            _, _, mmr = asyncio.run(proc.fetch_team_mmr('윌슨조아', team))
        return mmr

    def test_all_players_resolved_uses_top_three(self):
        self.assertAlmostEqual(self._fetch(self.FULL), (8522 + 8297 + 7666) / 3, places=2)

    def test_partial_failure_is_not_confirmed(self):
        partial = dict(self.FULL)
        partial['네무리히메'] = None
        self.assertEqual(self._fetch(partial), 0.0)

    def test_all_failure_is_not_confirmed(self):
        self.assertEqual(self._fetch({p: None for p in self.PLAYERS}), 0.0)

    def test_zero_mmr_counts_as_a_real_value(self):
        zeroed = dict(self.FULL)
        zeroed['주사위의모험'] = 0
        self.assertAlmostEqual(self._fetch(zeroed), (8522 + 8297 + 7666) / 3, places=2)


class TestAccountZeroMMRTest(unittest.TestCase):
    """시트 0점은 값, 시트에 없으면 미확정."""

    PLAYERS = ['트수급백수', '이런법이어딨어', 'KCW', 'CNJTEST1']
    REAL = {'트수급백수': 8000, '이런법이어딨어': 7500, 'KCW': 7000}

    def _fetch(self, test_accounts, players=None):
        proc, fake_api = make_processor(dict(self.REAL), test_accounts)
        team = TeamData(name='CNJ', players=list(players or self.PLAYERS))
        with mock.patch('models.team_processor.BSERAPIClient', fake_api):
            _, _, mmr = asyncio.run(proc.fetch_team_mmr('CNJ', team))
        return mmr

    def test_sheet_zero_does_not_sink_the_team(self):
        self.assertAlmostEqual(self._fetch({'CNJTEST1': 0.0}), (8000 + 7500 + 7000) / 3, places=2)

    def test_sheet_zero_counts_in_a_three_player_team(self):
        mmr = self._fetch({'CNJTEST1': 0.0}, players=['트수급백수', '이런법이어딨어', 'CNJTEST1'])
        self.assertAlmostEqual(mmr, (8000 + 7500 + 0) / 3, places=2)

    def test_unlisted_test_account_is_not_confirmed(self):
        self.assertEqual(self._fetch({}), 0.0)

    def test_all_test_accounts_use_sheet_values(self):
        proc, fake_api = make_processor({}, {'T1': 8000.0, 'T2': 7000.0, 'T3': 0.0})
        team = TeamData(name='테스트팀', players=['T1', 'T2', 'T3'])
        with mock.patch('models.team_processor.BSERAPIClient', fake_api):
            _, _, mmr = asyncio.run(proc.fetch_team_mmr('테스트팀', team))
        self.assertAlmostEqual(mmr, (8000 + 7000 + 0) / 3, places=2)


class AssignmentMMRFallbackTest(unittest.TestCase):
    """조회 실패 시 마지막 확정값 사용."""

    def test_fetch_all_falls_back_to_stored_mmr(self):
        proc, fake_api = make_processor({'A선수': None, 'B선수': 8000})
        proc.ensure_test_accounts_loaded = mock.AsyncMock(return_value=True)

        failed = TeamData(name='조회실패팀', players=['A선수'])
        failed.mmr = 8500.0
        ok = TeamData(name='정상팀', players=['B선수'])
        ok.mmr = 7000.0

        with mock.patch('models.team_processor.BSERAPIClient', fake_api):
            info = asyncio.run(proc._fetch_all_team_mmr({'조회실패팀': failed, '정상팀': ok}))

        by_name = {name: mmr for name, _, mmr in info}
        self.assertEqual(by_name['조회실패팀'], 8500.0)
        self.assertEqual(by_name['정상팀'], 8000.0)
        self.assertEqual([name for name, _, _ in info], ['조회실패팀', '정상팀'])


if __name__ == '__main__':
    unittest.main()
