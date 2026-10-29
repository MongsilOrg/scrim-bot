import os
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot import events
from config.settings import settings
from models import scrim_orchestrator
from services import labs_api
from tests.test_lifecycle_assignment import AssignmentHarness

TODAY = date(2026, 10, 29)


def texts(view):
    return [c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay)]


class LabsStateMixin:
    def use_labs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (('LABS_SCRIM_TOKEN', 's' * 64), ('LABS_STATE_PATH', os.path.join(tmp.name, 'labs.json'))):
            original = getattr(settings, name)
            setattr(settings, name, value)
            self.addCleanup(setattr, settings, name, original)


class LabsStageTest(LabsStateMixin, AssignmentHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.use_labs()
        self.create = patch.object(labs_api, 'create_scrim', new=AsyncMock()).start()
        patch.object(labs_api, 'flush_backups', new=AsyncMock()).start()
        patch.object(labs_api, 'delete_previous', new=AsyncMock()).start()
        patch.object(scrim_orchestrator.asyncio, 'sleep', new=AsyncMock()).start()

    async def test_links_labs_after_group_notices(self):
        mgr = self.make_manager()
        self.use_groups(mgr, 2)
        service = self.use_service(mgr)
        self.create.side_effect = lambda guild, letters: service.calls.append(('labs', letters)) or {'status': 'ok', 'code': 'K3PQ8M', 'missing': []}

        await mgr.start_team_assignment()

        self.assertEqual(service.names()[-2:], ['voice', 'labs'])
        self.assertEqual(service.calls[-1], ('labs', ['A', 'B']))
        self.assertIn('labs', mgr.assignment_progress['done'])
        self.log_channel.send.assert_not_awaited()

    async def test_failure_reports_once_and_still_finishes(self):
        mgr = self.make_manager()
        self.use_groups(mgr, 1)
        self.use_service(mgr)
        self.create.return_value = None

        await mgr.start_team_assignment()

        self.assertEqual(self.create.await_count, 3)
        self.assertNotIn('labs', mgr.assignment_progress['done'])
        self.assertEqual(mgr.last_auto_assignment, self.now)
        self.log_channel.send.assert_awaited_once()

    async def test_state_write_error_still_finishes(self):
        mgr = self.make_manager()
        self.use_groups(mgr, 1)
        self.use_service(mgr)
        self.create.side_effect = OSError("disk full")

        await mgr.start_team_assignment()

        self.assertEqual(mgr.last_auto_assignment, self.now)
        self.log_channel.send.assert_awaited_once()

    async def test_no_labs_stage_when_group_notice_failed(self):
        mgr = self.make_manager()
        self.use_groups(mgr, 2)
        self.use_service(mgr, fail_letters=['B'])

        await mgr.start_team_assignment()

        self.create.assert_not_awaited()


class LabsApiTest(LabsStateMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.use_labs()
        channels = settings.GROUP_CHANNEL_IDS
        settings.GROUP_CHANNEL_IDS = {'A': 111, 'B': 222}
        self.addCleanup(setattr, settings, 'GROUP_CHANNEL_IDS', channels)
        self.request = patch.object(labs_api, '_request', new=AsyncMock()).start()
        self.addCleanup(patch.stopall)
        patch.object(labs_api, 'get_current_kst_time', return_value=SimpleNamespace(date=lambda: TODAY)).start()

    def make_guild(self):
        member = lambda i, bot=False: SimpleNamespace(id=i, bot=bot)
        guild = MagicMock()
        guild.owner_id = 1
        guild.name = '스크림'
        guild.roles = [
            SimpleNamespace(name='Mod', members=[member(1), member(2)]),
            SimpleNamespace(name='Staff', members=[member(3), member(99, bot=True)]),
            SimpleNamespace(name='Former', members=[member(4)]),
        ]
        guild.get_channel.side_effect = {111: SimpleNamespace(name='a조'), 222: SimpleNamespace(name='b조')}.get
        return guild

    async def test_create_sends_operators_and_links(self):
        self.request.return_value = {'status': 'ok', 'code': 'K3PQ8M', 'missing': ['3']}

        result = await labs_api.create_scrim(self.make_guild(), ['A', 'B'])

        self.assertEqual(result['code'], 'K3PQ8M')
        method, path, body = self.request.await_args.args
        self.assertEqual((method, path), ('POST', '/api/bot/t'))
        self.assertEqual(body['owner'], '1')
        self.assertEqual(body['operators'], ['1', '2', '3'])
        self.assertEqual(body['name'], '10월 29일 목요일 스크림')
        self.assertEqual(body['rules']['bans'], 'basic')
        self.assertEqual([(l['group'], l['channelId'], l['channel']) for l in body['links']], [('A', '111', 'a조'), ('B', '222', 'b조')])
        self.assertTrue(labs_api.linked_today('a', TODAY))
        self.assertFalse(labs_api.linked_today('C', TODAY))
        self.assertFalse(labs_api.linked_today('A', date(2026, 10, 30)))

    async def test_failed_create_keeps_scrimbot_scoring(self):
        self.request.return_value = None
        self.assertIsNone(await labs_api.create_scrim(self.make_guild(), ['A']))
        self.assertFalse(labs_api.linked_today('A', TODAY))

    async def test_linked_off_without_token(self):
        labs_api.save_state({'date': TODAY.isoformat(), 'code': 'K3PQ8M', 'groups': ['A'], 'posted': []})
        settings.LABS_SCRIM_TOKEN = ''
        self.assertFalse(labs_api.linked_today('A', TODAY))

    async def test_backup_waits_for_last_round_then_posts_once(self):
        labs_api.save_state({'date': TODAY.isoformat(), 'code': 'K3PQ8M', 'groups': ['A'], 'posted': []})
        channel = MagicMock()
        channel.send = AsyncMock()
        client = MagicMock()
        client.get_channel.return_value = channel
        replays = [{'roundNo': 1, 'gameId': 111}, {'roundNo': 2, 'gameId': 222}]

        self.request.return_value = {'played': 2, 'plannedRounds': 4, 'ended': False, 'replays': replays}
        self.assertFalse(await labs_api.post_backup(client, 'A'))
        channel.send.assert_not_awaited()

        self.request.return_value = {'played': 4, 'plannedRounds': 4, 'ended': True, 'replays': replays}
        self.assertTrue(await labs_api.post_backup(client, 'A'))
        self.assertFalse(await labs_api.post_backup(client, 'A'))
        channel.send.assert_awaited_once()
        self.assertIn("**1R**: `111`\n**2R**: `222`", "\n".join(texts(channel.send.await_args.kwargs['view'])))
        self.assertEqual(self.request.await_args.args[:2], ('GET', '/api/bot/t/K3PQ8M?group=A'))

    async def test_last_check_posts_played_rounds(self):
        labs_api.save_state({'date': TODAY.isoformat(), 'code': 'K3PQ8M', 'groups': ['A'], 'posted': []})
        channel = MagicMock()
        channel.send = AsyncMock()
        client = MagicMock()
        client.get_channel.return_value = channel
        self.request.return_value = {'played': 3, 'plannedRounds': 4, 'ended': False, 'replays': [{'roundNo': 1, 'gameId': 5}]}
        with patch.object(labs_api.asyncio, 'sleep', new=AsyncMock()) as sleep:
            labs_api.schedule_backup(client, 'A')
            await labs_api._backup_tasks['A']
        self.assertEqual([c.args[0] for c in sleep.await_args_list], list(labs_api.BACKUP_CHECK_DELAYS))
        channel.send.assert_awaited_once()

    async def test_failed_send_can_retry(self):
        labs_api.save_state({'date': TODAY.isoformat(), 'code': 'K3PQ8M', 'groups': ['A'], 'posted': []})
        channel = MagicMock()
        channel.send = AsyncMock(side_effect=ConnectionResetError())
        client = MagicMock()
        client.get_channel.return_value = channel
        self.request.return_value = {'played': 4, 'plannedRounds': 4, 'ended': True, 'replays': [{'roundNo': 1, 'gameId': 5}]}

        with self.assertRaises(ConnectionResetError):
            await labs_api.post_backup(client, 'A')
        self.assertEqual(labs_api.load_state()['posted'], [])

    async def test_restart_resumes_unposted_groups(self):
        labs_api.save_state({'date': TODAY.isoformat(), 'code': 'K3PQ8M', 'groups': ['A', 'B'], 'posted': ['A']})
        with patch.object(labs_api, 'schedule_backup') as schedule:
            labs_api.resume_backups('client')
        schedule.assert_called_once_with('client', 'B')

    async def test_flush_posts_unfinished_groups(self):
        labs_api.save_state({'date': '2026-10-28', 'code': 'K3PQ8M', 'groups': ['A', 'B'], 'posted': ['A']})
        channel = MagicMock()
        channel.send = AsyncMock()
        client = MagicMock()
        client.get_channel.return_value = channel
        self.request.return_value = {'played': 3, 'plannedRounds': 4, 'ended': False, 'replays': [{'roundNo': 1, 'gameId': 5}]}

        await labs_api.flush_backups(client)

        channel.send.assert_awaited_once()
        self.assertEqual(labs_api.load_state()['posted'], ['A', 'B'])

    async def test_deletes_previous_day_after_backups(self):
        labs_api.save_state({'date': '2026-10-28', 'code': 'OLD000', 'groups': ['A', 'B'], 'posted': ['A', 'B']})
        self.request.return_value = {'status': 'ok'}

        await labs_api.delete_previous(self.make_guild(), TODAY)
        await labs_api.delete_previous(self.make_guild(), TODAY)

        self.request.assert_awaited_once_with('DELETE', '/api/bot/t/OLD000', {'owner': '1'})
        self.assertTrue(labs_api.load_state()['deleted'])

    async def test_keeps_today_and_unsent_tournaments(self):
        labs_api.save_state({'date': TODAY.isoformat(), 'code': 'K3PQ8M', 'groups': ['A'], 'posted': ['A']})
        await labs_api.delete_previous(self.make_guild(), TODAY)
        labs_api.save_state({'date': '2026-10-28', 'code': 'OLD000', 'groups': ['A', 'B'], 'posted': ['A']})
        await labs_api.delete_previous(self.make_guild(), TODAY)
        self.request.assert_not_awaited()

    async def test_failed_delete_is_left_to_retention(self):
        labs_api.save_state({'date': '2026-10-28', 'code': 'OLD000', 'groups': ['A'], 'posted': ['A']})
        self.request.return_value = None
        await labs_api.delete_previous(self.make_guild(), TODAY)
        self.assertNotIn('deleted', labs_api.load_state())

    async def test_flush_marks_group_without_games(self):
        labs_api.save_state({'date': '2026-10-28', 'code': 'OLD000', 'groups': ['A'], 'posted': []})
        client = MagicMock()
        self.request.return_value = {'played': 0, 'plannedRounds': 4, 'ended': False, 'replays': []}
        await labs_api.flush_backups(client)
        client.get_channel.return_value.send.assert_not_called()
        self.assertEqual(labs_api.load_state()['posted'], ['A'])

    async def test_flush_skips_today(self):
        labs_api.save_state({'date': TODAY.isoformat(), 'code': 'K3PQ8M', 'groups': ['A'], 'posted': []})
        await labs_api.flush_backups(MagicMock())
        self.request.assert_not_awaited()
        self.assertEqual(labs_api.load_state()['posted'], [])

    async def test_new_day_keeps_no_posted_groups(self):
        labs_api.save_state({'date': '2026-10-28', 'code': 'OLD000', 'groups': ['A'], 'posted': ['A']})
        self.request.return_value = {'status': 'ok', 'code': 'K3PQ8M', 'missing': []}
        await labs_api.create_scrim(self.make_guild(), ['A'])
        self.assertEqual(labs_api.load_state()['posted'], [])


class LinkedCsvTest(LabsStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_linked_group_skips_scoring_and_checks_backup(self):
        self.use_labs()
        message = MagicMock()
        message.author.bot = False
        message.attachments = [SimpleNamespace(filename='GameResult.csv')]
        with patch.object(events, 'get_group_letter', return_value='A'), \
                patch.object(events.labs_api, 'linked_today', return_value=True), \
                patch.object(events.labs_api, 'schedule_backup') as schedule, \
                patch.object(events, '_process_csv_attachments', new=AsyncMock()) as process, \
                patch.object(events, 'BotManager') as bm:
            await events.on_message(message)
        process.assert_not_awaited()
        schedule.assert_called_once_with(bm.get_instance.return_value.get_client.return_value, 'A')


if __name__ == '__main__':
    unittest.main()
