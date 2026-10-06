import unittest
from types import SimpleNamespace
from unittest import mock

from models import mmr_updater
from models.mmr_updater import MmrUpdater
from models.team_data import TeamData
from services.bser_api import UID_ERROR, UID_FOUND, UID_NOT_FOUND


class FakeAPI:
    def __init__(self, statuses, maintenance=False):
        self.statuses = statuses
        self.maintenance = maintenance
        self.maintenance_calls = 0

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def lookup_user_uid(self, nickname):
        status = self.statuses.get(nickname, UID_FOUND)
        return status, ('uid' if status == UID_FOUND else None)

    async def check_server_maintenance(self):
        self.maintenance_calls += 1
        return self.maintenance


class VerifyUnverifiedTeamsTest(unittest.IsolatedAsyncioTestCase):
    def _setup(self, api):
        team = TeamData(name='팀', players=['가', '나', '다'], user_id='42')
        self.mgr = SimpleNamespace(
            unverified_teams={'팀'}, teams={'팀': team},
            set_team_mmr=mock.AsyncMock(),
        )
        self.mgr.clear_unverified = self.mgr.unverified_teams.discard
        self.user = mock.MagicMock()
        self.user.send = mock.AsyncMock()
        self.log_channel = mock.MagicMock()
        self.log_channel.send = mock.AsyncMock()
        self.mgr.client = mock.MagicMock()
        self.mgr.client.get_user.return_value = self.user
        self.mgr.client.get_channel.return_value = self.log_channel

        self.tp = mock.MagicMock()
        self.tp.ensure_test_accounts_loaded = mock.AsyncMock()
        self.tp.is_test_account.return_value = False
        self.tp.fetch_team_mmr = mock.AsyncMock(return_value=('팀', team, 7123.456))

        patches = [
            mock.patch.object(mmr_updater, 'BSERAPIClient', api),
            mock.patch.object(mmr_updater, 'BotManager'),
        ]
        bm = patches[1].start()
        patches[0].start()
        for p in patches:
            self.addCleanup(p.stop)
        bm.get_instance.return_value.get_team_processor.return_value = self.tp

    async def _run(self):
        await MmrUpdater(self.mgr).verify_unverified_teams()

    def _dm_text(self):
        view = self.user.send.await_args.kwargs['view']
        return view.children[0].children[0].content

    async def test_lookup_error_defers(self):
        self._setup(FakeAPI({'나': UID_ERROR}))
        await self._run()
        self.assertEqual(self.mgr.unverified_teams, {'팀'})
        self.user.send.assert_not_awaited()
        self.log_channel.send.assert_not_awaited()

    async def test_404_during_maintenance_defers(self):
        api = FakeAPI({'나': UID_NOT_FOUND}, maintenance=True)
        self._setup(api)
        await self._run()
        self.assertEqual(api.maintenance_calls, 1)
        self.assertEqual(self.mgr.unverified_teams, {'팀'})
        self.user.send.assert_not_awaited()

    async def test_confirmed_404_notifies_applicant_and_log_channel(self):
        self._setup(FakeAPI({'나': UID_NOT_FOUND}))
        await self._run()
        self.assertEqual(self.mgr.unverified_teams, set())
        text = self._dm_text()
        self.assertIn('**신청/수정** 버튼으로', text)
        self.assertIn(f'{mmr_updater.settings.TEAM_REGISTRATION_DEADLINE_HOUR}시 전에 고쳐주세요.', text)
        self.mgr.client.get_channel.assert_called_with(mmr_updater.settings.LOG_CHANNEL_ID)
        log_text = self.log_channel.send.await_args.args[0]
        self.assertIn('나', log_text)
        self.assertIn('<@42>', log_text)
        self.tp.fetch_team_mmr.assert_not_awaited()

    async def test_all_found_reports_mmr_with_two_decimals(self):
        self._setup(FakeAPI({}))

        async def set_mmr(name, mmr):
            self.mgr.teams[name].mmr = mmr
        self.mgr.set_team_mmr = mock.AsyncMock(side_effect=set_mmr)

        await self._run()
        self.assertEqual(self.mgr.unverified_teams, set())
        self.assertIn('MMR: **7123.46**', self._dm_text())
        self.log_channel.send.assert_not_awaited()

    async def test_failed_mmr_fetch_does_not_show_stale_value(self):
        self._setup(FakeAPI({}))
        self.mgr.teams['팀'].mmr = 6000.0
        self.tp.fetch_team_mmr.return_value = ('팀', self.mgr.teams['팀'], 0.0)
        await self._run()
        text = self._dm_text()
        self.assertNotIn('6000', text)
        self.assertIn('다음 갱신 때 반영', text)


if __name__ == '__main__':
    unittest.main()
