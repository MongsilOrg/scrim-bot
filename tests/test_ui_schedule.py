import unittest
from datetime import datetime
from unittest.mock import patch

from commands.ui import schedule_views
from models import schedule_manager
from models.schedule_manager import ScheduleManager
from utils.helpers import KST

MONDAY = datetime(2026, 10, 5, tzinfo=KST)


def _manager():
    mgr = ScheduleManager()
    mgr.week_label = "test"
    mgr.week_start = MONDAY
    mgr.admin_names = {"a": "Alice", "b": "Bob"}
    mgr.availability = {"a": {0, 1, 2, 3}, "b": {0, 1, 2, 3}}
    return mgr


class LockedDaysTest(unittest.TestCase):
    def _locked(self, now):
        mgr = _manager()
        with patch.object(schedule_manager, "get_current_kst_time", return_value=now):
            return mgr._locked_days()

    def test_before_start_hour_today_is_open(self):
        self.assertEqual(self._locked(datetime(2026, 10, 7, 15, tzinfo=KST)), {0, 1})

    def test_after_start_hour_today_is_locked(self):
        self.assertEqual(self._locked(datetime(2026, 10, 7, 21, tzinfo=KST)), {0, 1, 2})

    def test_next_week_is_open(self):
        self.assertEqual(self._locked(datetime(2026, 10, 3, 23, tzinfo=KST)), set())

    def test_no_week_start(self):
        mgr = _manager()
        mgr.week_start = None
        self.assertEqual(mgr._locked_days(), set())


@patch.object(ScheduleManager, "save_backup")
class ReadjustTest(unittest.TestCase):
    def test_past_days_keep_assignment(self, _save):
        mgr = _manager()
        mgr.assignments = {0: ["a"], 1: ["a"], 2: ["a"], 3: ["a"]}
        now = datetime(2026, 10, 7, 15, tzinfo=KST)
        with patch.object(schedule_manager, "POOL_SIZE", 1), \
                patch.object(schedule_manager, "get_current_kst_time", return_value=now):
            registered, changes = mgr.toggle_self_deployment(2, "a")

        self.assertTrue(registered)
        self.assertEqual(mgr.assignments[0], ["a"])
        self.assertEqual(mgr.assignments[1], ["a"])
        self.assertEqual(mgr.assignments[3], ["b"])
        self.assertEqual(changes, [(3, ["b"], ["a"])])

    def test_no_change_returns_empty(self, _save):
        mgr = _manager()
        mgr.assignments = {0: ["a", "b"], 1: ["a", "b"]}
        with patch.object(schedule_manager, "get_current_kst_time", return_value=datetime(2026, 10, 4, tzinfo=KST)):
            _, changes = mgr.toggle_self_deployment(0, "a")
        self.assertEqual(changes, [])


class ReadjustNoticeTest(unittest.TestCase):
    def test_notice_lists_people_and_days(self):
        mgr = _manager()
        text = schedule_views._readjust_notice(mgr, [(3, ["b"], ["a"])])
        self.assertEqual(text, "남은 요일 편성이 바뀌었습니다. 목요일 Bob 추가, Alice 빠짐.")

    def test_empty(self):
        self.assertEqual(schedule_views._readjust_notice(_manager(), []), "")


if __name__ == "__main__":
    unittest.main()
