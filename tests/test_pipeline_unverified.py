import unittest

from commands.team_pipeline import _apply_unverified_transition
from models.team_data_manager import TeamDataManager


def make_manager(markers=()) -> TeamDataManager:
    mgr = TeamDataManager.__new__(TeamDataManager)
    mgr.unverified_teams = set(markers)
    mgr._mmr_dirty = False
    mgr.save_backup = lambda: None
    return mgr


ROSTER = ['a', 'b', 'c']


class UnverifiedRenameTest(unittest.TestCase):
    def _apply(self, mgr, *, is_maintenance, new_members=ROSTER, validated=True):
        _apply_unverified_transition(
            mgr, is_maintenance=is_maintenance,
            old_members=ROSTER, new_members=new_members,
            old_name='옛팀명', new_name='새팀명', validated=validated,
        )

    def test_rename_only_during_maintenance_moves_marker(self):
        mgr = make_manager({'옛팀명'})
        self._apply(mgr, is_maintenance=True)
        self.assertEqual(mgr.unverified_teams, {'새팀명'})

    def test_rename_only_during_maintenance_without_marker_stays_clean(self):
        mgr = make_manager()
        self._apply(mgr, is_maintenance=True)
        self.assertEqual(mgr.unverified_teams, set())

    def test_rename_after_full_check_clears_marker(self):
        mgr = make_manager({'옛팀명'})
        self._apply(mgr, is_maintenance=False)
        self.assertEqual(mgr.unverified_teams, set())

    def test_roster_change_without_check_moves_marker(self):
        mgr = make_manager({'옛팀명'})
        self._apply(mgr, is_maintenance=False, new_members=['a', 'b', 'd'], validated=False)
        self.assertEqual(mgr.unverified_teams, {'새팀명'})

    def test_roster_change_without_check_does_not_add_marker(self):
        mgr = make_manager()
        self._apply(mgr, is_maintenance=False, new_members=['a', 'b', 'd'], validated=False)
        self.assertEqual(mgr.unverified_teams, set())


if __name__ == '__main__':
    unittest.main()
