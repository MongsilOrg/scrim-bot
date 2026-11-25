import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config.settings import settings
from services import labs_logs

USER = SimpleNamespace(id=111111111111111111, display_name='신청자')


class LabsLogsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, 'labs_logs.json')
        for name, value in (('LABS_SCRIM_TOKEN', 's' * 64), ('LABS_LOGS_PATH', self.path)):
            self.addCleanup(setattr, settings, name, getattr(settings, name))
            setattr(settings, name, value)
        self.addCleanup(setattr, labs_logs, '_queue', labs_logs._queue)
        labs_logs._queue = None
        self.request = patch.object(labs_logs.labs_api, 'request', new=AsyncMock(return_value={'status': 'ok', 'added': 1, 'total': 1})).start()
        self.addCleanup(patch.stopall)

    def saved(self):
        with open(self.path, encoding='utf-8') as f:
            return json.load(f)

    async def test_record_then_flush_empties_queue(self):
        labs_logs.record('강제취소', USER, '팀A', players=['가', ' ', '나'], staff=['다'], applicant_id='222222222222222222')

        self.assertTrue(await labs_logs.flush())

        body = self.request.await_args.args[2]['logs'][0]
        self.assertEqual(self.request.await_args.args[:2], ('POST', '/api/bot/scrim-logs'))
        self.assertEqual(
            {k: body[k] for k in ('kind', 'team', 'userId', 'userName', 'applicantId', 'players', 'staff', 'detail')},
            {'kind': '강제취소', 'team': '팀A', 'userId': str(USER.id), 'userName': '신청자', 'applicantId': '222222222222222222', 'players': ['가', '나'], 'staff': ['다'], 'detail': ''},
        )
        self.assertEqual(self.saved(), [])

    async def test_failed_send_keeps_queue_across_restart(self):
        self.request.return_value = None
        labs_logs.record('신청', USER, '팀A', players=['가'])
        self.assertFalse(await labs_logs.flush())
        key = self.saved()[0]['key']

        # 봇을 다시 켜도 파일에서 읽어 같은 key로 다시 보냄
        labs_logs._queue = None
        self.request.return_value = {'status': 'ok', 'added': 1, 'total': 1}
        self.assertTrue(await labs_logs.flush())
        self.assertEqual(self.request.await_args.args[2]['logs'][0]['key'], key)
        self.assertEqual(self.saved(), [])

    async def test_odd_applicant_id_is_dropped(self):
        labs_logs.record('강제취소', USER, '팀A', applicant_id='알 수 없음')
        self.assertEqual(self.saved()[0]['applicantId'], '')

    async def test_skipped_without_labs(self):
        settings.LABS_SCRIM_TOKEN = ''
        labs_logs.record('신청', USER, '팀A')
        self.assertFalse(await labs_logs.flush())
        self.assertFalse(os.path.exists(self.path))
        self.request.assert_not_awaited()

    def test_record_finds_ids_by_unique_server_name(self):
        def member(id, nick, name):
            return SimpleNamespace(id=id, display_name=nick, global_name=None, name=name)
        guild = SimpleNamespace(members=[member(1, '가나', 'gana'), member(2, '다라', 'dara'), member(3, '다라', 'dara2'), member(4, '별명', 'maba')])
        user = SimpleNamespace(id=111111111111111111, display_name='신청자', guild=guild)
        labs_logs.record('신청', user, '팀A', players=['가나', '다라', 'maba'], staff=['MABA', '없음'])
        # 같은 별명이 둘이면 누구인지 알 수 없어 빼고, 사용자명으로도 찾음
        self.assertEqual(self.saved()[0]['ids'], {'가나': '1', 'maba': '4', 'MABA': '4'})
        labs_logs.record('신청', USER, '팀A', players=['가나'])
        self.assertEqual(self.saved()[1]['ids'], {})
