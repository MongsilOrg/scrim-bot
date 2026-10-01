import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from services.discord_service import DiscordService


class FakeRole(SimpleNamespace):
    def __hash__(self):
        return hash(self.name)

    def is_default(self):
        return self.name == '@everyone'


EVERYONE, OTHER, A, B = (FakeRole(name=n) for n in ('@everyone', '기타', 'A조', 'B조'))


def member(name, roles):
    m = SimpleNamespace(display_name=name, roles=list(roles))
    m.edit = AsyncMock()
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    return m


class RoleUpdatesTest(unittest.IsolatedAsyncioTestCase):
    async def test_split_between_edit_and_role_calls(self):
        service = DiscordService(processor=SimpleNamespace(), team_data_manager=SimpleNamespace())
        moved = member('이동', [EVERYONE, OTHER, A])
        singles = [member(f'추가{i}', [EVERYONE]) for i in range(4)]
        updates = [(m, set(), {A}) for m in singles] + [(moved, {A}, {B})]

        await service._apply_role_updates(updates)

        moved.edit.assert_awaited_once_with(roles=[OTHER, B])
        moved.add_roles.assert_not_awaited()
        moved.remove_roles.assert_not_awaited()
        edited = sum(m.edit.await_count for m in singles)
        added = sum(m.add_roles.await_count for m in singles)
        self.assertEqual((edited, added), (1, 3))

    async def test_remove_only_member(self):
        service = DiscordService(processor=SimpleNamespace(), team_data_manager=SimpleNamespace())
        left = member('제외', [EVERYONE, B])

        await service._apply_role_updates([(left, {B}, set())])

        left.remove_roles.assert_awaited_once_with(B)
        left.edit.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
