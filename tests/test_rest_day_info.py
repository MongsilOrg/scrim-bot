import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from services import holidays_api


class TestRestDayInfo(unittest.IsolatedAsyncioTestCase):
    async def test_saturday_is_rest_day(self):
        with patch("services.holidays_api.get_holiday_names", new=AsyncMock(return_value=[])):
            info = await holidays_api.get_rest_day_info(date(2026, 8, 22))
            self.assertTrue(info["is_rest_day"])
            self.assertTrue(info["is_saturday"])
            self.assertEqual(info["labels"], ["토요일"])

    async def test_sunday_is_rest_day(self):
        with patch("services.holidays_api.get_holiday_names", new=AsyncMock(return_value=[])):
            info = await holidays_api.get_rest_day_info(date(2026, 8, 23))
            self.assertTrue(info["is_rest_day"])
            self.assertTrue(info["is_sunday"])
            self.assertEqual(info["labels"], ["일요일"])

    async def test_holiday_weekday_is_rest_day(self):
        with patch("services.holidays_api.get_holiday_names", new=AsyncMock(return_value=["광복절"])):
            info = await holidays_api.get_rest_day_info(date(2026, 8, 17))
            self.assertTrue(info["is_rest_day"])
            self.assertTrue(info["is_holiday"])
            self.assertEqual(info["labels"], ["광복절"])

    async def test_plain_weekday_is_not_rest_day(self):
        with patch("services.holidays_api.get_holiday_names", new=AsyncMock(return_value=[])):
            for d in (date(2026, 8, 12), date(2026, 8, 17), date(2026, 9, 1)):
                info = await holidays_api.get_rest_day_info(d)
                self.assertFalse(info["is_rest_day"], f"{d} 평일인데 휴무 처리")
                self.assertEqual(info["labels"], [])


if __name__ == "__main__":
    unittest.main()
