import unittest
from unittest import mock

from commands.team_pipeline import _update_mmr_message_for_individual_team


class MmrChannelAfterEditTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_resolved_channel_without_mmr_message(self):
        mgr = mock.Mock()
        mgr.mmr_message = None
        channel = object()
        mgr.resolve_mmr_channel.return_value = channel
        mgr.update_mmr_message = mock.AsyncMock()

        await _update_mmr_message_for_individual_team(mgr)

        mgr.update_mmr_message.assert_awaited_once_with(channel)

    async def test_skips_when_no_channel(self):
        mgr = mock.Mock()
        mgr.resolve_mmr_channel.return_value = None
        mgr.update_mmr_message = mock.AsyncMock()

        await _update_mmr_message_for_individual_team(mgr)

        mgr.update_mmr_message.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
