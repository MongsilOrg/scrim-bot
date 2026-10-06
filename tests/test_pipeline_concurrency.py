import asyncio
import unittest
from datetime import datetime
from unittest import mock

from config.settings import settings
from models.team_data import TeamData
from models.team_data_manager import TeamDataManager


def make_manager(teams=None, *, started=False) -> TeamDataManager:
    mgr = TeamDataManager.__new__(TeamDataManager)
    mgr._teams_lock = asyncio.Lock()
    mgr.teams = {}
    mgr.team_by_member = {}
    mgr._mmr_dirty = False
    mgr.is_team_assignment_started = started
    mgr.scrim_day = None
    mgr.scrim_month = None
    mgr.save_backup = lambda: None
    for name, team in (teams or {}).items():
        mgr.teams[name] = team
        mgr._add_member_index(name, team)
    return mgr


def user(uid: int):
    return mock.Mock(id=uid)


class ConcurrentAddTeamTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_member_in_two_concurrent_applications(self):
        mgr = make_manager()
        first = TeamData(name='알파팀', players=['겹침', 'a1', 'a2'])
        second = TeamData(name='베타팀', players=['겹침', 'b1', 'b2'])

        results = await asyncio.gather(
            mgr.add_team('알파팀', first, user(1)),
            mgr.add_team('베타팀', second, user(2)),
        )

        self.assertEqual(sorted(ok for ok, _ in results), [False, True])
        reason = next(msg for ok, msg in results if not ok)
        self.assertIn('겹침', reason)
        self.assertEqual(len(mgr.teams), 1)

    async def test_same_normalized_team_name_rejected(self):
        mgr = make_manager()
        results = await asyncio.gather(
            mgr.add_team('Team ER', TeamData(name='Team ER', players=['a', 'b', 'c']), user(1)),
            mgr.add_team('team  er', TeamData(name='team  er', players=['d', 'e', 'f']), user(2)),
        )
        self.assertEqual(sorted(ok for ok, _ in results), [False, True])
        self.assertEqual(len(mgr.teams), 1)

    async def test_add_after_assignment_started_rejected(self):
        mgr = make_manager(started=True)
        ok, reason = await mgr.add_team('알파팀', TeamData(name='알파팀', players=['a', 'b', 'c']), user(1))
        self.assertFalse(ok)
        self.assertTrue(reason)
        self.assertEqual(mgr.teams, {})


class ReplaceTeamRulesTest(unittest.IsolatedAsyncioTestCase):
    async def test_edit_cannot_take_member_registered_meanwhile(self):
        mgr = make_manager({
            '알파팀': TeamData(name='알파팀', players=['a1', 'a2', 'a3'], user_id='1'),
            '베타팀': TeamData(name='베타팀', players=['b1', 'b2', 'b3'], user_id='2'),
        })
        edited = TeamData(name='알파팀', players=['a1', 'a2', 'b1'], user_id='1')

        ok, reason = await mgr.replace_team('알파팀', edited, 0.0)

        self.assertFalse(ok)
        self.assertIn('b1', reason)
        self.assertEqual(mgr.teams['알파팀'].players, ['a1', 'a2', 'a3'])

    async def test_edit_after_deadline_rejected(self):
        mgr = make_manager({'알파팀': TeamData(name='알파팀', players=['a1', 'a2', 'a3'])})
        mgr.scrim_day, mgr.scrim_month = 6, 10
        late = datetime(2026, 10, 6, settings.TEAM_REGISTRATION_DEADLINE_HOUR, 5)
        edited = TeamData(name='알파팀', players=['a1', 'a2', 'a4'])

        with mock.patch('models.team_data_manager.get_current_kst_time', return_value=late):
            ok, _ = await mgr.replace_team('알파팀', edited, 0.0)

        self.assertFalse(ok)
        self.assertEqual(mgr.teams['알파팀'].players, ['a1', 'a2', 'a3'])

    async def test_roster_change_skips_rules(self):
        mgr = make_manager({
            '알파팀': TeamData(name='알파팀', players=['a1', 'a2', 'a3']),
            '베타팀': TeamData(name='베타팀', players=['b1', 'b2', 'b3']),
        }, started=True)
        edited = TeamData(name='알파팀', players=['a1', 'a2', 'b1'])

        ok, _ = await mgr.replace_team('알파팀', edited, 0.0, enforce_rules=False)

        self.assertTrue(ok)
        self.assertEqual(mgr.teams['알파팀'].players, ['a1', 'a2', 'b1'])

    async def test_rename_into_existing_name_always_rejected(self):
        mgr = make_manager({
            '알파팀': TeamData(name='알파팀', players=['a1', 'a2', 'a3']),
            '베타팀': TeamData(name='베타팀', players=['b1', 'b2', 'b3']),
        }, started=True)
        edited = TeamData(name='베타팀', players=['a1', 'a2', 'a3'])

        ok, _ = await mgr.replace_team('알파팀', edited, 0.0, enforce_rules=False)

        self.assertFalse(ok)
        self.assertEqual(mgr.teams['베타팀'].players, ['b1', 'b2', 'b3'])


if __name__ == '__main__':
    unittest.main()
