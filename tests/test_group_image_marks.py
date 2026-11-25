import unittest

from models.team_data import TeamData
from services.image_generator import ImageGenerator


def _row(team, marks=None):
    return ImageGenerator._build_team_row_html(1, team.name, team, marks=marks)


class GroupImageMarksTest(unittest.TestCase):
    def test_row_without_marks_lists_the_roster_as_it_is(self):
        html = _row(TeamData(name='알파', players=['a1', 'a2', 'a3'], staff=['코치']))

        self.assertNotIn('joined', html)
        self.assertEqual(html.count('class="separator"'), 2)

    def test_sub_is_coloured_and_the_absent_player_stays(self):
        team = TeamData(name='알파', players=['a1', 'a2', '대타<1>'])
        html = _row(team, {'sub': ['대타<1>'], 'staff': [], 'out': ['a3']})

        sep = '<span class="separator">,</span>'
        self.assertIn(f'a1{sep}a2{sep}a3{sep}<span class="joined">대타&lt;1&gt;</span>', html)

    def test_added_staff_is_coloured_after_the_staff_of_the_team(self):
        team = TeamData(name='알파', players=['a1', 'a2', 'a3'], staff=['코치'])
        html = _row(team, {'sub': [], 'staff': ['코치', '분석'], 'out': []})

        self.assertIn('<div class="cell-sub staff">코치, <span class="joined">분석</span></div>', html)

    def test_marks_left_from_a_roster_changed_later_are_not_drawn_twice(self):
        team = TeamData(name='알파', players=['a1', 'a2', 'a3'])
        html = _row(team, {'sub': ['나간대타'], 'staff': [], 'out': ['a3']})

        self.assertNotIn('joined', html)
        self.assertEqual(html.count('a3'), 1)

    def test_marks_are_given_to_the_row_of_their_team_only(self):
        teams = [('알파', TeamData(name='알파', players=['a1'])), ('베타', TeamData(name='베타', players=['a1']))]
        html = ImageGenerator._create_mmr_html_template(
            teams, '20:00', server_info={'is_tournament': False}, marks={'베타': {'sub': ['a1'], 'out': ['빠진선수']}},
        )

        self.assertEqual(html.count('<span class="joined">a1</span>'), 1)
        self.assertEqual(html.count('빠진선수'), 1)
