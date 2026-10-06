import unittest
from types import SimpleNamespace
from unittest import mock

from models.team_data import TeamData
from services import discord_service
from services.discord_service import DiscordService


class FakeRole(SimpleNamespace):
    def __hash__(self):
        return hash(self.name)

    def is_default(self):
        return False


class MissingGroupRoleTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_role_skips_only_that_group(self):
        role_a = FakeRole(name='A조')
        alice = SimpleNamespace(display_name='alice', name='alice', nick=None, global_name=None, roles=[])
        bob = SimpleNamespace(display_name='bob', name='bob', nick=None, global_name=None, roles=[])
        guild = SimpleNamespace(roles=[role_a], members=[alice, bob])
        groups = [
            [('팀1', TeamData(name='팀1', players=['alice']), 0)],
            [('팀2', TeamData(name='팀2', players=['bob']), 0)],
        ]
        service = DiscordService(processor=SimpleNamespace(client=None), team_data_manager=SimpleNamespace())
        service._apply_role_updates = mock.AsyncMock()

        with mock.patch.object(discord_service.settings, 'GROUP_CHANNEL_IDS', {'A': 1, 'B': 2}), \
             self.assertLogs('scrim-bot.discord_service', level='INFO') as logs:
            await service.handle_discord_roles(guild, groups)

        updates = service._apply_role_updates.await_args.args[0]
        self.assertEqual([(m.display_name, add) for m, _, add in updates], [('alice', {role_a})])
        errors = [r.getMessage() for r in logs.records if r.levelname == 'ERROR']
        self.assertEqual(len(errors), 1)
        self.assertIn('B조', errors[0])


if __name__ == '__main__':
    unittest.main()
