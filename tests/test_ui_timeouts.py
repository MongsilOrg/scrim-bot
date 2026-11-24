import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from commands.ui import roster_views
from utils import layout_helpers


def _view_text(view):
    texts = []

    def walk(item):
        content = getattr(item, "content", None)
        if isinstance(content, str):
            texts.append(content)
        for child in getattr(item, "children", []) or []:
            walk(child)

    for item in view.children:
        walk(item)
    return "\n".join(texts)


CLOSED_TEXT = "제한 시간이 지났습니다.\n처음부터 다시 진행해주세요."


class TimeoutViewTextTest(unittest.TestCase):
    def test_default_text(self):
        self.assertIn(CLOSED_TEXT, _view_text(layout_helpers.timeout_view()))


class TeamSelectionTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_closes_message_on_timeout(self):
        parent = SimpleNamespace(group_teams=[])
        view = roster_views.TeamSelectionView(parent)
        view.message = MagicMock()
        view.message.edit = AsyncMock()

        await view.on_timeout()

        edited = view.message.edit.await_args.kwargs["view"]
        self.assertIn(CLOSED_TEXT, _view_text(edited))


if __name__ == "__main__":
    unittest.main()
