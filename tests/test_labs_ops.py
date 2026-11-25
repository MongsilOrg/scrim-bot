import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from config.settings import settings
from services import labs_ops

TODAY = date(2026, 11, 24)
STAFF = '111111111111111111'
MOD = '222222222222222222'


def texts(view):
    return [c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay)]


def member(uid, name, *roles, joined=2024):
    return SimpleNamespace(id=int(uid), display_name=name, roles=[SimpleNamespace(name=r) for r in roles], joined_at=datetime(joined, 1, 1, tzinfo=timezone.utc))


def response(**extra):
    return {'status': 'ok', 'staff': 2, 'added': 0, 'removed': 0, 'rev': 3, **extra}


class LabsOpsCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, 'labs_ops.json')
        for name, value in (('LABS_SCRIM_TOKEN', 's' * 64), ('LABS_OPS_PATH', self.path), ('GUILD_ID', 1), ('ALERT_CHANNEL_ID', 9)):
            self.addCleanup(setattr, settings, name, getattr(settings, name))
            setattr(settings, name, value)
        for name in ('_copy', '_copy_loaded', '_client', '_failures', '_notified', '_members_sent'):
            self.addCleanup(setattr, labs_ops, name, getattr(labs_ops, name))
        labs_ops._copy, labs_ops._copy_loaded, labs_ops._failures, labs_ops._notified = None, False, 0, False
        labs_ops._unanswered.clear()
        self.addCleanup(labs_ops._unanswered.clear)

        self.request = patch.object(labs_ops.labs_api, 'request', new=AsyncMock(return_value=response())).start()
        patch.object(labs_ops.labs_api, 'operator_ids', return_value=[STAFF, MOD]).start()
        patch.object(labs_ops, 'is_admin', side_effect=lambda m: str(m.id) == STAFF).start()
        patch.object(labs_ops.operator_log, 'scrim_day', return_value=TODAY).start()
        patch.object(labs_ops.operator_log, 'deployed', side_effect=lambda day: {'B': [STAFF]} if day == TODAY else {}).start()
        self.schedule = SimpleNamespace(
            deploy_history={'2026-11-24': [STAFF, 'abc'], '2026-11-23': [MOD], '2026-11-01': [STAFF]},
            count_since='2026-11-19',
        )
        patch.object(labs_ops, 'BotManager').start().get_instance.return_value.get_schedule_manager.return_value = self.schedule
        self.addCleanup(patch.stopall)

        members = {int(STAFF): member(STAFF, '스태프', 'Staff'), int(MOD): member(MOD, '모드', 'Mod')}
        self.guild = MagicMock(chunked=True)
        self.guild.get_member.side_effect = members.get
        self.channel = MagicMock()
        self.channel.send = AsyncMock()
        self.client = MagicMock()
        self.client.get_guild.return_value = self.guild
        self.client.get_channel.return_value = self.channel

    def saved(self):
        with open(self.path, encoding='utf-8') as f:
            return json.load(f)

    def posted(self):
        return ['\n'.join(texts(call.kwargs['view'])) for call in self.channel.send.await_args_list]


class RosterTest(unittest.TestCase):
    def test_Lead와_Staff만_서버에_들어온_순서로(self):
        members = [member(3, '다', 'Staff', joined=2025), member(1, '가', 'Head', 'Mod', joined=2020), member(2, '나', 'Lead', 'Mod', joined=2023), member(5, '마', 'Lead', joined=2025), member(4, '라', 'Staff', joined=2025)]
        members[3].joined_at = None
        self.assertEqual(labs_ops._roster(members), ['2', '3', '4', '5'])


class SyncTest(LabsOpsCase):
    async def test_body_lists_staff_ops_names_deploys_and_season_start(self):
        self.assertTrue(await labs_ops.sync(self.client))

        method, path, body = self.request.await_args.args
        self.assertEqual((method, path), ('PUT', '/api/bot/scrim-staff'))
        self.assertEqual(body['ids'], [STAFF, MOD])
        self.assertEqual(body['ops'], [STAFF])
        self.assertEqual(body['names'], {STAFF: '스태프', MOD: '모드'})
        self.assertEqual(body['roster'], [STAFF])
        self.assertEqual(body['since'], '2026-11-19')
        self.assertNotIn('rev', body)
        self.assertEqual(len(body['deploys']), labs_ops.DEPLOY_DAYS)
        self.assertEqual(body['deploys']['2026-11-24'], [{'id': STAFF, 'name': '스태프', 'group': 'B'}])
        self.assertEqual(body['deploys']['2026-11-23'], [{'id': MOD, 'name': '모드', 'group': ''}])
        self.assertEqual(body['deploys']['2026-11-22'], [])
        self.assertNotIn('2026-11-01', body['deploys'])

    async def test_copy_saved_and_served(self):
        self.request.return_value = response(
            seeds=[{'team_name': '시드A', 'players': ['a', 'b', 'c']}],
            tests=[{'nickname': '테스트A', 'mmr': 5800.5}],
            sanctions=[{'target_id': STAFF, 'kind': '경고'}],
        )

        await labs_ops.sync(self.client)

        self.assertEqual(labs_ops.seeds(), [{'team_name': '시드A', 'players': ['a', 'b', 'c']}])
        self.assertEqual(labs_ops.tests(), {'테스트A': 5800.5})
        self.assertEqual(labs_ops.sanctions(), [{'target_id': STAFF, 'kind': '경고'}])
        self.assertEqual(self.saved()['rev'], 3)

        labs_ops._copy, labs_ops._copy_loaded = None, False
        self.assertEqual(labs_ops.tests(), {'테스트A': 5800.5})

    async def test_unchanged_response_keeps_previous_copy_and_sends_rev(self):
        self.request.return_value = response(seeds=[], tests=[], sanctions=[{'kind': '주의'}])
        await labs_ops.sync(self.client)
        self.request.return_value = response()

        self.assertTrue(await labs_ops.sync(self.client))

        self.assertEqual(self.request.await_args.args[2]['rev'], 3)
        self.assertEqual(labs_ops.sanctions(), [{'kind': '주의'}])

    async def test_days_edited_on_labs_replace_history_and_refresh_dashboard(self):
        self.schedule.set_deployment = MagicMock(side_effect=[True, False, False])
        self.request.return_value = response(days=[
            {'day': '2026-11-23', 'ids': [STAFF, 'abc', 7, STAFF], 'rev': 5}, {'day': 'x', 'ids': []}, {'day': '2026-11-24'}, 'x',
            {'day': '2026-11-22', 'ids': [], 'rev': 4}, {'day': '2026-11-21', 'ids': [STAFF], 'rev': '6'},
        ])

        with patch('commands.ui.schedule_views.refresh_dashboard', new=AsyncMock()) as refresh:
            await labs_ops.sync(self.client)

        self.assertEqual(
            [c.args for c in self.schedule.set_deployment.call_args_list],
            [(date(2026, 11, 23), [STAFF], 5), (date(2026, 11, 22), [], 4), (date(2026, 11, 21), [STAFF], None)],
        )
        refresh.assert_awaited_once()

    async def test_free_days_saved_and_served(self):
        self.assertEqual(labs_ops.free_days(), set())
        self.request.return_value = response(free=['2026-11-26', '2026-11-25', 3])
        await labs_ops.sync(self.client)
        self.assertEqual(self.saved()['free'], ['2026-11-25', '2026-11-26'])

        # 같은 번호의 응답이어도 자율 진행일이 바뀌면 저장
        self.request.return_value = response(free=[])
        await labs_ops.sync(self.client)
        labs_ops._copy, labs_ops._copy_loaded = None, False
        self.assertEqual(labs_ops.free_days(), set())

    async def test_rev_follows_labs_without_posting(self):
        # 화면에서 바꾼 내용은 Labs 기록 화면에만 남아 봇은 받은 번호만 따라감
        self.request.return_value = response(rev=5, seeds=[{'team_name': '새팀', 'players': []}])

        await labs_ops.sync(self.client)

        self.assertEqual(self.saved()['rev'], 5)
        self.channel.send.assert_not_awaited()

    async def test_skipped_until_members_loaded(self):
        self.guild.chunked = False
        for _ in range(labs_ops.FAILURES_BEFORE_NOTICE):
            self.assertFalse(await labs_ops.sync(self.client))
        self.request.assert_not_awaited()
        self.assertIn("Labs에 연결하지 못하고 있습니다.", self.posted()[0])

    async def test_outage_notified_once_then_recovery(self):
        self.request.return_value = None
        for _ in range(labs_ops.FAILURES_BEFORE_NOTICE + 2):
            self.assertFalse(await labs_ops.sync(self.client))
        self.assertEqual(len(self.posted()), 1)
        self.assertIn("등록한 팀이 제재를 받았는지 직접 확인해주세요.", self.posted()[0])

        self.request.return_value = response()
        await labs_ops.sync(self.client)
        await labs_ops.sync(self.client)

        self.assertEqual(len(self.posted()), 2)
        self.assertIn("Labs 연결이 복구되었습니다.", self.posted()[1])
        self.client.get_channel.assert_called_with(9)
        mentions = self.channel.send.await_args.kwargs['allowed_mentions']
        self.assertFalse(mentions.everyone or mentions.users or mentions.roles)


class MembersTest(LabsOpsCase):
    def setUp(self):
        super().setUp()
        labs_ops._members_sent = ''
        self.guild.members = [
            SimpleNamespace(id=30, display_name='나', name='b', bot=False, avatar=None),
            SimpleNamespace(id=20, display_name='가', name='a', bot=False, avatar=SimpleNamespace(key='a_' + '0' * 32)),
            SimpleNamespace(id=10, display_name='봇', name='bot', bot=True, avatar=None),
        ]
        self.request.return_value = {'status': 'ok', 'members': 2}

    async def test_sent_once_until_changed(self):
        self.assertTrue(await labs_ops.sync_members(self.client))
        self.assertTrue(await labs_ops.sync_members(self.client))
        self.request.assert_awaited_once_with('PUT', '/api/bot/scrim-members', {'members': [
            {'id': '20', 'name': '가', 'username': 'a', 'avatar': 'a_' + '0' * 32}, {'id': '30', 'name': '나', 'username': 'b', 'avatar': ''}]})
        self.guild.members[0].display_name = '바뀐 이름'
        self.assertTrue(await labs_ops.sync_members(self.client))
        self.assertEqual(self.request.await_count, 2)

    async def test_failed_send_is_retried(self):
        self.request.return_value = None
        self.assertFalse(await labs_ops.sync_members(self.client))
        self.request.return_value = {'status': 'ok', 'members': 2}
        self.assertTrue(await labs_ops.sync_members(self.client))
        self.assertEqual(self.request.await_count, 2)

    async def test_skipped_until_members_loaded(self):
        self.guild.chunked = False
        self.assertFalse(await labs_ops.sync_members(self.client))
        self.guild.chunked = True
        self.guild.members = [m for m in self.guild.members if m.bot]
        self.assertFalse(await labs_ops.sync_members(self.client))
        self.request.assert_not_awaited()


class GrantTest(LabsOpsCase):
    ARGS = dict(target='Alice', target_id=STAFF, kind='경고', reason='대타', admin='운영진A', today='2026-11-24', warning_day='2026-11-23')

    async def test_same_key_resent_once_then_copy_refreshed(self):
        granted = {'status': 'ok', 'kind': '경고', 'warning': {'warning_count': 1}, 'converted': []}
        self.request.side_effect = [None, granted, response(sanctions=[{'kind': '경고'}])]
        labs_ops._client = self.client

        self.assertEqual(await labs_ops.grant(**self.ARGS), granted)

        first, second, synced = self.request.await_args_list
        self.assertEqual(first.args[:2], ('POST', '/api/bot/scrim-sanctions'))
        self.assertEqual(first.args[2], second.args[2])
        self.assertEqual({k: v for k, v in first.args[2].items() if k != 'key'}, {
            'target': 'Alice', 'targetId': STAFF, 'kind': '경고', 'reason': '대타', 'admin': '운영진A',
            'today': '2026-11-24', 'warningDay': '2026-11-23',
        })
        self.assertEqual(synced.args[1], '/api/bot/scrim-staff')
        self.assertEqual(labs_ops.sanctions(), [{'kind': '경고'}])

    async def test_two_failures_return_none(self):
        self.request.return_value = None
        self.assertIsNone(await labs_ops.grant(**self.ARGS))
        self.assertEqual(self.request.await_count, 2)

    async def test_grant_after_failure_sends_the_same_key(self):
        self.request.return_value = None
        await labs_ops.grant(**self.ARGS)
        failed = self.request.await_args.args[2]['key']
        granted = {'status': 'ok', 'kind': '경고', 'warning': {'warning_count': 1}, 'converted': []}
        self.request.side_effect = [granted, granted]

        await labs_ops.grant(**self.ARGS)
        again = self.request.await_args_list[2].args[2]['key']
        await labs_ops.grant(**self.ARGS)
        fresh = self.request.await_args_list[3].args[2]['key']

        self.assertEqual(again, failed)
        self.assertNotEqual(fresh, failed)

    async def test_old_unanswered_key_is_not_reused(self):
        self.request.return_value = None
        await labs_ops.grant(**self.ARGS)
        failed = self.request.await_args.args[2]['key']

        with patch.object(labs_ops.time, 'monotonic', return_value=labs_ops.time.monotonic() + labs_ops.UNANSWERED_SECONDS + 1):
            await labs_ops.grant(**self.ARGS)

        self.assertNotEqual(self.request.await_args.args[2]['key'], failed)

    async def test_extend_masters_returns_count_or_none(self):
        self.request.return_value = {'status': 'ok', 'extended': 2}
        self.assertEqual(await labs_ops.extend_masters('2026-11-24'), 2)
        self.request.assert_awaited_once_with('POST', '/api/bot/scrim-sanctions', {'masters': '2026-11-24'})
        self.request.return_value = None
        self.assertIsNone(await labs_ops.extend_masters('2026-11-24'))


if __name__ == '__main__':
    unittest.main()
