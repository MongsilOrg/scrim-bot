import asyncio
import unittest
from datetime import datetime
from unittest import mock

from models.team_data import TeamData
from models.team_data_manager import TeamDataManager


class FakeWarningManager:
    def __init__(self, restricted):
        self.worksheet = object()
        self._restricted = {name.lower() for name in restricted}
        self.checked = []

    def is_restricted(self, target_id=None, target_name=None, check_date=None):
        self.checked.append(target_name)
        if target_name and target_name.lower() in self._restricted:
            return True, '2026-08-31'
        return False, None


def make_manager(teams, warning_manager) -> TeamDataManager:
    mgr = TeamDataManager.__new__(TeamDataManager)
    mgr.teams = teams
    mgr.client = None
    bot_manager = mock.Mock()
    bot_manager.get_warning_manager.return_value = warning_manager
    patcher = mock.patch('models.team_data_manager.BotManager')
    started = patcher.start()
    started.get_instance.return_value = bot_manager
    mgr._test_patcher = patcher
    return mgr


class MemberRestrictionScopeTest(unittest.TestCase):
    NOW = datetime(2026, 8, 29, 16, 0)

    def tearDown(self):
        mock.patch.stopall()

    def _check(self, *, existing_teams, new_team, previous_members=None, restricted=('horrific',)):
        wm = FakeWarningManager(restricted)
        mgr = make_manager(existing_teams, wm)
        allowed, msg = asyncio.run(
            mgr.check_member_restrictions(self.NOW, new_team=new_team, previous_members=previous_members)
        )
        return allowed, msg, wm

    def test_other_team_restriction_does_not_block_new_application(self):
        existing = {'피어리스': TeamData(name='피어리스', players=['Horrific', '준라가스', '할수있다'])}
        new_team = TeamData(name='ㅌㅈㅇ', players=['서르', '물냉파', '무뇌의화신고이솔'])

        allowed, msg, wm = self._check(existing_teams=existing, new_team=new_team)

        self.assertTrue(allowed, msg)
        self.assertNotIn('Horrific', wm.checked)

    def test_restricted_new_member_is_blocked(self):
        new_team = TeamData(name='피어리스', players=['Horrific', '준라가스', '할수있다'])

        allowed, msg, _ = self._check(existing_teams={}, new_team=new_team)

        self.assertFalse(allowed)
        self.assertIn('Horrific', msg)
        self.assertIn('2026-08-31', msg)

    def test_edit_checks_only_newly_added_member(self):
        new_team = TeamData(name='기체', players=['할수있다', '준라가스'], staff=['horrific'])

        allowed, msg, wm = self._check(
            existing_teams={},
            new_team=new_team,
            previous_members=['할수있다', '준라가스'],
        )

        self.assertFalse(allowed)
        self.assertIn('horrific', msg)
        self.assertEqual(wm.checked, ['horrific'])

    def test_edit_without_new_member_passes(self):
        new_team = TeamData(name='기체', players=['할수있다', '준라가스'])

        allowed, _, wm = self._check(
            existing_teams={},
            new_team=new_team,
            previous_members=['준라가스', '할수있다'],
        )

        self.assertTrue(allowed)
        self.assertEqual(wm.checked, [])


if __name__ == '__main__':
    unittest.main()
