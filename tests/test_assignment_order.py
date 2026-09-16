import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config.settings import settings
from services.discord_service import DiscordService


class AssignmentOrderTest(unittest.IsolatedAsyncioTestCase):
    async def test_roles_assigned_before_group_announcements(self):
        service = DiscordService(processor=SimpleNamespace(), team_data_manager=SimpleNamespace())
        order = []

        async def _rec(name):
            async def _cb(*a, **k):
                order.append(name)
            return _cb

        service.clear_channel_messages = await _rec("clear")
        service.send_group_announcement_with_image = await _rec("announce")
        service.handle_discord_roles = await _rec("roles")
        service.rename_voice_channels = await _rec("rename")
        service.create_group_announcement_message = lambda *a, **k: "msg"

        original = settings.GROUP_CHANNEL_IDS
        settings.GROUP_CHANNEL_IDS = {"A": 111}
        self.addCleanup(lambda: setattr(settings, "GROUP_CHANNEL_IDS", original))

        fake_channel = SimpleNamespace(name="A조", id=111)
        guild = SimpleNamespace(get_channel=lambda cid: fake_channel)
        groups = [[("TeamA", SimpleNamespace(), 1500.0)]]

        with patch("services.discord_service.get_rest_day_info", new=AsyncMock(return_value={"is_rest_day": False})):
            await service.send_notices(guild, groups)

        self.assertIn("roles", order)
        self.assertIn("announce", order)
        self.assertLess(
            order.index("roles"), order.index("announce"),
            f"역할이 공지보다 먼저 처리되어야 함. 실제 순서: {order}",
        )


if __name__ == "__main__":
    unittest.main()
