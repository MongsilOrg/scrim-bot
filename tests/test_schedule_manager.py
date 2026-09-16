import unittest
from unittest.mock import patch

from models.schedule_manager import ScheduleManager, ACTIVE_DAYS


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


class TestStatusTextSorting(unittest.TestCase):
    def setUp(self):
        self.mgr = ScheduleManager()
        self.mgr.week_label = '3/24 ~ 3/30'

    @patch.object(ScheduleManager, 'save_backup')
    def test_sorted_by_name(self, mock_save):
        self.mgr.register_schedule('u3', 'Charlie', {0, 1})
        self.mgr.register_schedule('u1', 'Alice', {0, 2})
        self.mgr.register_schedule('u2', 'Bob', set(), '출장')

        all_admins = [('u1', 'Alice'), ('u2', 'Bob'), ('u3', 'Charlie')]
        text = self.mgr.get_status_text(all_admins)

        alice_pos = text.index('Alice')
        bob_pos = text.index('Bob')
        charlie_pos = text.index('Charlie')
        self.assertLess(alice_pos, bob_pos)
        self.assertLess(bob_pos, charlie_pos)


class TestDeployViewAssignment(unittest.TestCase):
    def setUp(self):
        self.mgr = ScheduleManager()
        self.mgr.week_label = '3/24 ~ 3/30'

    @patch.object(ScheduleManager, 'save_backup')
    def test_assigned_days_identified(self, mock_save):
        self.mgr.register_schedule('u1', 'Alice', {0, 1, 2, 3, 4, 5})
        self.mgr.register_schedule('u2', 'Bob', {0, 2})
        self.mgr.generate_assignments()

        u2_assigned = {
            d for d in ACTIVE_DAYS
            if 'u2' in self.mgr.assignments.get(d, [])
        }
        self.assertTrue(u2_assigned.issubset({0, 2}))


if __name__ == '__main__':
    unittest.main()
