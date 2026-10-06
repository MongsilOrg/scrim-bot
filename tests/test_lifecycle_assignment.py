import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from config.settings import settings
from models import scrim_orchestrator
from models.team_data import TeamData
from models.team_data_manager import TeamDataManager
from utils.helpers import KST

GROUP_CHANNELS = {'A': 111, 'B': 222}
SCRIM_CHANNEL = 900


def _kst(hour, minute=0):
    return datetime(2026, 10, 6, hour, minute, tzinfo=KST)


class FakeService:
    def __init__(self, mgr, fail_letters=(), fail_roles=False):
        self.mgr = mgr
        self.fail_letters = set(fail_letters)
        self.fail_roles = fail_roles
        self.calls = []

    async def send_global_announcement(self, guild, groups, unmatched):
        self.calls.append(('global', [name for name, _, _ in unmatched]))

    async def handle_discord_roles(self, guild, groups):
        self.calls.append(('roles',))
        if self.fail_roles:
            raise RuntimeError("roles crashed")

    async def clear_channel_messages(self, channel):
        self.calls.append(('clear', channel.letter))

    def create_group_announcement_message(self, letter, group, info):
        return f"{letter} {info['operate']}"

    async def send_group_announcement_with_image(self, channel, message, group, is_rest_day=False):
        self.calls.append(('announce', channel.letter))
        if channel.letter not in self.fail_letters:
            self.mgr.group_message_ids[channel.letter] = 1000 + ord(channel.letter)

    async def rename_voice_channels(self, guild, groups):
        self.calls.append(('voice',))

    def names(self):
        return [c[0] for c in self.calls]


class AssignmentHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.backup_path = os.path.join(tmp.name, 'teams_backup.json')

        original_channels = settings.GROUP_CHANNEL_IDS
        settings.GROUP_CHANNEL_IDS = dict(GROUP_CHANNELS)
        self.addCleanup(setattr, settings, 'GROUP_CHANNEL_IDS', original_channels)
        original_admins = settings.ADMIN_ROLE_IDS
        settings.ADMIN_ROLE_IDS = {77}
        self.addCleanup(setattr, settings, 'ADMIN_ROLE_IDS', original_admins)

        self.log_channel = MagicMock()
        self.log_channel.send = AsyncMock()
        self.scrim_channel = MagicMock()
        self.scrim_channel.send = AsyncMock()
        group_channels = {
            cid: SimpleNamespace(id=cid, letter=letter) for letter, cid in GROUP_CHANNELS.items()
        }
        self.guild = MagicMock()
        self.guild.get_channel.side_effect = group_channels.get
        self.client = MagicMock()
        self.client.get_guild.side_effect = lambda gid: self.guild if gid == settings.GUILD_ID else None
        self.client.get_channel.side_effect = {
            settings.LOG_CHANNEL_ID: self.log_channel, SCRIM_CHANNEL: self.scrim_channel,
        }.get

        self.processor = MagicMock()
        self.processor.build_groups = AsyncMock()
        self.processor.is_test_account = lambda name: False

        self.now = _kst(17, 0)
        for target in (
            patch.object(scrim_orchestrator, 'get_current_kst_time', side_effect=lambda: self.now),
            patch('models.team_data_manager.get_current_kst_time', side_effect=lambda: self.now),
            patch.object(scrim_orchestrator, 'record_assignment', new=AsyncMock()),
            patch.object(scrim_orchestrator, 'get_rest_day_info', new=AsyncMock(return_value={'is_rest_day': False})),
            patch.object(scrim_orchestrator, 'get_server_info', new=lambda: {'operate': 'Live'}),
        ):
            target.start()
            self.addCleanup(target.stop)
        bm = patch.object(scrim_orchestrator, 'BotManager').start()
        self.addCleanup(patch.stopall)
        bm.get_instance.return_value.get_team_processor.return_value = self.processor
        bm.get_instance.return_value.get_client.return_value = self.client

    def make_manager(self, team_count=16):
        mgr = TeamDataManager(self.client)
        mgr.BACKUP_FILE = self.backup_path
        mgr.scrim_day, mgr.scrim_month = 6, 10
        mgr.scrim_channel_id = SCRIM_CHANNEL
        for i in range(team_count):
            name = f"팀{i:02d}"
            mgr.teams[name] = TeamData(name=name, players=[f"p{i}"], user_id=str(i), mmr=1000 + i)
        mgr.update_all_team_mmr = AsyncMock(return_value=(team_count, 0))
        mgr.resolve_mmr_channel = MagicMock(return_value=None)
        mgr.mmr_update_loop = AsyncMock()
        return mgr

    def use_groups(self, mgr, group_count, unmatched=()):
        names = list(mgr.teams)
        per = settings.TEAMS_PER_GROUP
        groups = [
            [(n, mgr.teams[n], mgr.teams[n].mmr) for n in names[i * per:(i + 1) * per]]
            for i in range(group_count)
        ]
        self.processor.build_groups.return_value = (
            groups, [(n, mgr.teams[n], mgr.teams[n].mmr) for n in unmatched],
        )

    def use_service(self, mgr, **kwargs):
        service = FakeService(mgr, **kwargs)
        self.processor.discord_service = service
        return service

    def restart(self):
        mgr = TeamDataManager(self.client)
        mgr.BACKUP_FILE = self.backup_path
        self.assertTrue(mgr.load_backup())
        mgr.mmr_update_loop = AsyncMock()
        return mgr


class FreshAssignmentTest(AssignmentHarness):
    async def test_runs_all_stages_in_order_and_marks_done(self):
        mgr = self.make_manager(team_count=9)
        self.use_groups(mgr, 1, unmatched=['팀08'])
        service = self.use_service(mgr)

        await mgr.start_team_assignment()

        self.assertEqual(service.calls, [
            ('global', ['팀08']), ('roles',), ('clear', 'A'), ('announce', 'A'), ('clear', 'B'), ('voice',),
        ])
        self.assertEqual(mgr.last_auto_assignment, self.now)
        self.assertEqual(
            sorted(mgr.assignment_progress['done']),
            sorted(['global_notice', 'roles', 'group_notices', 'voice']),
        )
        self.log_channel.send.assert_not_awaited()

    async def test_completed_assignment_does_not_resume_after_restart(self):
        mgr = self.make_manager()
        self.use_groups(mgr, 2)
        self.use_service(mgr)
        await mgr.start_team_assignment()

        mgr2 = self.restart()
        service = self.use_service(mgr2)
        self.now = _kst(18, 0)
        await mgr2.start_team_assignment()
        self.assertEqual(service.calls, [])


class ResumeAssignmentTest(AssignmentHarness):
    async def test_restart_resumes_after_crash_without_resending_global(self):
        mgr = self.make_manager(team_count=17)
        self.use_groups(mgr, 2, unmatched=['팀16'])
        self.use_service(mgr, fail_roles=True)
        await mgr.start_team_assignment()

        self.assertIsNone(mgr.last_auto_assignment)
        self.assertTrue(mgr.is_team_assignment_started)
        self.log_channel.send.assert_awaited_once()
        self.scrim_channel.send.assert_not_awaited()

        mgr2 = self.restart()
        service = self.use_service(mgr2)
        self.now = _kst(17, 5)
        await mgr2.start_team_assignment()

        self.assertNotIn('global', service.names())
        self.assertEqual(service.names(), ['roles', 'clear', 'announce', 'clear', 'announce', 'voice'])
        self.processor.build_groups.assert_awaited_once()
        self.assertEqual(mgr2.last_auto_assignment, self.now)

    async def test_failed_group_notice_is_resent_alone(self):
        mgr = self.make_manager()
        self.use_groups(mgr, 2)
        self.use_service(mgr, fail_letters={'B'})
        await mgr.start_team_assignment()

        self.assertIsNone(mgr.last_auto_assignment)
        alert = self.log_channel.send.await_args.args[0]
        self.assertIn('<@&77>', alert)
        self.assertIn('B조', alert)

        service = self.use_service(mgr)
        ok, _ = mgr._orchestrator.rerun_team_assignment()
        self.assertTrue(ok)
        await asyncio.gather(*mgr._pending_tasks)

        self.assertEqual(service.calls, [('clear', 'B'), ('announce', 'B')])
        self.assertEqual(mgr.last_auto_assignment, self.now)

    async def test_state_without_stage_record_is_treated_as_done(self):
        mgr = self.make_manager()
        self.use_groups(mgr, 2)
        mgr.groups = self.processor.build_groups.return_value[0]
        mgr.is_team_assignment_started = True
        service = self.use_service(mgr)

        await mgr.start_team_assignment()

        self.assertEqual(service.calls, [])
        self.assertEqual(mgr.last_auto_assignment, self.now)


class AssignmentFailureTest(AssignmentHarness):
    async def test_build_failure_keeps_day_open_and_alerts(self):
        mgr = self.make_manager()
        self.processor.build_groups.side_effect = RuntimeError("sheet down")
        service = self.use_service(mgr)

        await mgr.start_team_assignment()
        await asyncio.sleep(0)

        self.assertIsNone(mgr.last_auto_assignment)
        self.assertFalse(mgr.is_team_assignment_started)
        self.assertIsNone(mgr.groups)
        self.assertEqual(service.calls, [])
        alert = self.log_channel.send.await_args
        self.assertIn('<@&77>', alert.args[0])
        self.assertTrue(alert.kwargs['allowed_mentions'].roles)
        self.scrim_channel.send.assert_awaited_once()
        mgr.mmr_update_loop.assert_awaited()

    async def test_alert_without_admin_roles_has_no_mention(self):
        settings.ADMIN_ROLE_IDS = set()
        mgr = self.make_manager()
        self.processor.build_groups.side_effect = RuntimeError("sheet down")
        self.use_service(mgr)

        await mgr.start_team_assignment()

        self.assertNotIn('<@&', self.log_channel.send.await_args.args[0])

    async def test_missing_guild_alerts_and_resumes_later(self):
        mgr = self.make_manager()
        self.use_groups(mgr, 2)
        service = self.use_service(mgr)
        self.client.get_guild.side_effect = lambda gid: None

        await mgr.start_team_assignment()

        self.assertEqual(service.calls, [])
        self.assertIsNone(mgr.last_auto_assignment)
        self.scrim_channel.send.assert_awaited_once()

        self.client.get_guild.side_effect = lambda gid: self.guild
        await mgr.start_team_assignment()
        self.assertEqual(service.names()[0], 'global')
        self.assertEqual(mgr.last_auto_assignment, self.now)


class TeamCountRecheckTest(AssignmentHarness):
    async def test_cancel_during_mmr_refresh_takes_cancel_path(self):
        mgr = self.make_manager(team_count=8)
        service = self.use_service(mgr)

        async def refresh(force=False):
            mgr.teams.pop('팀07')
            return 7, 0

        mgr.update_all_team_mmr = AsyncMock(side_effect=refresh)

        await mgr.start_team_assignment()

        self.processor.build_groups.assert_not_awaited()
        self.assertEqual(service.calls, [])
        self.assertEqual(mgr.last_auto_assignment, self.now)
        self.assertFalse(mgr.is_team_assignment_started)
        self.scrim_channel.send.assert_awaited_once()

    async def test_empty_groups_take_cancel_path(self):
        mgr = self.make_manager(team_count=8)
        self.processor.build_groups.return_value = ([], [])
        service = self.use_service(mgr)

        await mgr.start_team_assignment()

        self.assertEqual(service.calls, [])
        self.assertEqual(mgr.last_auto_assignment, self.now)
        self.scrim_channel.send.assert_awaited_once()


class AssignmentWindowTest(AssignmentHarness):
    async def test_no_assignment_after_next_scrim_opens(self):
        mgr = self.make_manager()
        self.use_groups(mgr, 2)
        self.use_service(mgr)
        self.now = _kst(22, 5)

        await mgr.start_team_assignment()

        self.processor.build_groups.assert_not_awaited()
        self.assertIsNone(mgr.last_auto_assignment)

    async def test_auto_assign_loop_ignores_scrim_day_after_22(self):
        mgr = self.make_manager()
        self.now = _kst(22, 30)
        start = AsyncMock()
        mgr._orchestrator.start_team_assignment = start
        with patch.object(scrim_orchestrator.asyncio, 'sleep', AsyncMock(side_effect=asyncio.CancelledError)):
            await mgr.check_and_auto_assign()
        start.assert_not_awaited()

    async def test_rerun_refusals(self):
        mgr = self.make_manager()

        self.now = _kst(16, 59)
        self.assertFalse(mgr._orchestrator.rerun_team_assignment()[0])

        self.now = _kst(18, 0)
        mgr.last_auto_assignment = _kst(17, 3)
        self.assertFalse(mgr._orchestrator.rerun_team_assignment()[0])

        mgr.last_auto_assignment = None
        async with mgr._orchestrator._assign_lock:
            self.assertFalse(mgr._orchestrator.rerun_team_assignment()[0])
        self.assertEqual(mgr._pending_tasks, set())


class BootstrapResumeTest(unittest.IsolatedAsyncioTestCase):
    def _manager(self, *, started=False, groups=None, last=None):
        mgr = MagicMock()
        mgr.is_team_assignment_started = started
        mgr.groups = groups
        mgr.last_auto_assignment = last
        mgr.is_scrim_date_today.return_value = True
        mgr.restore_group_roster_views = AsyncMock()
        mgr.start_team_assignment = MagicMock(return_value='coro')
        return mgr

    async def _resume(self, mgr, now):
        from bot import events
        with patch.object(events, 'get_current_kst_time', return_value=now):
            await events._resume_after_restore(MagicMock(), mgr)

    async def test_restart_after_22_does_not_run_past_assignment(self):
        mgr = self._manager()
        await self._resume(mgr, _kst(22, 30))
        mgr.spawn_task.assert_not_called()
        mgr.start_background_tasks.assert_called_once()

    async def test_restart_mid_assignment_resumes(self):
        mgr = self._manager(started=True, groups=[['x']])
        await self._resume(mgr, _kst(17, 2))
        mgr.restore_group_roster_views.assert_awaited_once()
        mgr.spawn_task.assert_called_once_with('coro')
        mgr.start_background_tasks.assert_not_called()

    async def test_restart_after_completed_assignment_only_restores_views(self):
        mgr = self._manager(started=True, groups=[['x']], last=_kst(17, 4))
        await self._resume(mgr, _kst(19, 0))
        mgr.restore_group_roster_views.assert_awaited_once()
        mgr.spawn_task.assert_not_called()
        mgr.start_background_tasks.assert_not_called()


if __name__ == '__main__':
    unittest.main()
