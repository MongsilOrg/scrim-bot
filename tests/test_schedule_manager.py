"""ScheduleManager UX 개선 관련 테스트"""
import unittest
from unittest.mock import patch

from models.schedule_manager import ScheduleManager, ACTIVE_DAYS


class TestRegisterSchedule(unittest.TestCase):
    """register_schedule 통합 등록 메서드 테스트"""

    def setUp(self):
        self.mgr = ScheduleManager()
        self.mgr.week_label = '3/24 ~ 3/30'

    @patch.object(ScheduleManager, '_save_backup')
    def test_full_participation(self, mock_save):
        """전체 참가: 모든 요일 선택, 사유 없음"""
        self.mgr.register_schedule('u1', 'Admin1', {0, 1, 2, 3, 4, 5})
        self.assertEqual(self.mgr.availability['u1'], {0, 1, 2, 3, 4, 5})
        self.assertNotIn('u1', self.mgr.absence_reasons)

    @patch.object(ScheduleManager, '_save_backup')
    def test_partial_participation(self, mock_save):
        """부분 참가: 월화수목만 선택 → 불참 사유 없음"""
        self.mgr.register_schedule('u1', 'Admin1', {0, 1, 2, 3})
        self.assertEqual(self.mgr.availability['u1'], {0, 1, 2, 3})
        self.assertNotIn('u1', self.mgr.absence_reasons)

    @patch.object(ScheduleManager, '_save_backup')
    def test_partial_participation_without_reason(self, mock_save):
        """부분 참가: 월화수 참가, 사유 없음 → 불참 사유 미기록"""
        self.mgr.register_schedule('u1', 'Admin1', {0, 1, 2})
        self.assertEqual(self.mgr.availability['u1'], {0, 1, 2})
        self.assertNotIn('u1', self.mgr.absence_reasons)

    @patch.object(ScheduleManager, '_save_backup')
    def test_full_absence(self, mock_save):
        """전체 불참: 0개 선택 + 사유 있음"""
        self.mgr.register_schedule('u1', 'Admin1', set(), '출장')
        self.assertEqual(self.mgr.availability['u1'], set())
        self.assertEqual(self.mgr.absence_reasons['u1'], {-1: '출장'})

    @patch.object(ScheduleManager, '_save_backup')
    def test_full_absence_no_reason(self, mock_save):
        """전체 불참: 0개 선택 + 사유 없음 → 기본 사유"""
        self.mgr.register_schedule('u1', 'Admin1', set(), None)
        self.assertEqual(self.mgr.absence_reasons['u1'], {-1: '사유 없음'})

    @patch.object(ScheduleManager, '_save_backup')
    def test_overwrite_previous_response(self, mock_save):
        """기존 응답을 덮어쓰기: 전체불참 → 부분참가"""
        self.mgr.register_schedule('u1', 'Admin1', set(), '출장')
        self.assertEqual(self.mgr.absence_reasons['u1'], {-1: '출장'})

        self.mgr.register_schedule('u1', 'Admin1', {0, 1})
        self.assertEqual(self.mgr.availability['u1'], {0, 1})
        self.assertNotIn('u1', self.mgr.absence_reasons)

    @patch.object(ScheduleManager, '_save_backup')
    def test_overwrite_partial_to_full_absence(self, mock_save):
        """기존 응답 덮어쓰기: 부분참가 → 전체불참"""
        self.mgr.register_schedule('u1', 'Admin1', {0, 1, 2}, '약속')
        self.mgr.register_schedule('u1', 'Admin1', set(), '입원')
        self.assertEqual(self.mgr.availability['u1'], set())
        self.assertEqual(self.mgr.absence_reasons['u1'], {-1: '입원'})


class TestRemoveResponse(unittest.TestCase):
    """응답 삭제 기능 테스트"""

    def setUp(self):
        self.mgr = ScheduleManager()
        self.mgr.week_label = '3/24 ~ 3/30'

    @patch.object(ScheduleManager, '_save_backup')
    def test_remove_existing_response(self, mock_save):
        """등록된 응답 삭제"""
        self.mgr.register_schedule('u1', 'Admin1', {0, 1}, '약속')
        self.assertTrue(self.mgr.remove_response('u1'))
        self.assertNotIn('u1', self.mgr.availability)
        self.assertNotIn('u1', self.mgr.absence_reasons)

    @patch.object(ScheduleManager, '_save_backup')
    def test_remove_nonexistent(self, mock_save):
        """미등록 응답 삭제 시도"""
        self.assertFalse(self.mgr.remove_response('u_nonexistent'))


class TestStatusTextSorting(unittest.TestCase):
    """상태 텍스트 이름순 정렬 테스트"""

    def setUp(self):
        self.mgr = ScheduleManager()
        self.mgr.week_label = '3/24 ~ 3/30'

    @patch.object(ScheduleManager, '_save_backup')
    def test_sorted_by_name(self, mock_save):
        """응답 현황이 이름순으로 정렬되는지 확인"""
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

    @patch.object(ScheduleManager, '_save_backup')
    def test_assignment_day_count(self, mock_save):
        """편성 결과에 요일별 인원수가 표시되는지 확인"""
        self.mgr.register_schedule('u1', 'Alice', {0, 1, 2})
        self.mgr.register_schedule('u2', 'Bob', {0, 1})
        self.mgr.generate_assignments()

        all_admins = [('u1', 'Alice'), ('u2', 'Bob')]
        text = self.mgr.get_status_text(all_admins)

        # 월요일에 2명 배정
        self.assertIn('**월** (2명)', text)
        # 총 N건 표시
        self.assertIn('총', text)
        self.assertIn('건', text)


class TestDeployViewAssignment(unittest.TestCase):
    """투입 뷰에서 배정/미배정 구분 테스트"""

    def setUp(self):
        self.mgr = ScheduleManager()
        self.mgr.week_label = '3/24 ~ 3/30'

    @patch.object(ScheduleManager, '_save_backup')
    def test_assigned_days_identified(self, mock_save):
        """특정 사용자의 배정 요일이 올바르게 식별되는지"""
        self.mgr.register_schedule('u1', 'Alice', {0, 1, 2, 3, 4, 5})
        self.mgr.register_schedule('u2', 'Bob', {0, 2})
        self.mgr.generate_assignments()

        # u2는 0(월), 2(수)에만 참가 가능 → 해당 요일에 배정
        u2_assigned = {
            d for d in ACTIVE_DAYS
            if 'u2' in self.mgr.assignments.get(d, [])
        }
        self.assertTrue(u2_assigned.issubset({0, 2}))


if __name__ == '__main__':
    unittest.main()
