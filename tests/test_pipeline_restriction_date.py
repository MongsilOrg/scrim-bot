import asyncio
import unittest
from datetime import date, datetime
from unittest import mock

from config.settings import settings
from models.team_data import TeamData
from models.team_data_manager import TeamDataManager, format_korean_date


class DateAwareWarningManager:
    worksheet = object()

    def __init__(self, restricted_until: date):
        self.restricted_until = restricted_until
        self.check_dates = []

    async def ensure_connected(self):
        return True

    def is_restricted(self, target_id=None, target_name=None, check_date=None):
        self.check_dates.append(check_date)
        if check_date.date() <= self.restricted_until:
            return True, self.restricted_until.strftime('%Y-%m-%d')
        return False, None


class RestrictionScrimDateTest(unittest.TestCase):
    def tearDown(self):
        mock.patch.stopall()

    def _check(self, now: datetime, until: date, players=('제한됨', 'b', 'c')):
        wm = DateAwareWarningManager(until)
        mgr = TeamDataManager.__new__(TeamDataManager)
        mgr.client = None
        bot = mock.Mock()
        bot.get_warning_manager.return_value = wm
        mock.patch('models.team_data_manager.BotManager').start().get_instance.return_value = bot
        team = TeamData(name='팀', players=list(players))
        allowed, msg = asyncio.run(mgr.check_member_restrictions(now, new_team=team))
        return allowed, msg, wm

    def test_restriction_ending_today_does_not_block_next_day_scrim(self):
        now = datetime(2026, 10, 6, settings.NEXT_SCRIM_OPEN_HOUR, 30)
        allowed, msg, wm = self._check(now, date(2026, 10, 6))
        self.assertTrue(allowed, msg)
        self.assertEqual(wm.check_dates[0].date(), date(2026, 10, 7))

    def test_restriction_ending_today_blocks_today_scrim(self):
        now = datetime(2026, 10, 6, 12, 0)
        allowed, msg, _ = self._check(now, date(2026, 10, 6))
        self.assertFalse(allowed)
        self.assertIn('**제한됨**: 10월 6일 화요일까지', msg)

    def test_all_restricted_members_listed(self):
        now = datetime(2026, 10, 6, 12, 0)
        allowed, msg, _ = self._check(now, date(2026, 10, 8), players=('하나', '둘', '셋'))
        self.assertFalse(allowed)
        for name in ('하나', '둘', '셋'):
            self.assertIn(f'**{name}**', msg)


class KoreanDateFormatTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_korean_date('2026-10-06'), '10월 6일 화요일')
        self.assertEqual(format_korean_date('알 수 없음'), '알 수 없음')
        self.assertEqual(format_korean_date(None), '')


if __name__ == '__main__':
    unittest.main()
