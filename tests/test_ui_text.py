import re
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from commands import room_code
from commands.ui import warning_modals
from models.schedule_manager import ScheduleManager
from utils.helpers import KST
from utils.layout_helpers import format_kr_date

FORBIDDEN = re.compile(r"[—·•→…“”‘’]|\(|N/A")


class FormatDateTest(unittest.TestCase):
    def test_date_and_strings(self):
        self.assertEqual(format_kr_date(date(2026, 10, 6)), "10월 6일 화요일")
        self.assertEqual(format_kr_date("2026-10-09"), "10월 9일 금요일")
        self.assertEqual(format_kr_date("2026-10-06 21:30:00"), "10월 6일 화요일")
        self.assertEqual(format_kr_date(datetime(2026, 10, 12, tzinfo=KST)), "10월 12일 월요일")

    def test_unparsed_string_kept(self):
        self.assertEqual(format_kr_date("기록 없음"), "기록 없음")


class BanLineTest(unittest.TestCase):
    def _message(self, line):
        child = SimpleNamespace(content=line)
        return SimpleNamespace(components=[SimpleNamespace(children=[child])])

    def test_new_and_legacy_prefix(self):
        self.assertEqual(room_code._find_ban_display(self._message("밴: `A` `B`")), "`A` `B`")
        self.assertEqual(room_code._find_ban_display(self._message("🚫 밴: `A`")), "`A`")


class ScheduleStatusTextTest(unittest.TestCase):
    @patch.object(ScheduleManager, "save_backup")
    def test_status_text_has_no_brackets(self, _save):
        mgr = ScheduleManager()
        with patch("models.schedule_manager.get_current_kst_time", return_value=datetime(2026, 10, 3, 22, tzinfo=KST)):
            mgr.initialize_week()
        self.assertEqual(mgr.week_label, "10월 5일 월요일부터 10월 9일 금요일까지")
        mgr.register_schedule("a", "Alice", {0, 1})
        mgr.register_schedule("b", "Bob", set(), "출장")
        mgr.generate_assignments()
        mgr.actual_deployments = {0: ["a"]}

        text = mgr.get_status_text([("a", "Alice"), ("b", "Bob"), ("c", "Carol")])

        self.assertIsNone(FORBIDDEN.search(text), text)
        self.assertIn("3명 중 2명 응답", text)
        self.assertIn("Bob: 불참, 사유 출장", text)
        self.assertIn("**월** 1명: **Alice** 투입", text)
        self.assertIn("**수** 배정 없음", text)


class SanctionTextTest(unittest.TestCase):
    def test_restriction_summary(self):
        text = warning_modals._restriction_summary(
            {"restricted_until": "2026-10-09", "warning_count": 2, "duration_days": 3}
        )
        self.assertEqual(text, "**10월 9일 금요일**까지 스크림에 참여할 수 없습니다.\n누적 경고 2회, 제한 3일")

    def test_missing_values(self):
        self.assertIsNone(FORBIDDEN.search(warning_modals._restriction_summary({})))
        history = warning_modals._caution_history([{}], detailed=False)
        self.assertEqual(history, "`1회` 기록 없음: 기록 없음")


if __name__ == "__main__":
    unittest.main()
