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

    async def test_preseason_error_keeps_weekend(self):
        with patch("services.holidays_api.get_holiday_names", new=AsyncMock(return_value=[])), \
                patch("services.holidays_api.is_preseason", new=AsyncMock(side_effect=AttributeError)):
            info = await holidays_api.get_rest_day_info(date(2026, 8, 22))
            self.assertTrue(info["is_rest_day"])
            self.assertFalse(info["is_preseason"])


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
            # 다음 행이 없어도 정규 시즌이 끝난 뒤 1주는 프리시즌으로 보고, 행은 계속 다시 받음
            with self._now("2026-11-12 09:30"):
                self.assertTrue(await holidays_api.is_preseason(date(2026, 11, 12)))
            with self._now("2026-11-12 09:35"):
                self.assertTrue(await holidays_api.is_preseason(date(2026, 11, 12)))
            self.assertEqual(fetch.await_count, 1)
            with self._now("2026-11-12 12:01"):
                self.assertTrue(await holidays_api.is_preseason(date(2026, 11, 12)))
        self.assertEqual(fetch.await_count, 2)

    async def test_gap_after_regular_season_is_preseason_for_a_week(self):
        fetch = AsyncMock(return_value=SEASONS[:1])
        with self._now("2026-11-12 13:00"), patch("services.holidays_api._fetch_seasons", new=fetch):
            self.assertFalse(await holidays_api.is_preseason(date(2026, 11, 11)))
            self.assertTrue(await holidays_api.is_preseason(date(2026, 11, 12)))
            self.assertTrue(await holidays_api.is_preseason(date(2026, 11, 18)))
            self.assertFalse(await holidays_api.is_preseason(date(2026, 11, 19)))

    async def test_gap_with_next_regular_row_only_is_preseason(self):
        rows = [SEASONS[0], _season("Season22", "2026-11-19 12:00", "2027-02-18 09:00")]
        fetch = AsyncMock(return_value=rows)
        with self._now("2026-11-12 13:00"), patch("services.holidays_api._fetch_seasons", new=fetch):
            self.assertTrue(await holidays_api.is_preseason(date(2026, 11, 12)))
            self.assertFalse(await holidays_api.is_preseason(date(2026, 11, 19)))

    async def test_gap_after_preseason_is_regular(self):
        fetch = AsyncMock(return_value=[_season("Pre-Season22", "2026-11-12 12:00", "2026-11-19 12:00")])
        with self._now("2026-11-19 13:00"), patch("services.holidays_api._fetch_seasons", new=fetch):
            self.assertFalse(await holidays_api.is_preseason(date(2026, 11, 19)))

    async def test_concurrent_calls_share_one_fetch(self):
        import asyncio

        async def slow():
            await asyncio.sleep(0.01)
            return SEASONS
        fetch = AsyncMock(side_effect=slow)
        with self._now("2026-11-12 13:00"), patch("services.holidays_api._fetch_seasons", new=fetch):
            results = await asyncio.gather(*(holidays_api.is_preseason(date(2026, 11, 12)) for _ in range(3)))
        self.assertEqual(results, [True, True, True])
        self.assertEqual(fetch.await_count, 1)

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

    def test_missing_end_runs_until_next_start(self):
        rows = [
            {"seasonName": "Season21", "seasonStart": "2026-08-13 12:00:00", "seasonEnd": None},
            {"seasonName": "Pre-Season22", "seasonStart": "2026-11-12 12:00:00", "seasonEnd": ""},
            {"seasonName": "broken", "seasonStart": None, "seasonEnd": "2026-01-01 00:00:00"},
        ]
        seasons = holidays_api._parse_seasons(rows)
        self.assertEqual([s[0] for s in seasons], ["Season21", "Pre-Season22"])
        at = lambda d: datetime.fromisoformat(d).replace(tzinfo=KST)
        self.assertEqual(holidays_api._season_at(seasons, at("2026-11-11 20:00")), "Season21")
        self.assertEqual(holidays_api._season_at(seasons, at("2026-11-12 20:00")), "Pre-Season22")
        self.assertEqual(holidays_api._season_at(seasons, at("2027-01-01 20:00")), "Pre-Season22")

    async def test_open_ended_season_keeps_refetching(self):
        open_rows = holidays_api._parse_seasons([
            {"seasonName": "Season21", "seasonStart": "2026-08-13 12:00:00", "seasonEnd": None}])
        both = holidays_api._parse_seasons([
            {"seasonName": "Season21", "seasonStart": "2026-08-13 12:00:00", "seasonEnd": None},
            {"seasonName": "Pre-Season22", "seasonStart": "2026-11-12 12:00:00", "seasonEnd": None}])
        fetch = AsyncMock(side_effect=[open_rows, both])
        with patch("services.holidays_api._fetch_seasons", new=fetch):
            with self._now("2026-11-12 09:30"):
                self.assertFalse(await holidays_api.is_preseason(date(2026, 11, 12)))
            with self._now("2026-11-12 09:35"):
                self.assertFalse(await holidays_api.is_preseason(date(2026, 11, 12)))
            self.assertEqual(fetch.await_count, 1)
            with self._now("2026-11-12 12:01"):
                self.assertTrue(await holidays_api.is_preseason(date(2026, 11, 12)))
        self.assertEqual(fetch.await_count, 2)

    async def test_rank_season_switches_on_first_sunday_noon_after_regular_start(self):
        rows = holidays_api._parse_seasons([
            {"seasonID": 39, "seasonName": "Season20", "seasonStart": "2026-05-07 12:00:00", "seasonEnd": "2026-08-06 09:00:00"},
            {"seasonID": 40, "seasonName": "Pre-Season21", "seasonStart": "2026-08-06 12:00:00", "seasonEnd": "2026-08-13 12:00:00"},
            {"seasonID": 41, "seasonName": "Season21", "seasonStart": "2026-08-13 12:00:00", "seasonEnd": "2026-11-12 09:00:00"},
            {"seasonID": 42, "seasonName": "Pre-Season22", "seasonStart": "2026-11-12 12:00:00", "seasonEnd": "2026-11-19 12:00:00"},
            {"seasonID": 43, "seasonName": "Season22", "seasonStart": "2026-11-19 12:00:00", "seasonEnd": "2027-02-18 09:00:00"},
        ])
        with patch("services.holidays_api._fetch_seasons", new=AsyncMock(return_value=rows)):
            for now, expected in (("2026-11-13 12:00", (41, 39)), ("2026-11-19 13:00", (41, 39)),
                                  ("2026-11-22 11:59", (41, 39)), ("2026-11-22 12:00", (43, 41))):
                with self._now(now):
                    self.assertEqual(await holidays_api.rank_season_ids(), expected, now)

    def test_rank_switch_time_for_other_start_days(self):
        at = lambda d: datetime.fromisoformat(d).replace(tzinfo=KST)
        self.assertEqual(holidays_api._rank_switch_at(at("2027-02-18 12:00")), at("2027-02-21 12:00"))
        self.assertEqual(holidays_api._rank_switch_at(at("2027-02-20 12:00")), at("2027-02-21 12:00"))
        # 일요일 12시나 그 뒤에 시작하면 다음 일요일에 넘어감
        self.assertEqual(holidays_api._rank_switch_at(at("2027-02-21 12:00")), at("2027-02-28 12:00"))

    async def test_rank_season_none_without_list(self):
        with self._now("2026-11-13 12:00"), \
                patch("services.holidays_api._fetch_seasons", new=AsyncMock(return_value=None)):
            self.assertIsNone(await holidays_api.rank_season_ids())

    def test_guide_text_names_preseason(self):
        from commands.ui.roster_views import build_rest_day_guide_view
        text = build_rest_day_guide_view("팀", "1", preseason=True).children[0].children[0].content
        self.assertIn("프리시즌 스크림 자율 진행 안내", text)
        self.assertNotIn("주말", text)


if __name__ == "__main__":
    unittest.main()
