import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

from services import holidays_api
from utils.helpers import KST


def _season(name, start, end):
    return (name, datetime.fromisoformat(start).replace(tzinfo=KST), datetime.fromisoformat(end).replace(tzinfo=KST))


SEASONS = [
    _season("Season21", "2026-08-13 12:00", "2026-11-12 09:00"),
    _season("Pre-Season22", "2026-11-12 12:00", "2026-11-19 12:00"),
]


class TestRestDayInfo(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch("services.holidays_api.is_preseason", new=AsyncMock(return_value=False))
        self.addCleanup(patcher.stop)
        patcher.start()

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


    async def test_preseason_weekday_is_rest_day(self):
        with patch("services.holidays_api.get_holiday_names", new=AsyncMock(return_value=[])), \
                patch("services.holidays_api.is_preseason", new=AsyncMock(return_value=True)):
            info = await holidays_api.get_rest_day_info(date(2026, 11, 12))
            self.assertTrue(info["is_rest_day"])
            self.assertTrue(info["is_preseason"])
            self.assertEqual(info["labels"], ["프리시즌"])


class TestPreseason(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        holidays_api._season_cache.update({'day': None, 'tried_at': None, 'seasons': []})
        for target, value in (("BSER_API_KEY", "test-key"), ("SCRIM_START_HOUR", 20)):
            patcher = patch.object(holidays_api.settings, target, value)
            self.addCleanup(patcher.stop)
            patcher.start()

    def _now(self, iso):
        return patch("services.holidays_api.get_current_kst_time",
                     return_value=datetime.fromisoformat(iso).replace(tzinfo=KST))

    async def test_scrim_start_inside_preseason(self):
        fetch = AsyncMock(return_value=SEASONS)
        with self._now("2026-11-12 13:00"), patch("services.holidays_api._fetch_seasons", new=fetch):
            self.assertTrue(await holidays_api.is_preseason(date(2026, 11, 12)))
            self.assertTrue(await holidays_api.is_preseason(date(2026, 11, 18)))
            # 프리시즌이 12시에 끝나는 날의 20시 스크림은 정규 시즌
            self.assertFalse(await holidays_api.is_preseason(date(2026, 11, 11)))
        self.assertEqual(fetch.await_count, 1)

    async def test_next_season_missing_refetches_after_retry_gap(self):
        fetch = AsyncMock(side_effect=[SEASONS[:1], SEASONS])
        with patch("services.holidays_api._fetch_seasons", new=fetch):
            with self._now("2026-11-12 09:30"):
                self.assertFalse(await holidays_api.is_preseason(date(2026, 11, 12)))
            with self._now("2026-11-12 09:35"):
                self.assertFalse(await holidays_api.is_preseason(date(2026, 11, 12)))
            self.assertEqual(fetch.await_count, 1)
            with self._now("2026-11-12 12:01"):
                self.assertTrue(await holidays_api.is_preseason(date(2026, 11, 12)))
        self.assertEqual(fetch.await_count, 2)

    async def test_fetch_failure_counts_as_regular(self):
        fetch = AsyncMock(return_value=None)
        with self._now("2026-11-12 13:00"), patch("services.holidays_api._fetch_seasons", new=fetch):
            self.assertFalse(await holidays_api.is_preseason(date(2026, 11, 12)))
            self.assertFalse(await holidays_api.is_preseason(date(2026, 11, 12)))
        self.assertEqual(fetch.await_count, 1)

    async def test_no_api_key_skips_request(self):
        fetch = AsyncMock(return_value=SEASONS)
        with patch.object(holidays_api.settings, "BSER_API_KEY", ""), \
                patch("services.holidays_api._fetch_seasons", new=fetch):
            self.assertFalse(await holidays_api.is_preseason(date(2026, 11, 12)))
        fetch.assert_not_awaited()

    def test_guide_text_names_preseason(self):
        from commands.ui.roster_views import build_rest_day_guide_view
        text = build_rest_day_guide_view("팀", "1", preseason=True).children[0].children[0].content
        self.assertIn("프리시즌 스크림 자율 진행 안내", text)
        self.assertNotIn("주말", text)


if __name__ == "__main__":
    unittest.main()
