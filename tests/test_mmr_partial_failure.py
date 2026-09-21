import asyncio
import unittest
from unittest import mock

from models.team_data import TeamData
from models.team_processor import TeamProcessor


def make_processor(mmr_by_nick: dict, test_accounts: dict = None) -> TeamProcessor:
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
            _, _, result = asyncio.run(proc.fetch_team_mmr('윌슨조아', team))
        return result

    def test_all_players_resolved_uses_top_three(self):
        result = self._fetch(self.FULL)
        self.assertTrue(result.confirmed)
        self.assertAlmostEqual(result.mmr, (8522 + 8297 + 7666) / 3, places=2)
        self.assertEqual(result.failed_players, ())

    def test_partial_failure_is_not_confirmed(self):
        partial = dict(self.FULL)
        partial['네무리히메'] = None
        result = self._fetch(partial)
        self.assertFalse(result.confirmed)
        self.assertEqual(result.failed_players, ('네무리히메',))

    def test_all_failure_is_not_confirmed(self):
        result = self._fetch({p: None for p in self.PLAYERS})
        self.assertFalse(result.confirmed)
        self.assertEqual(set(result.failed_players), set(self.PLAYERS))

    def test_zero_mmr_counts_as_a_real_value(self):
        zeroed = dict(self.FULL)
        zeroed['주사위의모험'] = 0
        result = self._fetch(zeroed)
        self.assertTrue(result.confirmed)
        self.assertAlmostEqual(result.mmr, (8522 + 8297 + 7666) / 3, places=2)

    def test_whole_team_at_zero_is_confirmed_not_failed(self):
        result = self._fetch({p: 0 for p in self.PLAYERS})
        self.assertTrue(result.confirmed)
        self.assertEqual(result.mmr, 0.0)
        self.assertEqual(result.failed_players, ())


class TestAccountZeroMMRTest(unittest.TestCase):
    PLAYERS = ['트수급백수', '이런법이어딨어', 'KCW', 'CNJTEST1']
    REAL = {'트수급백수': 8000, '이런법이어딨어': 7500, 'KCW': 7000}

    def _fetch(self, test_accounts, players=None):
        proc, fake_api = make_processor(dict(self.REAL), test_accounts)
        team = TeamData(name='CNJ', players=list(players or self.PLAYERS))
        with mock.patch('models.team_processor.BSERAPIClient', fake_api):
            _, _, result = asyncio.run(proc.fetch_team_mmr('CNJ', team))
        return result

    def test_sheet_zero_does_not_sink_the_team(self):
        result = self._fetch({'CNJTEST1': 0.0})
        self.assertTrue(result.confirmed)
        self.assertAlmostEqual(result.mmr, (8000 + 7500 + 7000) / 3, places=2)

    def test_sheet_zero_counts_in_a_three_player_team(self):
        result = self._fetch({'CNJTEST1': 0.0}, players=['트수급백수', '이런법이어딨어', 'CNJTEST1'])
        self.assertTrue(result.confirmed)
        self.assertAlmostEqual(result.mmr, (8000 + 7500 + 0) / 3, places=2)

    def test_unlisted_test_account_is_not_confirmed(self):
        result = self._fetch({})
        self.assertFalse(result.confirmed)
        self.assertEqual(result.failed_players, ('CNJTEST1',))

    def test_all_test_accounts_use_sheet_values(self):
        proc, fake_api = make_processor({}, {'T1': 8000.0, 'T2': 7000.0, 'T3': 0.0})
        team = TeamData(name='테스트팀', players=['T1', 'T2', 'T3'])
        with mock.patch('models.team_processor.BSERAPIClient', fake_api):
            _, _, result = asyncio.run(proc.fetch_team_mmr('테스트팀', team))
        self.assertTrue(result.confirmed)
        self.assertAlmostEqual(result.mmr, (8000 + 7000 + 0) / 3, places=2)

    def test_nicknames_outside_the_sheet_are_reported_as_failed(self):
        # 시트에 없는 닉네임은 테스트 계정이 아니라 일반 API 조회 대상
        proc, fake_api = make_processor({}, {'T1': 8000.0})
        team = TeamData(name='테스트팀', players=['T1', 'T2', 'T3'])
        with mock.patch('models.team_processor.BSERAPIClient', fake_api):
            _, _, result = asyncio.run(proc.fetch_team_mmr('테스트팀', team))
        self.assertFalse(result.confirmed)
        self.assertEqual(set(result.failed_players), {'T2', 'T3'})


class AssignmentMMRFallbackTest(unittest.TestCase):
    def test_fetch_all_falls_back_to_stored_mmr(self):
        proc, fake_api = make_processor({'A선수': None, 'B선수': 8000, 'C선수': None})
        proc.ensure_test_accounts_loaded = mock.AsyncMock(return_value=True)

        failed = TeamData(name='조회실패팀', players=['A선수'])
        failed.mmr = 8500.0
        failed.mmr_confirmed = True
        ok = TeamData(name='정상팀', players=['B선수'])
        ok.mmr = 7000.0
        ok.mmr_confirmed = True
        never = TeamData(name='미확정팀', players=['C선수'])

        with mock.patch('models.team_processor.BSERAPIClient', fake_api):
            info = asyncio.run(proc._fetch_all_team_mmr(
                {'조회실패팀': failed, '정상팀': ok, '미확정팀': never}
            ))

        by_name = {name: mmr for name, _, mmr in info}
        self.assertEqual(by_name['조회실패팀'], 8500.0)
        self.assertEqual(by_name['정상팀'], 8000.0)
        self.assertEqual(by_name['미확정팀'], 0.0)
        self.assertEqual([name for name, _, _ in info], ['조회실패팀', '정상팀', '미확정팀'])

    def test_confirmed_zero_team_keeps_its_zero_on_failure(self):
        proc, fake_api = make_processor({'D선수': None})
        proc.ensure_test_accounts_loaded = mock.AsyncMock(return_value=True)

        zero_team = TeamData(name='영점팀', players=['D선수'])
        zero_team.mmr = 0.0
        zero_team.mmr_confirmed = True

        with mock.patch('models.team_processor.BSERAPIClient', fake_api):
            info = asyncio.run(proc._fetch_all_team_mmr({'영점팀': zero_team}))

        self.assertEqual(info[0][2], 0.0)


if __name__ == '__main__':
    unittest.main()
