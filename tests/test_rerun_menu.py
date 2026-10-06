import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from commands.ui import views


class RerunMenuTest(unittest.IsolatedAsyncioTestCase):
    async def test_rerun_result_view(self):
        mgr = MagicMock()
        mgr.rerun_team_assignment.return_value = (True, "조편성을 다시 실행합니다.")
        bot = SimpleNamespace(get_team_data_manager=lambda: mgr)
        with patch.object(views.BotManager, "get_instance", return_value=bot), \
             patch.object(views, "is_admin", return_value=True):
            view = await views.TeamInputView._rerun_assignment(object(), SimpleNamespace(user="관리자"))
        mgr.rerun_team_assignment.assert_called_once()
        self.assertIsInstance(view, views.LayoutView)

    async def test_rerun_requires_admin(self):
        mgr = MagicMock()
        bot = SimpleNamespace(get_team_data_manager=lambda: mgr)
        with patch.object(views.BotManager, "get_instance", return_value=bot), \
             patch.object(views, "is_admin", return_value=False):
            await views.TeamInputView._rerun_assignment(object(), SimpleNamespace(user="일반"))
        mgr.rerun_team_assignment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
