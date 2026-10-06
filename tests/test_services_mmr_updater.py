import io
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from models import mmr_updater
from models.mmr_updater import MmrUpdater
from models.team_data import TeamData
from services.bser_api import UID_ERROR, UID_FOUND, UID_NOT_FOUND

SERVER_INFO = {'operate': 'Live 서버', 'is_tournament': False}


class MmrMessageResendTest(unittest.IsolatedAsyncioTestCase):
    async def test_resend_after_failed_edit_keeps_image(self):
        async def failing_edit(**kwargs):
            kwargs['attachments'][0].fp.read()
            raise discord.HTTPException(mock.MagicMock(status=500, reason='err'), 'edit failed')

        old_message = mock.MagicMock()
        old_message.edit = mock.AsyncMock(side_effect=failing_edit)
        old_message.delete = mock.AsyncMock()
        mgr = SimpleNamespace(
            is_team_assignment_started=False, teams={}, unverified_teams=set(),
            _last_success_time='12:00', is_maintenance=False,
            mmr_message=old_message, mmr_message_id=1, _mmr_dirty=True,
            save_backup=mock.MagicMock(),
        )
        channel = mock.MagicMock()
        channel.send = mock.AsyncMock(return_value=SimpleNamespace(id=2))

        with mock.patch.object(mmr_updater, 'get_server_info', return_value=SERVER_INFO), \
             mock.patch.object(mmr_updater, 'BotManager'), \
             mock.patch.object(mmr_updater.ImageGenerator, 'generate_mmr_image_async',
                               mock.AsyncMock(return_value=io.BytesIO(b'png-bytes'))):
            await MmrUpdater(mgr)._render_mmr_message(channel, 0)

        sent_file = channel.send.await_args.kwargs['file']
        self.assertEqual(sent_file.fp.read(), b'png-bytes')
        self.assertEqual(mgr.mmr_message_id, 2)


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


class QuietCycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_loop_passes_maintenance_state_as_quiet(self):
        mgr = SimpleNamespace(teams={}, save_backup=mock.MagicMock())
        captured = {}

        class CapturingClient:
            def __init__(self, *, quiet=False):
                captured['quiet'] = quiet

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        team = TeamData(name='팀', players=['가'])
        mgr.teams = {'팀': team}
        mgr.set_team_mmr = mock.AsyncMock()
        tp = mock.MagicMock()
        tp.ensure_test_accounts_loaded = mock.AsyncMock()
        tp.fetch_team_mmr = mock.AsyncMock(return_value=('팀', team, 0.0))
        with mock.patch.object(mmr_updater, 'BSERAPIClient', CapturingClient), \
             mock.patch.object(mmr_updater, 'BotManager') as bm:
            bm.get_instance.return_value.get_team_processor.return_value = tp
            await MmrUpdater(mgr).update_all_team_mmr(quiet=True)
        self.assertTrue(captured['quiet'])


if __name__ == '__main__':
    unittest.main()
