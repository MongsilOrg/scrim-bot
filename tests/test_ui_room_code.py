import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from commands import room_code


def _member(*role_names):
    return SimpleNamespace(roles=[SimpleNamespace(name=n, id=0) for n in role_names])


class SubWeatherPermissionTest(unittest.IsolatedAsyncioTestCase):
    async def _denial(self, member, *, admin=False, rest_day=False):
        with patch.object(room_code, "is_admin", return_value=admin), \
                patch.object(room_code, "get_rest_day_info", new=AsyncMock(return_value={"is_rest_day": rest_day})):
            return await room_code._sub_weather_denial(member, "A")

    async def test_admin_always_allowed(self):
        self.assertIsNone(await self._denial(_member(), admin=True))

    async def test_weekday_group_member_denied(self):
        denied = await self._denial(_member("A조"))
        self.assertEqual(denied, "서브 날씨는 관리자만 고를 수 있습니다.")

    async def test_rest_day_group_member_allowed(self):
        self.assertIsNone(await self._denial(_member("A조"), rest_day=True))

    async def test_rest_day_other_group_denied(self):
        denied = await self._denial(_member("B조"), rest_day=True)
        self.assertIn("A조 참가자", denied)

    async def test_rest_day_lookup_timeout_counts_as_weekday(self):
        async def slow(*_args, **_kwargs):
            await asyncio.sleep(1)
            return {"is_rest_day": True}

        with patch.object(room_code, "REST_DAY_CHECK_TIMEOUT", 0.01), \
                patch.object(room_code, "is_admin", return_value=False), \
                patch.object(room_code, "get_rest_day_info", new=slow):
            denied = await room_code._sub_weather_denial(_member("A조"), "A")
        self.assertEqual(denied, "서브 날씨는 관리자만 고를 수 있습니다.")


if __name__ == "__main__":
    unittest.main()
