import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from models.team_processor import TeamProcessor


def _bare_processor():
    tp = TeamProcessor.__new__(TeamProcessor)
    tp._set_test_accounts({})
    tp._test_accounts_loaded_at = 0.0
    tp._test_accounts_attempted_at = 0.0
    return tp


class TestEnsureTestAccountsLoaded(unittest.IsolatedAsyncioTestCase):
    async def test_reloads_when_stale_and_recognizes_new_account(self):
        tp = _bare_processor()
        self.assertFalse(tp.is_test_account("신규테스트"))

        def fake_load():
            tp._set_test_accounts({"신규테스트": 6000.0})
            return True

        with patch.object(tp, "_load_test_accounts_data_sync", side_effect=fake_load) as loader:
            await tp.ensure_test_accounts_loaded()

        loader.assert_called_once()
        self.assertTrue(tp.is_test_account("신규테스트"))
        self.assertEqual(tp._get_test_account_mmr("신규테스트"), 6000.0)

    async def test_ttl_cache_skips_reload_when_fresh(self):
        tp = _bare_processor()
        with patch.object(tp, "_load_test_accounts_data_sync") as loader:
            await tp.ensure_test_accounts_loaded()
            await tp.ensure_test_accounts_loaded()
        loader.assert_called_once()

    async def test_force_reloads_even_when_fresh(self):
        tp = _bare_processor()
        with patch.object(tp, "_load_test_accounts_data_sync") as loader:
            await tp.ensure_test_accounts_loaded()
            await tp.ensure_test_accounts_loaded(force=True)
        self.assertEqual(loader.call_count, 2)


class TestLoadFailureKeepsCache(unittest.IsolatedAsyncioTestCase):
    def test_sheet_error_keeps_previous_accounts(self):
        tp = _bare_processor()
        tp._set_test_accounts({"TEST1": 6000.0})
        tp.gspread_spreadsheet = MagicMock()
        tp.gspread_spreadsheet.worksheet.side_effect = RuntimeError("503")

        self.assertFalse(tp._load_test_accounts_data_sync())
        self.assertEqual(tp.test_accounts_data, {"TEST1": 6000.0})
        self.assertTrue(tp.is_test_account("test1"))

    async def test_failure_does_not_mark_cache_fresh(self):
        tp = _bare_processor()
        with patch.object(tp, "_load_test_accounts_data_sync", return_value=False):
            self.assertFalse(await tp.ensure_test_accounts_loaded())
        self.assertEqual(tp._test_accounts_loaded_at, 0.0)

    async def test_failure_cooldown_blocks_immediate_retry(self):
        tp = _bare_processor()
        with patch.object(tp, "_load_test_accounts_data_sync", return_value=False) as loader:
            await tp.ensure_test_accounts_loaded()
            await tp.ensure_test_accounts_loaded()
        loader.assert_called_once()


class TestFetchAllTeamMmrReloadsTestAccounts(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_all_triggers_reload(self):
        tp = _bare_processor()
        tp.ensure_test_accounts_loaded = AsyncMock()
        tp.fetch_team_mmr = AsyncMock(return_value=("팀A", object(), 1500.0))

        await tp._fetch_all_team_mmr({"팀A": object()})

        tp.ensure_test_accounts_loaded.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
