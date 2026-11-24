import json
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

from models import warning_manager as wm_module
from models.warning_manager import WarningManager
from services import notion_api


class MatchesTargetTest(unittest.TestCase):
    def test_both_ids_present_id_decides(self):
        self.assertTrue(
            WarningManager._matches_target('123', 'Alice', '123', 'Bob')
        )
        self.assertFalse(
            WarningManager._matches_target('123', 'Alice', '456', 'Alice')
        )

    def test_record_id_missing_falls_back_to_name(self):
        self.assertTrue(
            WarningManager._matches_target('', 'Alice', '456', 'Alice')
        )
        self.assertTrue(
            WarningManager._matches_target('', '  Ali  ce ', '456', 'ali CE')
        )
        self.assertTrue(
            WarningManager._matches_target('', 'ALICE', '456', 'alice')
        )
        self.assertFalse(
            WarningManager._matches_target('', 'Alice', '456', 'Bob')
        )

    def test_no_target_name_and_no_record_id(self):
        self.assertFalse(
            WarningManager._matches_target('', 'Alice', '456', None)
        )
        self.assertFalse(
            WarningManager._matches_target('', 'Alice', '456', '')
        )


class AddWarningTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.grant = mock.patch.object(wm_module.labs_ops, 'grant', new=mock.AsyncMock()).start()
        mock.patch.object(wm_module, 'get_current_kst_time', return_value=datetime(2026, 11, 24, 16, 0)).start()
        self.addCleanup(mock.patch.stopall)
        self.manager = WarningManager()

    async def _add(self, kind):
        return await self.manager.add_warning(
            target='Alice', target_id='111', warning_type=kind, reason='대타', admin_display_name='운영진A'
        )

    async def test_unknown_type_rejected_without_request(self):
        success, message, warning, converted = await self._add('정지')
        self.assertFalse(success)
        self.assertEqual(message, "유형은 '주의' 또는 '경고'만 가능합니다.")
        self.grant.assert_not_awaited()

    async def test_request_carries_today_and_previous_scrim_day_before_deadline(self):
        self.grant.return_value = {'status': 'ok', 'warning': None, 'converted': []}

        self.assertEqual(await self._add('주의'), (True, "주의가 추가되었습니다.", None, []))

        self.grant.assert_awaited_once_with(
            target='Alice', target_id='111', kind='주의', reason='대타', admin='운영진A',
            today='2026-11-24', warning_day='2026-11-23',
        )

    async def test_failure_reports_retry_message(self):
        self.grant.return_value = None
        with self.assertLogs('scrim-bot.warning_manager', level='ERROR'):
            success, message, warning, converted = await self._add('경고')
        self.assertFalse(success)
        self.assertEqual(message, "제재를 기록하지 못했습니다.\n잠시 후 다시 시도해주세요.")
        self.assertIsNone(warning)
        self.assertEqual(converted, [])

    async def test_warning_message_uses_terms_from_labs(self):
        terms = {'warning_date': '2026-11-23', 'restricted_until': '2026-11-26', 'warning_count': 1, 'duration_days': 3}
        self.grant.return_value = {'status': 'ok', 'warning': terms, 'converted': []}

        success, message, warning, converted = await self._add('경고')

        self.assertTrue(success)
        self.assertEqual(message, "경고가 추가되었습니다. 누적 1회, 제한 3일입니다. 2026-11-26까지 제한됩니다.")
        self.assertEqual(warning, terms)
        self.assertEqual(converted, [])

    async def test_caution_conversion_returns_warning_and_converted(self):
        terms = {'warning_date': '2026-11-23', 'restricted_until': '2026-11-30', 'warning_count': 2, 'duration_days': 7}
        merged = [{'날짜': '2026-11-20', '사유': '지각', '관리자ID': '운영진B'}, {'날짜': '2026-11-24', '사유': '대타', '관리자ID': '운영진A'}]
        self.grant.return_value = {'status': 'ok', 'warning': terms, 'converted': merged}

        success, message, warning, converted = await self._add('주의')

        self.assertTrue(success)
        self.assertEqual(
            message,
            "주의가 추가되었습니다. 주의 2회로 인해 경고 1회가 자동 부여되었습니다. 누적 2회, 제한 7일입니다. 2026-11-30까지 제한됩니다.",
        )
        self.assertEqual(warning, terms)
        self.assertEqual(converted, merged)


def sanction(target_id='111', name='Alice', kind='경고', until='2026-08-15', released=0):
    return {'target_id': target_id, 'target_name': name, 'kind': kind, 'until': until, 'released': released}


class IsRestrictedTest(unittest.TestCase):
    def _check(self, rows, when=datetime(2026, 8, 15, 23, 59), **target):
        with mock.patch.object(wm_module.labs_ops, 'sanctions', return_value=rows):
            return WarningManager().is_restricted(check_date=when, **target)

    def test_restricted_through_release_date(self):
        self.assertEqual(self._check([sanction()], target_id='111'), (True, '2026-08-15'))

    def test_not_restricted_day_after(self):
        self.assertEqual(
            self._check([sanction()], when=datetime(2026, 8, 16, 0, 0), target_id='111'), (False, None)
        )

    def test_latest_release_date_wins(self):
        rows = [sanction(until='2026-08-20'), sanction(until='2026-08-15'), sanction(until='날짜아님')]
        self.assertEqual(self._check(rows, target_id='111'), (True, '2026-08-20'))

    def test_released_and_caution_rows_ignored(self):
        rows = [sanction(released=1), sanction(kind='주의', until='2026-08-30')]
        self.assertEqual(self._check(rows, target_id='111'), (False, None))

    def test_record_without_id_matches_by_name(self):
        rows = [sanction(target_id='', name='  ALI ce')]
        self.assertEqual(self._check(rows, target_id='999', target_name='ali CE'), (True, '2026-08-15'))
        self.assertEqual(self._check(rows, target_id='999', target_name='Bob'), (False, None))

    def test_other_id_not_matched_by_same_name(self):
        self.assertEqual(self._check([sanction()], target_id='999', target_name='Alice'), (False, None))

    def test_missing_copy_restricts_nobody(self):
        self.assertEqual(self._check(None, target_id='111'), (False, None))


class ProcessMastersDaysTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state_path = os.path.join(self.tmpdir.name, 'masters_state.json')

        mock.patch.object(WarningManager, 'MASTERS_STATE_FILE', self.state_path).start()
        mock.patch.object(wm_module, 'get_current_kst_time', return_value=datetime(2026, 8, 11, 12, 0, 0)).start()
        self.extend = mock.patch.object(wm_module.labs_ops, 'extend_masters', new=mock.AsyncMock(return_value=0)).start()
        self.addCleanup(mock.patch.stopall)

        self.manager = WarningManager()

    def _write_state(self, last_processed):
        with open(self.state_path, 'w', encoding='utf-8') as f:
            json.dump({'last_processed': last_processed}, f)

    def _read_state(self):
        with open(self.state_path, 'r', encoding='utf-8') as f:
            return json.load(f)['last_processed']

    async def test_first_run_queries_today_only(self):
        with mock.patch(
            'services.notion_api.get_masters_dates', return_value=set()
        ) as get_dates:
            self.assertTrue(await self.manager.process_masters_days())

        get_dates.assert_called_once_with(date(2026, 8, 11), date(2026, 8, 11))
        self.extend.assert_not_awaited()
        self.assertEqual(self._read_state(), '2026-08-11')

    async def test_fetch_failure_keeps_state(self):
        self._write_state('2026-08-09')

        with mock.patch(
            'services.notion_api.get_masters_dates',
            side_effect=Exception('notion down'),
        ) as get_dates, self.assertLogs('scrim-bot.warning_manager', level='ERROR'):
            self.assertFalse(await self.manager.process_masters_days())

        get_dates.assert_called_once_with(date(2026, 8, 10), date(2026, 8, 11))
        self.extend.assert_not_awaited()
        self.assertEqual(self._read_state(), '2026-08-09')

    async def test_masters_day_extends_and_saves_state(self):
        self._write_state('2026-08-10')
        self.extend.return_value = 2

        with mock.patch(
            'services.notion_api.get_masters_dates',
            return_value={date(2026, 8, 11)},
        ):
            self.assertTrue(await self.manager.process_masters_days())

        self.extend.assert_awaited_once_with('2026-08-11')
        self.assertEqual(self._read_state(), '2026-08-11')

    async def test_mid_range_failure_saves_state_up_to_previous_day(self):
        self._write_state('2026-08-08')
        self.extend.side_effect = [1, None]

        with mock.patch(
            'services.notion_api.get_masters_dates',
            return_value={date(2026, 8, 9), date(2026, 8, 11)},
        ), self.assertLogs('scrim-bot.warning_manager', level='ERROR'):
            self.assertFalse(await self.manager.process_masters_days())

        self.assertEqual([c.args for c in self.extend.await_args_list], [('2026-08-09',), ('2026-08-11',)])
        self.assertEqual(self._read_state(), '2026-08-10')


def notion_row(start, end, tags):
    return {
        'properties': {
            '날짜': {'date': {'start': start, 'end': end}},
            '태그': {'multi_select': [{'name': t} for t in tags]},
        }
    }


class GetMastersDatesTest(unittest.TestCase):
    def _mock_response(self, results, **extra):
        res = mock.Mock()
        res.raise_for_status = mock.Mock()
        res.json.return_value = {'results': results, **extra}
        return res

    def test_only_tournament_rows_counted(self):
        results = [
            notion_row('2026-08-11', '2026-08-11', ['마스터즈']),
            notion_row('2026-08-11', '2026-08-11', ['9.0']),
            notion_row('2026-08-11', '2026-08-11', ['KEL', '결승']),
            notion_row('2026-08-11', '2026-08-11', []),
            notion_row('2026-08-12', '2026-08-12', ['9.0', '결승']),
        ]
        with mock.patch(
            'services.notion_api.requests.post',
            return_value=self._mock_response(results),
        ):
            days = notion_api.get_masters_dates(
                date(2026, 8, 10), date(2026, 8, 12)
            )

        self.assertEqual(days, {date(2026, 8, 11), date(2026, 8, 12)})

    def test_multi_day_rows_clipped_to_range(self):
        results = [
            notion_row('2026-08-08', '2026-08-11', ['마스터즈']),
            notion_row('2026-08-12', '2026-08-20', ['결승']),
            notion_row('2026-08-20', '2026-08-25', ['대회']),
        ]
        with mock.patch(
            'services.notion_api.requests.post',
            return_value=self._mock_response(results),
        ):
            days = notion_api.get_masters_dates(
                date(2026, 8, 10), date(2026, 8, 12)
            )

        self.assertEqual(
            days, {date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)}
        )

    def test_http_failure_propagates(self):
        # 호출부가 예외를 받아야 다음 주기에 재시도
        with mock.patch(
            'services.notion_api.requests.post',
            side_effect=ConnectionError('notion down'),
        ):
            with self.assertRaises(ConnectionError):
                notion_api.get_masters_dates(date(2026, 8, 10), date(2026, 8, 12))

    def test_http_error_status_propagates(self):
        res = mock.Mock()
        res.raise_for_status.side_effect = RuntimeError('HTTP 500')
        with mock.patch(
            'services.notion_api.requests.post', return_value=res
        ):
            with self.assertRaises(RuntimeError):
                notion_api.get_masters_dates(date(2026, 8, 10), date(2026, 8, 12))

    def test_pagination_follows_next_cursor(self):
        page1 = self._mock_response(
            [notion_row('2026-08-10', '2026-08-10', ['마스터즈'])],
            has_more=True, next_cursor='cur123',
        )
        page2 = self._mock_response(
            [notion_row('2026-08-11', '2026-08-11', ['결승'])],
        )
        with mock.patch(
            'services.notion_api.requests.post', side_effect=[page1, page2]
        ) as post:
            days = notion_api.get_masters_dates(
                date(2026, 8, 10), date(2026, 8, 12)
            )

        self.assertEqual(days, {date(2026, 8, 10), date(2026, 8, 11)})
        self.assertEqual(post.call_count, 2)
        first_body = post.call_args_list[0].kwargs['json']
        second_body = post.call_args_list[1].kwargs['json']
        self.assertNotIn('start_cursor', first_body)
        self.assertEqual(second_body['start_cursor'], 'cur123')


if __name__ == '__main__':
    unittest.main()
