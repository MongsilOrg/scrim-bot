import unittest
from unittest.mock import AsyncMock, patch

from models.team_processor import TeamProcessor


def _bare_processor():
    tp = TeamProcessor.__new__(TeamProcessor)
    tp._set_test_accounts({})
    return tp


class TestEnsureTestAccountsLoaded(unittest.IsolatedAsyncioTestCase):
    async def test_reads_copy_every_time(self):
        tp = _bare_processor()
        self.assertFalse(tp.is_test_account("신규테스트"))

        with patch("models.team_processor.labs_ops.tests", return_value={"신규테스트": 6000.5}):
            self.assertTrue(await tp.ensure_test_accounts_loaded())
        self.assertTrue(tp.is_test_account("신규테스트"))
        self.assertEqual(tp._get_test_account_mmr("신규테스트"), 6000.5)

        with patch("models.team_processor.labs_ops.tests", return_value={}):
            await tp.ensure_test_accounts_loaded()
        self.assertFalse(tp.is_test_account("신규테스트"))

    async def test_missing_copy_keeps_previous_accounts(self):
        tp = _bare_processor()
        tp._set_test_accounts({"TEST1": 6000.0})

        with patch("models.team_processor.labs_ops.tests", return_value=None):
            self.assertFalse(await tp.ensure_test_accounts_loaded())

        self.assertEqual(tp.test_accounts_data, {"TEST1": 6000.0})
        self.assertTrue(tp.is_test_account("test1"))


class TestFetchAllTeamMmrReloadsTestAccounts(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_all_triggers_reload(self):
        tp = _bare_processor()
        tp.ensure_test_accounts_loaded = AsyncMock()
        tp.fetch_team_mmr = AsyncMock(return_value=("팀A", object(), 1500.0))

        await tp._fetch_all_team_mmr({"팀A": object()})

        tp.ensure_test_accounts_loaded.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
