"""조편성 알고리즘 테스트 (models/team_processor.py)

TeamProcessor 의 조편성 핵심 로직을 실데이터 구조(TeamData)로 검증한다:
- _process_team_groups: 8배수 제한, 시드팀 우선 선발, 예비팀 분리
- _apply_snake_draft / _apply_grouped_snake_pattern / _apply_snake_pattern:
  MMR 기반 스네이크 드래프트 분배
- _are_players_matching / ensure_seeds_marked: 시드 매칭 규칙
  (정규화 기준 전원 일치 + 3~4명)

TeamProcessor 생성자는 구글 시트에 붙으므로 __new__ 로 우회 생성한다.
"""
import time
import unittest
from unittest import mock

from models.team_data import TeamData
from models.team_processor import TeamProcessor


def make_processor():
    """구글 시트 연결 없이 TeamProcessor 인스턴스를 만든다."""
    processor = TeamProcessor.__new__(TeamProcessor)
    processor.seeds_data = None
    processor._seeds_loaded_at = 0.0
    processor._set_test_accounts({})
    return processor


def team_entry(name, mmr, players=None):
    """_process_team_groups 입력 형식인 (팀명, TeamData, MMR) 튜플"""
    if players is None:
        players = [f'{name}_p{i}' for i in range(1, 4)]
    return (name, TeamData(name=name, players=list(players)), float(mmr))


def make_team_infos(count, top_mmr=1600.0, step=10.0):
    """MMR 이 top_mmr 부터 step 씩 감소하는 팀 count 개"""
    return [
        team_entry(f'팀{i + 1:02d}', top_mmr - i * step)
        for i in range(count)
    ]


def names(entries):
    return [entry[0] for entry in entries]


def priorities_for(team_info, seed_names=()):
    """시드팀만 1순위, 나머지 2순위인 우선순위 딕셔너리"""
    return {
        name: 1 if name in seed_names else 2
        for name in names(team_info)
    }


class GroupCountLimitTest(unittest.IsolatedAsyncioTestCase):
    """8배수 제한: 완성된 8팀 조만 편성하고 잔여 팀은 예비로 뺀다."""

    async def test_16_teams_two_full_groups_no_reserves(self):
        team_info = make_team_infos(16)
        groups, unmatched = await make_processor()._process_team_groups(
            team_info, priorities_for(team_info)
        )

        self.assertEqual(len(groups), 2)
        self.assertEqual([len(g) for g in groups], [8, 8])
        self.assertEqual(unmatched, [])
        # 팀 유실/중복 없음
        placed = [name for group in groups for name in names(group)]
        self.assertCountEqual(placed, names(team_info))

    async def test_19_teams_two_groups_three_lowest_reserved(self):
        team_info = make_team_infos(19)
        groups, unmatched = await make_processor()._process_team_groups(
            team_info, priorities_for(team_info)
        )

        self.assertEqual(len(groups), 2)
        self.assertEqual([len(g) for g in groups], [8, 8])
        # 예비는 MMR 하위 3팀
        self.assertCountEqual(names(unmatched), ['팀17', '팀18', '팀19'])

    async def test_7_teams_no_groups_all_reserved(self):
        team_info = make_team_infos(7)
        groups, unmatched = await make_processor()._process_team_groups(
            team_info, priorities_for(team_info)
        )

        self.assertEqual(groups, [])
        self.assertCountEqual(names(unmatched), names(team_info))

    async def test_8_teams_single_group_keeps_mmr_order(self):
        """1개 조는 스네이크 없이 MMR 내림차순 그대로"""
        team_info = make_team_infos(8)
        groups, unmatched = await make_processor()._process_team_groups(
            team_info, priorities_for(team_info)
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(unmatched, [])
        self.assertEqual(names(groups[0]), names(team_info))


class SeedPriorityTest(unittest.IsolatedAsyncioTestCase):
    """정원 초과 시 시드팀(1순위)이 비시드팀(2순위)보다 먼저 선발된다."""

    async def test_low_mmr_seeds_beat_high_mmr_nonseeds(self):
        # 정원 8에 10팀, 시드 2팀이 MMR 최하위인 상황
        nonseeds = make_team_infos(8, top_mmr=1600.0)
        seeds = [
            team_entry('시드1', 900.0),
            team_entry('시드2', 800.0),
        ]
        team_info = nonseeds + seeds
        priorities = priorities_for(team_info, seed_names={'시드1', '시드2'})

        groups, unmatched = await make_processor()._process_team_groups(
            team_info, priorities
        )

        self.assertEqual(len(groups), 1)
        placed = names(groups[0])
        # 시드팀은 MMR 최하위임에도 선발
        self.assertIn('시드1', placed)
        self.assertIn('시드2', placed)
        # 비시드 하위 2팀(팀07, 팀08)이 예비로 밀린다
        self.assertCountEqual(names(unmatched), ['팀07', '팀08'])
        # 선발 후 조 내부는 MMR 내림차순으로 재정렬
        mmrs = [entry[2] for entry in groups[0]]
        self.assertEqual(mmrs, sorted(mmrs, reverse=True))

    async def test_nine_seed_teams_top8_by_mmr_one_reserved(self):
        """시드팀만 9개면 시드 내 MMR 순으로 8팀 선발, 최하위 1팀 예비"""
        team_info = make_team_infos(9)
        priorities = priorities_for(team_info, seed_names=set(names(team_info)))

        groups, unmatched = await make_processor()._process_team_groups(
            team_info, priorities
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(names(groups[0]), names(team_info)[:8])
        self.assertEqual(names(unmatched), ['팀09'])

    async def test_seed_overflow_excludes_all_nonseeds(self):
        """시드가 8배수를 넘으면 비시드는 MMR 무관 전원 예비"""
        seeds = make_team_infos(9, top_mmr=1000.0)  # 시드 9팀 (저MMR)
        nonseeds = [
            team_entry('비시드1', 2000.0),
            team_entry('비시드2', 1900.0),
            team_entry('비시드3', 1800.0),
        ]
        team_info = seeds + nonseeds
        priorities = priorities_for(team_info, seed_names=set(names(seeds)))

        groups, unmatched = await make_processor()._process_team_groups(
            team_info, priorities
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(names(groups[0]), names(seeds)[:8])
        self.assertCountEqual(
            names(unmatched), ['팀09', '비시드1', '비시드2', '비시드3']
        )


class SnakeDraftTest(unittest.IsolatedAsyncioTestCase):
    """MMR 내림차순 팀들이 2개 조 단위 스네이크 패턴으로 분배된다.

    패턴 (MMR 내림차순 인덱스 기준): 1조 0,3,4,7,8,11,12,15 / 2조 나머지
    """

    GROUP0_IDX = [0, 3, 4, 7, 8, 11, 12, 15]
    GROUP1_IDX = [1, 2, 5, 6, 9, 10, 13, 14]

    async def test_two_groups_snake_order(self):
        team_info = make_team_infos(16)
        groups, _ = await make_processor()._process_team_groups(
            team_info, priorities_for(team_info)
        )

        expected0 = [names(team_info)[i] for i in self.GROUP0_IDX]
        expected1 = [names(team_info)[i] for i in self.GROUP1_IDX]
        self.assertEqual(names(groups[0]), expected0)
        self.assertEqual(names(groups[1]), expected1)

    def test_three_groups_last_group_gets_remainder_in_mmr_order(self):
        """홀수 조: 앞 2개 조는 스네이크, 마지막 조는 잔여 팀 MMR 순"""
        team_info = make_team_infos(24)
        initial = [team_info[i:i + 8] for i in range(0, 24, 8)]

        groups = make_processor()._apply_snake_draft(initial)

        all_names = names(team_info)
        self.assertEqual(
            names(groups[0]), [all_names[i] for i in self.GROUP0_IDX]
        )
        self.assertEqual(
            names(groups[1]), [all_names[i] for i in self.GROUP1_IDX]
        )
        self.assertEqual(names(groups[2]), all_names[16:24])

    def test_four_groups_pairwise_snake_no_team_lost(self):
        """4개 조는 앞뒤 두 조씩 쌍으로 스네이크 분배, 팀 유실과 중복 없음"""
        team_info = make_team_infos(32)
        initial = [team_info[i:i + 8] for i in range(0, 32, 8)]

        groups = make_processor()._apply_snake_draft(initial)

        all_names = names(team_info)
        self.assertEqual(
            names(groups[2]), [all_names[16 + i] for i in self.GROUP0_IDX]
        )
        self.assertEqual(
            names(groups[3]), [all_names[16 + i] for i in self.GROUP1_IDX]
        )
        placed = [name for group in groups for name in names(group)]
        self.assertCountEqual(placed, all_names)


class PlayersMatchingTest(unittest.TestCase):
    """_are_players_matching: 정규화 기준 전원 일치 + 3~4명일 때만 시드 적용"""

    def setUp(self):
        self.processor = make_processor()

    def test_four_player_exact_match(self):
        self.assertTrue(self.processor._are_players_matching(
            ['Alpha', 'Bravo', 'Charlie', 'Delta'],
            ['Alpha', 'Bravo', 'Charlie', 'Delta'],
        ))

    def test_order_independent(self):
        self.assertTrue(self.processor._are_players_matching(
            ['Charlie', 'Alpha', 'Bravo'],
            ['Alpha', 'Bravo', 'Charlie'],
        ))

    def test_normalization_variants_match(self):
        # 닉네임 정규화와 같은 규칙: 대소문자, 앞뒤 공백, 중간 공백 개수 차이로 시드 매칭이 빠지지 않는다
        self.assertTrue(self.processor._are_players_matching(
            ['  AL  PHA ', 'bravo', 'ChArLiE'],
            ['al pha', ' Bravo', 'charlie  '],
        ))

    def test_partial_overlap_not_matching(self):
        self.assertFalse(self.processor._are_players_matching(
            ['Alpha', 'Bravo', 'Charlie'],
            ['Alpha', 'Bravo', 'Delta'],
        ))

    def test_size_mismatch_not_applied(self):
        # 3명 팀 vs 4명 시드 → 전원 일치가 아니므로 미적용
        self.assertFalse(self.processor._are_players_matching(
            ['Alpha', 'Bravo', 'Charlie'],
            ['Alpha', 'Bravo', 'Charlie', 'Delta'],
        ))

    def test_two_player_teams_not_applied(self):
        # 전원 일치라도 2명이면 시드 미적용
        self.assertFalse(self.processor._are_players_matching(
            ['Alpha', 'Bravo'],
            ['Alpha', 'Bravo'],
        ))

    def test_five_player_teams_not_applied(self):
        self.assertFalse(self.processor._are_players_matching(
            ['A', 'B', 'C', 'D', 'E'],
            ['A', 'B', 'C', 'D', 'E'],
        ))


class EnsureSeedsMarkedTest(unittest.IsolatedAsyncioTestCase):
    """ensure_seeds_marked: 시드 매칭 결과를 TeamData 에 마킹한다"""

    def make_fresh_processor(self, seeds):
        """캐시가 신선한 상태의 processor (시트 재로드 없음 보장)"""
        processor = make_processor()
        processor.seeds_data = {'seeds': seeds}
        processor._seeds_loaded_at = time.monotonic()
        processor._load_seeds_data = mock.AsyncMock()
        return processor

    async def test_matching_team_marked_with_seed_name(self):
        processor = self.make_fresh_processor([
            {'team_name': '시드팀A', 'players': ['Alpha', 'Bravo', 'Charlie']},
        ])
        teams = {
            '우리팀': TeamData(name='우리팀',
                               players=['Alpha', 'Bravo', 'Charlie']),
            '남의팀': TeamData(name='남의팀',
                               players=['Delta', 'Echo', 'Foxtrot']),
        }

        await processor.ensure_seeds_marked(teams)

        self.assertTrue(teams['우리팀'].is_seed)
        self.assertEqual(teams['우리팀'].seed_name, '시드팀A')
        self.assertFalse(teams['남의팀'].is_seed)
        self.assertIsNone(teams['남의팀'].seed_name)
        # 캐시가 신선하면 시트를 다시 읽지 않는다
        processor._load_seeds_data.assert_not_awaited()

    async def test_normalization_variants_match_through_full_path(self):
        processor = self.make_fresh_processor([
            {'team_name': '시드팀A', 'players': ['alpha', 'BRAVO', ' charlie ']},
        ])
        teams = {
            '우리팀': TeamData(name='우리팀',
                               players=[' ALPHA', 'bravo ', 'Charlie']),
        }

        await processor.ensure_seeds_marked(teams)

        self.assertTrue(teams['우리팀'].is_seed)

    async def test_two_player_team_not_marked(self):
        processor = self.make_fresh_processor([
            {'team_name': '시드팀A', 'players': ['Alpha', 'Bravo']},
        ])
        teams = {
            '우리팀': TeamData(name='우리팀', players=['Alpha', 'Bravo']),
        }

        await processor.ensure_seeds_marked(teams)

        self.assertFalse(teams['우리팀'].is_seed)

    async def test_stale_seed_flags_cleared_on_rerun(self):
        # 이전 실행에서 시드였던 팀이 시드 명단에서 빠지면 플래그가 풀린다
        processor = self.make_fresh_processor([
            {'team_name': '시드팀A', 'players': ['Alpha', 'Bravo', 'Charlie']},
        ])
        team = TeamData(name='우리팀', players=['Delta', 'Echo', 'Foxtrot'])
        team.is_seed = True
        team.seed_name = '옛시드'

        await processor.ensure_seeds_marked({'우리팀': team})

        self.assertFalse(team.is_seed)
        self.assertIsNone(team.seed_name)

    async def test_empty_seed_data_marks_nothing(self):
        processor = self.make_fresh_processor([])
        teams = {
            '우리팀': TeamData(name='우리팀',
                               players=['Alpha', 'Bravo', 'Charlie']),
        }

        await processor.ensure_seeds_marked(teams)

        self.assertFalse(teams['우리팀'].is_seed)


if __name__ == '__main__':
    unittest.main()
