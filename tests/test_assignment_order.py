"""조편성 Discord 플로우 순서 회귀 테스트.

버그: send_notices가 조편채널 공지(역할 핑 포함)를 먼저 보내고, 그 다음에야
handle_discord_roles로 조 역할을 재배정해서 — 공지 시점엔 핑이 이전 역할
보유자에게 가고 신규 멤버는 권한 게이팅된 채널을 못 봐 '공지/조 혼선'이 발생.
역할 재배정이 조별 공지보다 먼저 일어나야 한다.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config.settings import settings
from services.discord_service import DiscordService


class AssignmentOrderTest(unittest.IsolatedAsyncioTestCase):
    async def test_roles_assigned_before_group_announcements(self):
        service = DiscordService(processor=SimpleNamespace())
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

        # A조 채널 1개만 두고, A조에 팀이 있는 상황
        original = settings.GROUP_CHANNEL_IDS
        settings.GROUP_CHANNEL_IDS = {"A": 111}
        self.addCleanup(lambda: setattr(settings, "GROUP_CHANNEL_IDS", original))

        fake_channel = SimpleNamespace(name="A조", id=111)
        guild = SimpleNamespace(get_channel=lambda cid: fake_channel)
        groups = [[("TeamA", SimpleNamespace(), 1500.0)]]

        # 휴무일 조회는 외부 HTTP 호출이므로 테스트에서는 스텁 처리
        with patch("utils.helpers.get_rest_day_info", new=AsyncMock(return_value={"is_rest_day": False})):
            await service.send_notices(guild, groups)

        self.assertIn("roles", order)
        self.assertIn("announce", order)
        # 핵심: 역할 재배정이 조별 공지보다 먼저
        self.assertLess(
            order.index("roles"), order.index("announce"),
            f"역할이 공지보다 먼저 처리되어야 함. 실제 순서: {order}",
        )


if __name__ == "__main__":
    unittest.main()
