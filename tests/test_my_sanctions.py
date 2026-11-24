import unittest
from datetime import date, datetime
from unittest import mock

from models import warning_manager as wm_module
from models.warning_manager import MASTERS_NOT_DEDUCTED, WarningManager
from tests.test_pipeline_cancel import FakeInteraction, texts
from utils.helpers import KST

ME = '123456789012345678'
OTHER = '999'


def row(target_id, kind, day='2026-10-01', until=None, merged_into=None, released=0, reason='사유', name='나'):
    return {
        'id': 1, 'target_id': target_id, 'target_name': name, 'kind': kind, 'reason': reason,
        'admin': '관리자A', 'day': day, 'until': until, 'merged_into': merged_into, 'released': released,
    }


def make_manager(rows, now=datetime(2026, 10, 6, 12, 0, tzinfo=KST)):
    mock.patch.object(wm_module.labs_ops, 'sanctions', return_value=rows).start()
    mock.patch.object(wm_module, 'get_current_kst_time', return_value=now).start()
    return WarningManager()


class MemberSanctionsTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        mock.patch.stopall()

    async def test_counts_and_restriction_by_id_only(self):
        manager = make_manager([
            row(ME, '주의'),
            row(ME, '주의', merged_into=7),
            row('', '주의'),
            row(OTHER, '주의'),
            row(ME, '경고', until='2026-10-09'),
            row(ME, '경고', until='2026-10-30', released=1),
            row('', '경고', until='2026-10-30'),
        ])

        summary = await manager.get_member_sanctions(ME)

        self.assertEqual(summary['cautions'], 1)
        self.assertEqual(summary['warnings'], 2)
        self.assertEqual(summary['restricted_until'], date(2026, 10, 9))
        self.assertTrue(summary['has_records'])

    async def test_history_latest_first_and_limited(self):
        rows = [row(ME, '주의', f'2026-09-{day:02d}', reason=f'사유{day}') for day in range(1, 8)]
        rows.append(row(ME, '경고', '2026-09-10', reason='주의  누적'))
        manager = make_manager(rows)

        history = (await manager.get_member_sanctions(ME))['history']

        self.assertEqual(len(history), WarningManager.MY_SANCTION_HISTORY_LIMIT)
        self.assertEqual(history[0], {'date': '2026-09-10', 'type': '경고', 'reason': '주의 누적'})
        self.assertEqual(history[1]['reason'], '사유7')
        self.assertNotIn('관리자', repr(history))

    async def test_expired_restriction_not_shown(self):
        manager = make_manager([row(ME, '경고', until='2026-10-05')])
        summary = await manager.get_member_sanctions(ME)
        self.assertIsNone(summary['restricted_until'])

    async def test_last_day_after_open_hour_judged_for_next_scrim(self):
        manager = make_manager(
            [row(ME, '경고', until='2026-10-06')],
            now=datetime(2026, 10, 6, 22, 30, tzinfo=KST),
        )
        summary = await manager.get_member_sanctions(ME)
        self.assertIsNone(summary['restricted_until'])

    async def test_name_only_records_mean_no_sanctions(self):
        manager = make_manager([row('', '주의'), row('', '경고', until='2026-10-30')])
        summary = await manager.get_member_sanctions(ME)
        self.assertFalse(summary['has_records'])

    async def test_missing_copy_returns_none(self):
        manager = make_manager(None)
        self.assertIsNone(await manager.get_member_sanctions(ME))

    async def test_is_restricted_reads_only_warnings(self):
        manager = make_manager([row(ME, '주의', until='2026-10-20'), row(ME, '경고', until='2026-10-08')])
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
        self.assertIn("제재 기록을 불러오지 못했습니다.\n잠시 후 다시 시도해주세요.", texts(sent[0]))

    async def test_cooldown_skips_lookup(self):
        inter, warning_manager, _ = await self._press({'has_records': False}, cooldown=True)
        inter.response.defer.assert_not_awaited()
        warning_manager.get_member_sanctions.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
