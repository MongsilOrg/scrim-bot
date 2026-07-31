"""테스트 계정 캐시 갱신 회귀 테스트.

버그: TeamProcessor.test_accounts_data 가 __init__ 에서 한 번만 로드되고
이후 갱신되지 않음. 봇 실행 중 '테스트' 시트에 계정을 추가하면 _is_test_account
가 인식하지 못해 실계정 API 경로로 새고, 조회 실패 → 해당 멤버가 MMR 평균에서
탈락 → 팀 MMR 이 2인/1인 평균으로 잘못 계산됨.

수정: MMR 계산 직전 test_accounts_data 를 (TTL 캐시로) 재로드하고,
_fetch_all_team_mmr 가 그 재로드를 트리거하도록 함.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from models.team_processor import TeamProcessor


def _bare_processor():
    """__init__(네트워크/시트) 우회한 최소 인스턴스."""
    tp = TeamProcessor.__new__(TeamProcessor)
    tp.test_accounts_data = {}
    tp._test_accounts_loaded_at = 0.0
    tp._test_accounts_attempted_at = 0.0
    return tp


class TestEnsureTestAccountsLoaded(unittest.IsolatedAsyncioTestCase):
    async def test_reloads_when_stale_and_recognizes_new_account(self):
        """캐시가 스테일(loaded_at=0)이면 시트를 다시 읽어 새 테스트 계정을 인식해야 함."""
        tp = _bare_processor()
        self.assertFalse(tp._is_test_account("신규테스트"))  # 아직 미인식

        def fake_load():
            tp.test_accounts_data = {"신규테스트": 6000.0}
            return True

        with patch.object(tp, "_load_test_accounts_data_sync", side_effect=fake_load) as loader:
            await tp.ensure_test_accounts_loaded()

        loader.assert_called_once()
        self.assertTrue(tp._is_test_account("신규테스트"))
        self.assertEqual(tp._get_test_account_mmr("신규테스트"), 6000.0)

    async def test_ttl_cache_skips_reload_when_fresh(self):
        """TTL 이내면 시트를 다시 읽지 않아야 함(불필요한 네트워크 방지)."""
        tp = _bare_processor()
        with patch.object(tp, "_load_test_accounts_data_sync") as loader:
            await tp.ensure_test_accounts_loaded()   # 최초 로드 → loaded_at 갱신
            await tp.ensure_test_accounts_loaded()    # 곧바로 재호출 → TTL 이내라 스킵
        loader.assert_called_once()

    async def test_force_reloads_even_when_fresh(self):
        """force=True 면 TTL 무시하고 재로드."""
        tp = _bare_processor()
        with patch.object(tp, "_load_test_accounts_data_sync") as loader:
            await tp.ensure_test_accounts_loaded()
            await tp.ensure_test_accounts_loaded(force=True)
        self.assertEqual(loader.call_count, 2)


class TestLoadFailureKeepsCache(unittest.IsolatedAsyncioTestCase):
    """시트 로드 실패가 등록된 테스트 계정을 지우면 안 됨.

    버그: _load_test_accounts_data_sync 가 예외를 삼키고 test_accounts_data 를
    {} 로 덮어써서, 구글 시트 503 한 번에 등록된 테스트 계정 전체가 미등록으로
    취급되고 신청이 '닉네임을 찾을 수 없음'으로 반려됐다.
    """

    def test_sheet_error_keeps_previous_accounts(self):
        tp = _bare_processor()
        tp.test_accounts_data = {"TEST1": 6000.0}
        tp.gspread_spreadsheet = MagicMock()
        tp.gspread_spreadsheet.worksheet.side_effect = RuntimeError("503")

        self.assertFalse(tp._load_test_accounts_data_sync())
        self.assertEqual(tp.test_accounts_data, {"TEST1": 6000.0})

    async def test_failure_does_not_mark_cache_fresh(self):
        """실패 시 loaded_at 을 갱신하면 안 됨(쿨다운 후 재시도 가능해야 함)."""
        tp = _bare_processor()
        with patch.object(tp, "_load_test_accounts_data_sync", return_value=False):
            self.assertFalse(await tp.ensure_test_accounts_loaded())
        self.assertEqual(tp._test_accounts_loaded_at, 0.0)

    async def test_failure_cooldown_blocks_immediate_retry(self):
        """직전 시도가 실패했으면 쿨다운 동안 시트를 다시 때리지 않아야 함."""
        tp = _bare_processor()
        with patch.object(tp, "_load_test_accounts_data_sync", return_value=False) as loader:
            await tp.ensure_test_accounts_loaded()
            await tp.ensure_test_accounts_loaded()
        loader.assert_called_once()


class TestFetchAllTeamMmrReloadsTestAccounts(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_all_triggers_reload(self):
        """_fetch_all_team_mmr 가 팀 MMR 조회 전에 테스트계정을 재로드해야 함."""
        tp = _bare_processor()
        tp.ensure_test_accounts_loaded = AsyncMock()
        tp.fetch_team_mmr = AsyncMock(return_value=("팀A", object(), 1500.0))

        await tp._fetch_all_team_mmr({"팀A": object()})

        tp.ensure_test_accounts_loaded.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
