"""[임시] 2026-08-16까지 모든 스크림 자율 상시 안내 회귀 테스트.

기간 종료(2026-08-16) 후 get_rest_day_info의 temp_force_rest 블록을 제거하면
이 테스트도 함께 삭제한다.
"""
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from utils import helpers


class TestTempAllRestUntilAug16(unittest.IsolatedAsyncioTestCase):
    async def test_forced_rest_within_period_regardless_of_weekday(self):
        # 공휴일 API를 빈 값으로 고정 → 순수하게 강제 로직만 검증
        with patch("services.holidays_api.get_holiday_names", new=AsyncMock(return_value=[])):
            # 평일(수)인데도 기간 내라 자율 강제
            r = await helpers.get_rest_day_info(date(2026, 8, 12))
            self.assertTrue(r["is_rest_day"])
            self.assertEqual(r["labels"], ["자율"])  # 요일/공휴일 없을 때 자율 라벨

            # 경계 8/16 포함
            self.assertTrue((await helpers.get_rest_day_info(date(2026, 8, 16)))["is_rest_day"])

    async def test_reverts_after_period(self):
        with patch("services.holidays_api.get_holiday_names", new=AsyncMock(return_value=[])):
            # 기간 종료 후(8/17~): 강제 없음 → 실제 요일 기준(공휴일 mock 없음)
            for d in (date(2026, 8, 17), date(2026, 8, 20), date(2026, 9, 1)):
                info = await helpers.get_rest_day_info(d)
                self.assertEqual(info["is_rest_day"], d.weekday() == 6, f"{d} 원복 실패")


if __name__ == "__main__":
    unittest.main()
