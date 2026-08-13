"""강제취소 기능 테스트: 강제취소 선택 뷰."""
import unittest

from models.team_data import TeamData


def _collect_labels(component):
    """LayoutView 트리에서 모든 Button label을 모은다."""
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
    """LayoutView 트리에서 모든 Select option value를 모은다."""
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
    """LayoutView 트리에서 모든 TextDisplay content를 모은다."""
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
        # 기존 버튼도 유지
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
        # 30팀 전부 옵션에 포함 (Discord Select 25개 제한을 분할로 우회)
        self.assertEqual(set(values), set(teams.keys()))
        self.assertEqual(len(values), 30)

    def test_exactly_25_teams_single_select(self):
        from commands.ui.views import ForceCancelSelectView

        teams = self._teams(25)
        view = ForceCancelSelectView(parent_view=None, teams=teams)
        # 정확히 25개면 분할 없이 단일 Select
        self.assertEqual(len(view.selects), 1)
        self.assertEqual(len(_collect_select_values(view)), 25)


class ForceCancelSelectIdentifyTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_interaction_payload_values(self):
        """선택 식별은 발화한 드롭다운의 raw payload(values)를 직접 읽는다."""
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
        # 두 번째 청크(>25)에 속한 팀을 선택해도 정확히 식별되어야 함
        await view.team_select_callback(_FakeInteraction("T27"))
        confirm_texts = _collect_text_contents(captured["confirm_view"])
        self.assertTrue(any("T27" in text for text in confirm_texts))


if __name__ == "__main__":
    unittest.main()
