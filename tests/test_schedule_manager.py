import unittest
from unittest.mock import patch

from models.schedule_manager import ScheduleManager


class TestRegisterSchedule(unittest.TestCase):
    def setUp(self):
        self.mgr = ScheduleManager()
        self.mgr.week_label = '3/24 ~ 3/30'

    @patch.object(ScheduleManager, 'save_backup')
    def test_full_participation(self, mock_save):
        self.mgr.register_schedule('u1', 'Admin1', {0, 1, 2, 3, 4, 5})
        self.assertEqual(self.mgr.availability['u1'], {0, 1, 2, 3, 4, 5})
        self.assertNotIn('u1', self.mgr.absence_reasons)

    @patch.object(ScheduleManager, 'save_backup')
    def test_full_absence(self, mock_save):
        self.mgr.register_schedule('u1', 'Admin1', set(), '출장')
        self.assertEqual(self.mgr.availability['u1'], set())
        self.assertEqual(self.mgr.absence_reasons['u1'], {-1: '출장'})

    @patch.object(ScheduleManager, 'save_backup')
    def test_full_absence_no_reason(self, mock_save):
        self.mgr.register_schedule('u1', 'Admin1', set(), None)
        self.assertEqual(self.mgr.absence_reasons['u1'], {-1: '사유 없음'})

    @patch.object(ScheduleManager, 'save_backup')
    def test_overwrite_previous_response(self, mock_save):
        self.mgr.register_schedule('u1', 'Admin1', set(), '출장')
        self.assertEqual(self.mgr.absence_reasons['u1'], {-1: '출장'})

        self.mgr.register_schedule('u1', 'Admin1', {0, 1})
        self.assertEqual(self.mgr.availability['u1'], {0, 1})
        self.assertNotIn('u1', self.mgr.absence_reasons)

    @patch.object(ScheduleManager, 'save_backup')
    def test_overwrite_partial_to_full_absence(self, mock_save):
        self.mgr.register_schedule('u1', 'Admin1', {0, 1, 2}, '약속')
        self.mgr.register_schedule('u1', 'Admin1', set(), '입원')
        self.assertEqual(self.mgr.availability['u1'], set())
        self.assertEqual(self.mgr.absence_reasons['u1'], {-1: '입원'})


if __name__ == '__main__':
    unittest.main()
