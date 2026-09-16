import json
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

from models.warning_manager import WarningManager
from services import notion_api


def make_manager(worksheet=None, log_worksheet=None):
    manager = WarningManager.__new__(WarningManager)
    manager.worksheet = worksheet if worksheet is not None else mock.Mock()
    manager.warning_log_worksheet = (
        log_worksheet if log_worksheet is not None else mock.Mock()
    )
    manager._warnings_cache = None
    manager._cache_timestamp = None
    manager._cache_ttl = 300
    return manager


def penalty_row(target='', target_id='', row_type='', restricted_until='',
                warning_date='2026-08-01'):
    """패널티 시트 9열 raw 행."""
    return [
        '2026-08-01 12:00:00', target, target_id, row_type,
        '사유', warning_date, restricted_until, '관리자', '',
    ]


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


class RestrictionDaysTest(unittest.TestCase):
    def test_restriction_days_table(self):
        self.assertEqual(WarningManager.restriction_days_for(1), 3)
        self.assertEqual(WarningManager.restriction_days_for(2), 7)
        self.assertEqual(WarningManager.restriction_days_for(3), 14)
        self.assertEqual(WarningManager.restriction_days_for(10), 14)


class CautionConversionTest(unittest.IsolatedAsyncioTestCase):
    def _make_conversion_manager(self, caution_rows):
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            WarningManager.PENALTY_HEADERS,
            *caution_rows,
        ]
        log_worksheet = mock.Mock()
        log_worksheet.get_all_records.return_value = []
        return make_manager(worksheet=worksheet, log_worksheet=log_worksheet)

    async def test_threshold_cautions_convert_to_warning(self):
        # 방금 추가할 주의까지 포함한 시트 상태
        manager = self._make_conversion_manager([
            penalty_row('Alice', '111', '주의')
            for _ in range(WarningManager.CAUTION_TO_WARNING_COUNT)
        ])

        with mock.patch(
            'models.warning_manager.get_current_kst_time',
            return_value=datetime(2026, 8, 11, 18, 0, 0),
        ):
            ok, msg, auto_warning, converted = await manager.add_warning(
                'Alice', '111', '주의', '규칙 위반', '관리자'
            )

        self.assertTrue(ok)
        self.assertIsNotNone(auto_warning)
        self.assertIn('경고 1회가 자동 부여', msg)
        self.assertEqual(auto_warning['warning_count'], 1)
        self.assertEqual(auto_warning['warning_date'], '2026-08-11')
        self.assertEqual(auto_warning['restricted_until'], '2026-08-14')
        self.assertEqual(
            len(converted), WarningManager.CAUTION_TO_WARNING_COUNT
        )

        # 위에서부터 지우면 행 번호가 밀림
        manager.worksheet.delete_rows.assert_has_calls(
            [mock.call(3), mock.call(2)]
        )
        self.assertEqual(manager.worksheet.append_row.call_count, 2)
        auto_row = manager.worksheet.append_row.call_args_list[1].args[0]
        self.assertEqual(auto_row[3], '경고')        # 유형
        self.assertEqual(auto_row[6], '2026-08-14')  # 제한해제일
        self.assertEqual(auto_row[8], '주의 누적')   # 비고
        log_calls = manager.warning_log_worksheet.append_row.call_args_list
        self.assertEqual([call.args[0][4] for call in log_calls], ['주의', '경고'])

    async def test_below_threshold_no_conversion(self):
        manager = self._make_conversion_manager([
            penalty_row('Alice', '111', '주의')
            for _ in range(WarningManager.CAUTION_TO_WARNING_COUNT - 1)
        ])

        with mock.patch(
            'models.warning_manager.get_current_kst_time',
            return_value=datetime(2026, 8, 11, 18, 0, 0),
        ):
            ok, msg, auto_warning, converted = await manager.add_warning(
                'Alice', '111', '주의', '규칙 위반', '관리자'
            )

        self.assertTrue(ok)
        self.assertIsNone(auto_warning)
        self.assertEqual(converted, [])
        manager.worksheet.delete_rows.assert_not_called()
        self.assertEqual(manager.worksheet.append_row.call_count, 1)


class CountPreviousWarningsTest(unittest.TestCase):
    def test_counts_only_matching_warning_rows(self):
        log_worksheet = mock.Mock()
        log_worksheet.get_all_records.return_value = [
            {'유형': '주의', '대상ID': '111', '대상': 'Alice'},
            {'유형': '경고', '대상ID': '111', '대상': 'Alice'},
            {'유형': '경고', '대상ID': '222', '대상': 'Alice'},
            {'유형': '경고', '대상ID': '', '대상': 'alice'},
            {'유형': '경고', '대상ID': '', '대상': 'Bob'},
        ]
        manager = make_manager(log_worksheet=log_worksheet)

        count = manager._count_previous_warnings(
            target_id='111', target_name='Alice'
        )

        self.assertEqual(count, 2)
        log_worksheet.get_all_records.assert_called_once_with(
            expected_headers=WarningManager.LOG_HEADERS
        )

    def test_no_log_worksheet_returns_none(self):
        manager = make_manager()
        manager.warning_log_worksheet = None

        self.assertIsNone(
            manager._count_previous_warnings(target_id='111', target_name='Alice')
        )

    def test_fetch_failure_returns_none(self):
        log_worksheet = mock.Mock()
        log_worksheet.get_all_records.side_effect = Exception('api down')
        manager = make_manager(log_worksheet=log_worksheet)

        self.assertIsNone(
            manager._count_previous_warnings(target_id='111', target_name='Alice')
        )


class FindMaxRestrictionTest(unittest.TestCase):
    def test_returns_max_restricted_until_not_last_row(self):
        manager = make_manager()
        warnings = [
            {'대상ID': '111', '대상': 'Alice', '제한해제일': '2026-08-10'},
            {'대상ID': '111', '대상': 'Alice', '제한해제일': '2026-08-20'},
            {'대상ID': '111', '대상': 'Alice', '제한해제일': '2026-08-15'},
            {'대상ID': '999', '대상': 'Bob', '제한해제일': '2026-08-30'},
        ]

        latest = manager._find_max_restriction(warnings, target_id='111')

        self.assertIsNotNone(latest)
        self.assertEqual(latest['restricted_until'], date(2026, 8, 20))
        self.assertEqual(latest['target'], 'Alice')

    def test_ignores_unparseable_dates(self):
        manager = make_manager()
        warnings = [
            {'대상ID': '111', '대상': 'Alice', '제한해제일': '날짜아님'},
            {'대상ID': '111', '대상': 'Alice', '제한해제일': ''},
            {'대상ID': '111', '대상': 'Alice', '제한해제일': '2026-08-12'},
        ]

        latest = manager._find_max_restriction(warnings, target_id='111')

        self.assertEqual(latest['restricted_until'], date(2026, 8, 12))

    def test_no_match_returns_none(self):
        manager = make_manager()
        warnings = [
            {'대상ID': '111', '대상': 'Alice', '제한해제일': '2026-08-12'},
        ]

        self.assertIsNone(
            manager._find_max_restriction(warnings, target_id='777')
        )


class IsRestrictedTest(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()
        self.manager._get_warnings_cache = mock.Mock(return_value=[
            {'대상ID': '111', '대상': 'Alice', '제한해제일': '2026-08-15'},
        ])

    def test_restricted_through_release_date(self):
        restricted, until = self.manager.is_restricted(
            target_id='111', check_date=datetime(2026, 8, 15, 23, 59)
        )
        self.assertTrue(restricted)
        self.assertEqual(until, '2026-08-15')

    def test_not_restricted_day_after(self):
        restricted, until = self.manager.is_restricted(
            target_id='111', check_date=datetime(2026, 8, 16, 0, 0)
        )
        self.assertFalse(restricted)
        self.assertIsNone(until)


class ExtendActiveRestrictionsTest(unittest.TestCase):
    def test_extends_only_active_rows(self):
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            WarningManager.PENALTY_HEADERS,
            penalty_row('Alice', '111', '경고', '2026-08-05'),
            penalty_row('Bob', '222', '경고', '2026-08-10'),
            penalty_row('Carol', '333', '경고', '2026-08-12'),
            penalty_row('Dave', '444', '주의', ''),
            penalty_row('Eve', '555', '경고', '깨진값'),
            penalty_row('Frank', '666', '경고', '2026-08-15',
                        warning_date='2026-08-10'),
            penalty_row('Grace', '777', '경고', '2026-08-15',
                        warning_date='2026-08-11'),
            penalty_row('Heidi', '888', '경고', '2026-08-13',
                        warning_date=''),
            penalty_row('Ivan', '999', '경고', '2026-08-14',
                        warning_date='못읽는값'),
        ]
        manager = make_manager(worksheet=worksheet)

        extended = manager._extend_active_restrictions(date(2026, 8, 10))

        self.assertEqual(extended, 4)
        # 제한해제일은 G열, 1행은 헤더
        worksheet.batch_update.assert_called_once_with([
            {'range': 'G3', 'values': [['2026-08-11']]},
            {'range': 'G4', 'values': [['2026-08-13']]},
            {'range': 'G9', 'values': [['2026-08-14']]},
            {'range': 'G10', 'values': [['2026-08-15']]},
        ])
        worksheet.update_cell.assert_not_called()

    def test_no_eligible_rows_skips_batch_update(self):
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            WarningManager.PENALTY_HEADERS,
            penalty_row('Alice', '111', '경고', '2026-08-05'),
            penalty_row('Dave', '444', '주의', ''),
        ]
        manager = make_manager(worksheet=worksheet)

        extended = manager._extend_active_restrictions(date(2026, 8, 10))

        self.assertEqual(extended, 0)
        worksheet.batch_update.assert_not_called()


class ProcessMastersDaysTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state_path = os.path.join(self.tmpdir.name, 'masters_state.json')

        patcher = mock.patch.object(
            WarningManager, 'MASTERS_STATE_FILE', self.state_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        time_patcher = mock.patch(
            'models.warning_manager.get_current_kst_time',
            return_value=datetime(2026, 8, 11, 12, 0, 0),
        )
        time_patcher.start()
        self.addCleanup(time_patcher.stop)

        self.manager = make_manager()

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
            await self.manager.process_masters_days()

        get_dates.assert_called_once_with(date(2026, 8, 11), date(2026, 8, 11))
        self.assertEqual(self._read_state(), '2026-08-11')

    async def test_fetch_failure_keeps_state(self):
        self._write_state('2026-08-09')
        self.manager._extend_active_restrictions = mock.Mock()

        with mock.patch(
            'services.notion_api.get_masters_dates',
            side_effect=Exception('notion down'),
        ) as get_dates:
            await self.manager.process_masters_days()

        get_dates.assert_called_once_with(date(2026, 8, 10), date(2026, 8, 11))
        self.manager._extend_active_restrictions.assert_not_called()
        self.assertEqual(self._read_state(), '2026-08-09')

    async def test_masters_day_extends_and_saves_state(self):
        self._write_state('2026-08-10')
        self.manager._extend_active_restrictions = mock.Mock(return_value=2)

        with mock.patch(
            'services.notion_api.get_masters_dates',
            return_value={date(2026, 8, 11)},
        ):
            await self.manager.process_masters_days()

        self.manager._extend_active_restrictions.assert_called_once_with(
            date(2026, 8, 11)
        )
        self.assertEqual(self._read_state(), '2026-08-11')

    async def test_mid_range_failure_saves_state_up_to_previous_day(self):
        self._write_state('2026-08-08')
        self.manager._extend_active_restrictions = mock.Mock(
            side_effect=[1, Exception('sheet down')]
        )

        with mock.patch(
            'services.notion_api.get_masters_dates',
            return_value={date(2026, 8, 9), date(2026, 8, 11)},
        ):
            await self.manager.process_masters_days()

        self.manager._extend_active_restrictions.assert_has_calls([
            mock.call(date(2026, 8, 9)),
            mock.call(date(2026, 8, 11)),
        ])
        self.assertEqual(
            self.manager._extend_active_restrictions.call_count, 2
        )
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
