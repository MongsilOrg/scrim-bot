import unittest
from datetime import datetime
from unittest import mock

from config.settings import settings
from models.team_data import TeamData, TeamMmrResult
from models.team_data_manager import TeamDataManager
from commands.team_pipeline import (
    _apply_unverified_transition,
    _fetch_team_mmr_result,
    _log_edit_diff,
)


def make_team(name: str, players: list, staff: list = None) -> TeamData:
    return TeamData(name=name, players=players, staff=staff or [])


def make_manager(markers=()) -> TeamDataManager:
    mgr = TeamDataManager.__new__(TeamDataManager)
    mgr.unverified_teams = set(markers)
    mgr._mmr_dirty = False
    mgr.save_backup = lambda: None
    return mgr


class TeamTimeRulesTest(unittest.TestCase):
    DEADLINE = settings.TEAM_REGISTRATION_DEADLINE_HOUR

    def _rules(self, *, started=False, scrim_day=None, is_edit=False, hour=12):
        mgr = TeamDataManager.__new__(TeamDataManager)
        mgr.is_team_assignment_started = started
        mgr.scrim_day = scrim_day
        mgr.scrim_month = 8 if scrim_day else None
        return mgr.check_team_time_rules(
            datetime(2026, 8, 11, hour, 0), is_edit=is_edit
        )

    def test_time_rule_matrix(self):
        with self.subTest('조편성 완료면 차단, 등록 문구'):
            allowed, msg = self._rules(started=True)
            self.assertFalse(allowed)
            self.assertIn('팀 등록이 불가능', msg)
        with self.subTest('조편성 완료 + 수정이면 수정 문구'):
            allowed, msg = self._rules(started=True, is_edit=True)
            self.assertFalse(allowed)
            self.assertIn('팀 수정이 불가능', msg)
        with self.subTest('스크림 당일 마감 이후면 차단'):
            allowed, msg = self._rules(scrim_day=11, hour=self.DEADLINE)
            self.assertFalse(allowed)
            self.assertIn('추가 등록이 불가능', msg)
        with self.subTest('스크림 당일 마감 이후 수정도 차단, 수정 문구'):
            allowed, msg = self._rules(scrim_day=11, hour=self.DEADLINE, is_edit=True)
            self.assertFalse(allowed)
            self.assertIn('팀 수정이 불가능', msg)
        with self.subTest('스크림 당일 마감 전이면 허용'):
            allowed, _ = self._rules(scrim_day=11, hour=self.DEADLINE - 1)
            self.assertTrue(allowed)
        with self.subTest('스크림 날짜가 다르면 마감 이후에도 허용'):
            allowed, _ = self._rules(scrim_day=12, hour=self.DEADLINE)
            self.assertTrue(allowed)


class MmrFetchResultTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, fetch_result=None, fetch_error=None):
        processor = mock.Mock()
        if fetch_error:
            processor.fetch_team_mmr = mock.AsyncMock(side_effect=fetch_error)
        else:
            processor.fetch_team_mmr = mock.AsyncMock(return_value=(True, [], fetch_result))
        return await _fetch_team_mmr_result(processor, make_team('팀', ['a', 'b', 'c']))

    async def test_mmr_fetch_result_rules(self):
        with self.subTest('조회 성공이면 확정값 그대로'):
            result = await self._run(fetch_result=TeamMmrResult(mmr=77.0, confirmed=True))
            self.assertEqual((result.mmr, result.confirmed), (77.0, True))
        with self.subTest('실제 0점도 확정으로 전달'):
            result = await self._run(fetch_result=TeamMmrResult(mmr=0.0, confirmed=True))
            self.assertEqual((result.mmr, result.confirmed), (0.0, True))
        with self.subTest('미확정은 그대로 미확정, 실패 멤버 유지'):
            result = await self._run(fetch_result=TeamMmrResult(failed_players=('a',)))
            self.assertFalse(result.confirmed)
            self.assertEqual(result.failed_players, ('a',))
        with self.subTest('예외면 전원 실패로 미확정'):
            result = await self._run(fetch_error=RuntimeError('api down'))
            self.assertFalse(result.confirmed)
            self.assertEqual(result.failed_players, ('a', 'b', 'c'))


class UnverifiedTransitionTest(unittest.TestCase):
    def test_unverified_transition_rules(self):
        with self.subTest('점검 중 로스터 변경이면 마커 추가, 개명 시 구명 제거'):
            mgr = make_manager({'옛팀명'})
            _apply_unverified_transition(
                mgr, is_maintenance=True,
                old_players=['a', 'b', 'c'], new_players=['a', 'b', 'd'],
                old_name='옛팀명', new_name='새팀명',
            )
            self.assertEqual(mgr.unverified_teams, {'새팀명'})
            self.assertTrue(mgr._mmr_dirty)

        with self.subTest('점검 중이라도 정규화 기준 동일 로스터면 마커 미추가'):
            mgr = make_manager()
            _apply_unverified_transition(
                mgr, is_maintenance=True,
                old_players=['Alpha', 'bravo', 'C'], new_players=[' alpha ', 'BRAVO', 'c'],
                old_name='팀', new_name='팀',
            )
            self.assertEqual(mgr.unverified_teams, set())
            self.assertFalse(mgr._mmr_dirty)

        with self.subTest('평시 수정이면 기존 마커 제거'):
            mgr = make_manager({'팀'})
            _apply_unverified_transition(
                mgr, is_maintenance=False,
                old_players=['a', 'b', 'c'], new_players=['a', 'b', 'd'],
                old_name='팀', new_name='팀',
            )
            self.assertEqual(mgr.unverified_teams, set())
            self.assertTrue(mgr._mmr_dirty)


class EditDiffTest(unittest.TestCase):
    def _run(self, old: TeamData, new: TeamData):
        manager = mock.Mock()
        interaction = mock.Mock()
        return _log_edit_diff(interaction, manager, old.name, old, new, 100.0)

    def test_edit_diff_sets(self):
        with self.subTest('교체와 스태프 추가가 함께 집계된다'):
            added, removed = self._run(
                make_team('팀', ['a', 'b', 'c']),
                make_team('팀', ['a', 'b', 'd'], staff=['s']),
            )
            self.assertEqual(added, {'d', 's'})
            self.assertEqual(removed, {'c'})

        with self.subTest('무변경이면 빈 집합'):
            added, removed = self._run(
                make_team('팀', ['a', 'b', 'c']),
                make_team('팀', ['a', 'b', 'c']),
            )
            self.assertEqual((added, removed), (set(), set()))

        with self.subTest('대소문자/공백만 고친 수정은 변경으로 집계되지 않는다'):
            added, removed = self._run(
                make_team('팀', ['Alpha', 'bravo', 'C']),
                make_team('팀', [' alpha ', 'BRAVO', 'c']),
            )
            self.assertEqual((added, removed), (set(), set()))


class SanctionPolicyTest(unittest.TestCase):
    def test_reason_type_mapping_fixed(self):
        from commands.ui.warning_modals import REASON_TYPE

        self.assertEqual(REASON_TYPE, {
            '지각': '경고',
            '대타': '주의',
            '기타주의': '주의',
            '기타경고': '경고',
        })


class TeamSizeBoundaryTest(unittest.TestCase):
    def _validate(self, players, staff=()):
        from utils.validators import validate_team_data

        is_valid, _ = validate_team_data(make_team('팀', list(players), list(staff)))
        return is_valid

    def test_player_count_boundaries(self):
        with self.subTest('선수 2명 거부'):
            self.assertFalse(self._validate(['a', 'b']))
        with self.subTest('선수 3명 허용'):
            self.assertTrue(self._validate(['a', 'b', 'c']))
        with self.subTest('선수 4명 허용'):
            self.assertTrue(self._validate(['a', 'b', 'c', 'd']))
        with self.subTest('선수 5명 거부'):
            self.assertFalse(self._validate(['a', 'b', 'c', 'd', 'e']))

    def test_staff_count_boundaries(self):
        with self.subTest('스태프 3명 허용'):
            self.assertTrue(self._validate(['a', 'b', 'c'], ['s1', 's2', 's3']))
        with self.subTest('스태프 4명 거부'):
            self.assertFalse(self._validate(['a', 'b', 'c'], ['s1', 's2', 's3', 's4']))


class MaintenanceNicknameCheckTest(unittest.IsolatedAsyncioTestCase):
    """점검 중 404는 닉네임 캐시 밖 멤버에게만 발생."""

    async def _check(self, known, members, *, hint, season_maintenance=False):
        from utils import validators

        api = mock.AsyncMock()
        api.get_user_uid.side_effect = lambda m: 'uid' if m in known else None
        api.check_server_maintenance.return_value = season_maintenance
        client = mock.MagicMock()
        client.return_value.__aenter__.return_value = api
        with mock.patch.object(validators, 'BSERAPIClient', client):
            return await validators.validate_members_api(members, maintenance_hint=hint)

    async def test_cached_majority_during_maintenance_passes(self):
        result = await self._check({'a', 'b', 'c'}, ['a', 'b', 'c', 'new'], hint=True)
        self.assertEqual(result, (True, [], True))

    async def test_partial_failure_without_maintenance_rejects(self):
        result = await self._check({'a', 'b', 'c'}, ['a', 'b', 'c', 'typo'], hint=False)
        self.assertEqual(result, (False, ['typo'], False))

    async def test_all_valid_during_maintenance_is_not_unverified(self):
        result = await self._check({'a', 'b', 'c'}, ['a', 'b', 'c'], hint=True)
        self.assertEqual(result, (True, [], False))


if __name__ == '__main__':
    unittest.main()
