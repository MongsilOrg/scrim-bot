import unittest

from models.team_data import TeamData


def _collect_labels(component):
    labels = []

    def walk(item):
        label = getattr(item, "label", None)
        if label is not None:
            labels.append(label)
        for child in getattr(item, "children", []) or []:
            walk(child)

    for item in component.children:
        walk(item)
    return labels


def _collect_select_values(component):
    values = []

    def walk(item):
        for opt in getattr(item, "options", []) or []:
            values.append(opt.value)
        for child in getattr(item, "children", []) or []:
            walk(child)

    for item in component.children:
        walk(item)
    return values


def _collect_text_contents(component):
    texts = []

    def walk(item):
        content = getattr(item, "content", None)
        if isinstance(content, str):
            texts.append(content)
        for child in getattr(item, "children", []) or []:
            walk(child)

    for item in component.children:
        walk(item)
    return texts


class ManageButtonTest(unittest.TestCase):
    def test_dashboard_has_manage_button(self):
        from commands.ui.views import TeamInputView

        view = TeamInputView(scrim_day=22, scrim_month=5, scrim_weekday="목")
        labels = _collect_labels(view)
        self.assertIn("관리", labels)
        self.assertIn("신청/수정", labels)
        self.assertIn("취소", labels)


class ForceCancelSelectViewTest(unittest.TestCase):
    def _teams(self, n):
        return {
            f"T{i:02d}": TeamData(name=f"T{i:02d}", players=[f"p{i}"], staff=[])
            for i in range(n)
        }

    def test_lists_all_teams_as_options(self):
        from commands.ui.views import ForceCancelSelectView

        teams = self._teams(5)
        view = ForceCancelSelectView(parent_view=None, teams=teams)
        values = _collect_select_values(view)
        self.assertEqual(set(values), set(teams.keys()))

    def test_splits_over_25_teams_into_multiple_selects(self):
        from commands.ui.views import ForceCancelSelectView

        teams = self._teams(30)
        view = ForceCancelSelectView(parent_view=None, teams=teams)
        values = _collect_select_values(view)
        # Discord Select 옵션은 최대 25개
        self.assertEqual(set(values), set(teams.keys()))
        self.assertEqual(len(values), 30)

    def test_exactly_25_teams_single_select(self):
        from commands.ui.views import ForceCancelSelectView

        teams = self._teams(25)
        view = ForceCancelSelectView(parent_view=None, teams=teams)
        self.assertEqual(len(view.selects), 1)
        self.assertEqual(len(_collect_select_values(view)), 25)


class ForceCancelSelectIdentifyTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_interaction_payload_values(self):
        from commands.ui.views import ForceCancelSelectView

        teams = {f"T{i:02d}": TeamData(name=f"T{i:02d}") for i in range(30)}
        captured = {}

        class _FakeResp:
            def is_done(self):
                return False

            async def edit_message(self, **kwargs):
                captured["confirm_view"] = kwargs.get("view")

        class _FakeInteraction:
            def __init__(self, value):
                self.data = {"values": [value]}
                self.response = _FakeResp()

            async def original_response(self):
                return None

        view = ForceCancelSelectView(parent_view=None, teams=teams)
        await view.team_select_callback(_FakeInteraction("T27"))
        confirm_texts = _collect_text_contents(captured["confirm_view"])
        self.assertTrue(any("T27" in text for text in confirm_texts))


if __name__ == "__main__":
    unittest.main()
