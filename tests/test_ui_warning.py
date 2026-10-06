import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from commands.ui import warning_modals


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


def _target(send_side_effect=None):
    target = MagicMock()
    target.id = 42
    target.display_name = "대상"
    target.name = "target"
    target.mention = "<@42>"
    target.send = AsyncMock(side_effect=send_side_effect)
    return target


def _forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")


class SanctionDmResultTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_true_when_sent(self):
        self.assertTrue(await warning_modals.send_sanction_dm(_target(), "주의", "대타"))

    async def test_returns_false_when_dm_blocked(self):
        self.assertFalse(await warning_modals.send_sanction_dm(_target(_forbidden()), "주의", "대타"))


class SanctionResultCardTest(unittest.IsolatedAsyncioTestCase):
    async def _submit(self, target, choice, add_result, team_name=None, detail=""):
        modal = warning_modals.WarningReasonModal(target)
        modal.reason_radio = SimpleNamespace(value=choice)
        modal.detail_input = SimpleNamespace(value=detail)

        interaction = MagicMock()
        interaction.user.display_name = "관리자"
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        manager = MagicMock()
        manager.get_warning_manager.return_value.add_warning = AsyncMock(return_value=add_result)
        manager.get_team_data_manager.return_value.find_user_team.return_value = team_name
        with patch.object(warning_modals.BotManager, "get_instance", return_value=manager):
            await modal.on_submit(interaction)
        return _view_text(interaction.followup.send.await_args.kwargs["view"])

    async def test_warning_with_blocked_dm_and_team(self):
        warning = {"warning_count": 1, "duration_days": 3, "restricted_until": "2026-10-09"}
        text = await self._submit(
            _target(_forbidden()), "지각", (True, "ok", warning, []), team_name="몽실팀",
        )
        self.assertIn(warning_modals.DM_FAILED_TEXT, text)
        self.assertIn("현재 **몽실팀** 팀에 등록되어 있습니다.", text)

    async def test_caution_does_not_show_team(self):
        text = await self._submit(_target(), "대타", (True, "ok", None, []), team_name="몽실팀")
        self.assertNotIn("몽실팀", text)
        self.assertNotIn(warning_modals.DM_FAILED_TEXT, text)


if __name__ == "__main__":
    unittest.main()
