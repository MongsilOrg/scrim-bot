import json
import os
import tempfile
import unittest
from datetime import date
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


def response(**extra):
    return {'status': 'ok', 'staff': 2, 'added': 0, 'removed': 0, 'rev': 3, 'changes': [], **extra}


class LabsOpsCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, 'labs_ops.json')
        for name, value in (('LABS_SCRIM_TOKEN', 's' * 64), ('LABS_OPS_PATH', self.path), ('GUILD_ID', 1), ('LOG_CHANNEL_ID', 9)):
            self.addCleanup(setattr, settings, name, getattr(settings, name))
            setattr(settings, name, value)
        for name in ('_copy', '_copy_loaded', '_client', '_failures', '_notified'):
            self.addCleanup(setattr, labs_ops, name, getattr(labs_ops, name))
        labs_ops._copy, labs_ops._copy_loaded, labs_ops._failures, labs_ops._notified = None, False, 0, False

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

        members = {int(STAFF): SimpleNamespace(id=int(STAFF), display_name='스태프'), int(MOD): SimpleNamespace(id=int(MOD), display_name='모드')}
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


class SyncTest(LabsOpsCase):
    async def test_body_lists_staff_ops_names_deploys_and_season_start(self):
        self.assertTrue(await labs_ops.sync(self.client))

        method, path, body = self.request.await_args.args
        self.assertEqual((method, path), ('PUT', '/api/bot/scrim-staff'))
        self.assertEqual(body['ids'], [STAFF, MOD])
        self.assertEqual(body['ops'], [STAFF])
        self.assertEqual(body['names'], {STAFF: '스태프', MOD: '모드'})
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

    async def test_changes_posted_to_log_channel_without_mentions(self):
        self.request.return_value = response(changes=[{'id': 3, 'at': 0, 'by': '운영진A', 'text': '시드A를 추가했습니다.'}])

        await labs_ops.sync(self.client)

        self.assertEqual(len(self.posted()), 1)
        self.assertIn("## 운영툴 변경\n**운영진A**: 시드A를 추가했습니다.", self.posted()[0])
        mentions = self.channel.send.await_args.kwargs['allowed_mentions']
        self.assertFalse(mentions.everyone or mentions.users or mentions.roles)

    async def test_failed_post_keeps_rev_for_next_cycle(self):
        self.request.return_value = response(rev=2, seeds=[], tests=[], sanctions=[])
        await labs_ops.sync(self.client)
        self.channel.send.side_effect = discord.HTTPException(MagicMock(status=500), 'down')
        self.request.return_value = response(rev=5, changes=[{'id': 5, 'at': 0, 'by': '운영진A', 'text': '변경'}], seeds=[{'team_name': '새팀', 'players': []}])

        self.assertTrue(await labs_ops.sync(self.client))

        self.assertEqual(self.saved()['rev'], 2)
        self.assertEqual(labs_ops.seeds(), [{'team_name': '새팀', 'players': []}])

    async def test_skipped_until_members_loaded(self):
        self.guild.chunked = False
        self.assertFalse(await labs_ops.sync(self.client))
        self.request.assert_not_awaited()

    async def test_outage_notified_once_then_recovery(self):
        self.request.return_value = None
        for _ in range(labs_ops.FAILURES_BEFORE_NOTICE + 2):
            self.assertFalse(await labs_ops.sync(self.client))
        self.assertEqual(len(self.posted()), 1)
        self.assertIn("팀 등록의 제재 여부를 직접 확인해주세요.", self.posted()[0])

        self.request.return_value = response()
        await labs_ops.sync(self.client)
        await labs_ops.sync(self.client)

        self.assertEqual(len(self.posted()), 2)
        self.assertIn("Labs 연결이 복구되었습니다.", self.posted()[1])


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

    async def test_extend_masters_returns_count_or_none(self):
        self.request.return_value = {'status': 'ok', 'extended': 2}
        self.assertEqual(await labs_ops.extend_masters('2026-11-24'), 2)
        self.request.assert_awaited_once_with('POST', '/api/bot/scrim-sanctions', {'masters': '2026-11-24'})
        self.request.return_value = None
        self.assertIsNone(await labs_ops.extend_masters('2026-11-24'))


if __name__ == '__main__':
    unittest.main()
