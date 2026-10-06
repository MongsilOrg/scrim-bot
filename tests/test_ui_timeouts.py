import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from commands.ui import roster_views, schedule_views
from models.schedule_manager import ScheduleManager
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


CLOSED_TEXT = "시간이 지나 닫혔습니다. 버튼을 다시 눌러주세요."


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


class DeployViewTimeoutTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mgr = ScheduleManager()
        self.mgr.week_label = "test"
        self.mgr.availability = {"u1": {0, 1}}
        self.mgr.assignments = {0: ["u1"], 1: ["u1"]}

    async def test_timeout_without_click_edits_first_message(self):
        view = schedule_views.DeployView(self.mgr, "u1")
        view.message = MagicMock()
        view.message.edit = AsyncMock()

        await view.on_timeout()

        self.assertIn(CLOSED_TEXT, _view_text(view.message.edit.await_args.kwargs["view"]))

    async def test_timeout_after_click_uses_latest_interaction(self):
        view = schedule_views.DeployView(self.mgr, "u1")
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        click = MagicMock()
        click.response.edit_message = AsyncMock()
        click.edit_original_response = AsyncMock()

        with patch.object(ScheduleManager, "save_backup"), \
                patch.object(schedule_views, "_refresh_schedule_status", new=AsyncMock()):
            await view._make_day_callback(0)(click)
            await view.on_timeout()

        click.response.edit_message.assert_awaited_once_with(view=view)
        self.assertIn(CLOSED_TEXT, _view_text(click.edit_original_response.await_args.kwargs["view"]))
        view.message.edit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
