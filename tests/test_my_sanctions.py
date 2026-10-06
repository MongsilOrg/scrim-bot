import asyncio
import unittest
from datetime import date, datetime
from unittest import mock

from models import warning_manager as wm_module
from models.warning_manager import MASTERS_NOT_DEDUCTED, WarningManager
from tests.test_pipeline_cancel import FakeInteraction, texts
from utils.helpers import KST

ME = '123456789012345678'
OTHER = '999'


def penalty(target_id, row_type, restricted_until='', target='나', reason='사유', admin='관리자A'):
    return {
        '날짜': '2026-10-01 21:00:00', '대상': target, '대상ID': target_id, '유형': row_type,
        '사유': reason, '경고일': '', '제한해제일': restricted_until, '관리자ID': admin, '비고': '',
    }


def log(target_id, row_type, day, reason='사유', restricted_until='', target='나'):
    return {
        '대상': target, '날짜': day, '제한해제일': restricted_until,
        '사유': reason, '유형': row_type, '대상ID': target_id,
    }


def make_manager(penalty_rows, log_rows, now=datetime(2026, 10, 6, 12, 0, tzinfo=KST)):
    manager = WarningManager.__new__(WarningManager)
    manager.worksheet = mock.Mock()
    manager.worksheet.get_all_records.return_value = penalty_rows
    manager.warning_log_worksheet = mock.Mock()
    manager.warning_log_worksheet.get_all_records.return_value = log_rows
    manager._warnings_cache = None
    manager._cache_timestamp = None
    manager._cache_ttl = 300
    manager.ensure_connected = mock.AsyncMock(return_value=True)
    patcher = mock.patch.object(wm_module, 'get_current_kst_time', return_value=now)
    patcher.start()
    return manager


class MemberSanctionsTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        mock.patch.stopall()

    async def test_counts_and_restriction_by_id_only(self):
        manager = make_manager(
            [
                penalty(int(ME), '주의'),
                penalty('', '주의', target='나'),
                penalty(OTHER, '주의'),
                penalty(ME, '경고', restricted_until='2026-10-09'),
                penalty('', '경고', restricted_until='2026-10-30', target='나'),
            ],
            [
                log(ME, '경고', '2026-10-01', restricted_until='2026-10-08'),
                log(int(ME), '경고', '2026-09-01'),
                log('', '경고', '2026-08-01'),
                log(ME, '주의', '2026-10-05'),
            ],
        )

        summary = await manager.get_member_sanctions(ME)

        self.assertEqual(summary['cautions'], 1)
        self.assertEqual(summary['warnings'], 2)
        # 패널티 시트는 마스터즈 연장이 반영된 날짜
        self.assertEqual(summary['restricted_until'], date(2026, 10, 9))
        self.assertTrue(summary['has_records'])

    async def test_history_latest_first_and_limited(self):
        rows = [log(ME, '주의', f'2026-09-{day:02d}', reason=f'사유{day}') for day in range(1, 8)]
        rows.append(log(ME, '경고', '2026-09-10', reason='[주의 누적]\n1회 2026-09-06: 대타\n2회 2026-09-07: 대타'))
        manager = make_manager([], rows)

        history = (await manager.get_member_sanctions(ME))['history']

        self.assertEqual(len(history), WarningManager.MY_SANCTION_HISTORY_LIMIT)
        self.assertEqual(history[0], {'date': '2026-09-10', 'type': '경고', 'reason': '주의 누적'})
        self.assertEqual(history[1]['reason'], '사유7')
        self.assertNotIn('관리자', repr(history))

    async def test_expired_restriction_not_shown(self):
        manager = make_manager([penalty(ME, '경고', restricted_until='2026-10-05')], [])
        summary = await manager.get_member_sanctions(ME)
        self.assertIsNone(summary['restricted_until'])

    async def test_last_day_after_open_hour_judged_for_next_scrim(self):
        manager = make_manager(
            [penalty(ME, '경고', restricted_until='2026-10-06')], [],
            now=datetime(2026, 10, 6, 22, 30, tzinfo=KST),
        )
        summary = await manager.get_member_sanctions(ME)
        self.assertIsNone(summary['restricted_until'])

    async def test_name_only_records_mean_no_sanctions(self):
        manager = make_manager([penalty('', '주의', target='나')], [log('', '경고', '2026-10-01', target='나')])
        summary = await manager.get_member_sanctions(ME)
        self.assertFalse(summary['has_records'])

    async def test_lookup_failure_returns_none(self):
        manager = make_manager([], [])
        manager.warning_log_worksheet.get_all_records.side_effect = RuntimeError('quota')
        with self.assertLogs('scrim-bot.warning_manager', level='ERROR'):
            self.assertIsNone(await manager.get_member_sanctions(ME))

    async def test_not_connected_returns_none(self):
        manager = make_manager([], [])
        manager.ensure_connected.return_value = False
        self.assertIsNone(await manager.get_member_sanctions(ME))

    async def test_uses_ttl_cache_and_invalidation(self):
        manager = make_manager([penalty(ME, '주의')], [log(ME, '주의', '2026-10-05')])

        await manager.get_member_sanctions(ME)
        await manager.get_member_sanctions(ME)
        await asyncio.to_thread(manager.is_restricted, ME, None)
        self.assertEqual(manager.worksheet.get_all_records.call_count, 1)
        self.assertEqual(manager.warning_log_worksheet.get_all_records.call_count, 1)

        manager._invalidate_cache()
        await manager.get_member_sanctions(ME)
        self.assertEqual(manager.warning_log_worksheet.get_all_records.call_count, 2)

    async def test_is_restricted_still_reads_only_warnings(self):
        manager = make_manager(
            [penalty(ME, '주의', restricted_until='2026-10-20'), penalty(ME, '경고', restricted_until='2026-10-08')],
            [],
        )
        self.assertEqual(manager.is_restricted(ME, None), (True, '2026-10-08'))


class MySanctionsViewTest(unittest.TestCase):
    def test_empty(self):
        from commands.ui.views import my_sanctions_view

        body = texts(my_sanctions_view({'has_records': False}))
        self.assertIn("받은 제재가 없습니다.", body)

    def test_card(self):
        from commands.ui.views import my_sanctions_view

        body = texts(my_sanctions_view({
            'has_records': True, 'cautions': 1, 'warnings': 2,
            'restricted_until': date(2026, 10, 9),
            'history': [{'date': '2026-10-05', 'type': '주의', 'reason': '대타'}],
        }))
        self.assertIn("주의 1회, 누적 경고 2회", body)
        self.assertIn("10월 9일 금요일까지 스크림에 참가할 수 없습니다.", body)
        self.assertIn(MASTERS_NOT_DEDUCTED, body)
        self.assertIn("10월 5일 월요일 주의: 대타", body)

    def test_card_without_restriction(self):
        from commands.ui.views import my_sanctions_view

        body = texts(my_sanctions_view({
            'has_records': True, 'cautions': 0, 'warnings': 1, 'restricted_until': None,
            'history': [{'date': '2026-09-01', 'type': '경고', 'reason': '지각'}],
        }))
        self.assertNotIn("참가 제한", body)
        self.assertIn("9월 1일 화요일 경고: 지각", body)


class MySanctionsButtonTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        mock.patch.stopall()

    async def _press(self, summary, cooldown=False):
        from commands.ui.views import TeamInputView

        warning_manager = mock.Mock()
        warning_manager.get_member_sanctions = mock.AsyncMock(return_value=summary)
        bot = mock.Mock()
        bot.get_warning_manager.return_value = warning_manager
        mock.patch("commands.ui.views.BotManager").start().get_instance.return_value = bot
        mock.patch("commands.ui.views.check_cooldown", mock.AsyncMock(return_value=cooldown)).start()
        sent = []
        send = mock.AsyncMock(side_effect=lambda inter, view, **kw: sent.append(view))
        mock.patch("commands.ui.views.send_response", send).start()
        mock.patch("utils.layout_helpers.send_response", send).start()
        inter = FakeInteraction(user_id=int(ME))
        inter.response.defer = mock.AsyncMock()
        await TeamInputView(scrim_day=6, scrim_month=10, scrim_weekday="화요일").my_sanctions_callback(inter)
        return inter, warning_manager, sent

    async def test_looks_up_by_discord_id_ephemeral(self):
        inter, warning_manager, sent = await self._press({'has_records': False})
        inter.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        warning_manager.get_member_sanctions.assert_awaited_once_with(ME)
        self.assertIn("받은 제재가 없습니다.", texts(sent[0]))

    async def test_failure_message(self):
        _, _, sent = await self._press(None)
        self.assertIn("제재 기록을 불러오지 못했습니다. 잠시 후 다시 시도해주세요.", texts(sent[0]))

    async def test_cooldown_skips_lookup(self):
        inter, warning_manager, _ = await self._press({'has_records': False}, cooldown=True)
        inter.response.defer.assert_not_awaited()
        warning_manager.get_member_sanctions.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
