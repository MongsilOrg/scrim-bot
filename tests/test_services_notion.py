import unittest
from datetime import datetime
from unittest import mock

import requests

from config import logging_config
from services import notion_api
from utils.helpers import KST


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f'{status} error', response=response)


class ServerInfoCacheTest(unittest.TestCase):
    def setUp(self):
        notion_api._server_info_cache = None
        notion_api._server_info_cached_at = 0.0
        notion_api._server_info_cached_for = None
        notion_api._server_info_failed_at = None
        logging_config._log_once_at.clear()
        self.clock = [1000.0]
        self.now = [datetime(2026, 10, 6, 12, 0, tzinfo=KST)]
        patches = [
            mock.patch.object(notion_api.time, 'monotonic', side_effect=lambda: self.clock[0]),
            mock.patch.object(notion_api, 'get_current_kst_time', side_effect=lambda: self.now[0]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _call(self, result):
        side = result if isinstance(result, Exception) else None
        value = None if side else result
        with mock.patch.object(notion_api, 'check_notion_for_tags', side_effect=side, return_value=value) as check:
            info = notion_api.get_server_info()
        return info, check.call_count

    def test_auth_error_with_cache_logs_error(self):
        self._call([True, True])
        self.clock[0] += 400
        with self.assertLogs('scrim-bot.notion_api', level='WARNING') as logs:
            info, _ = self._call(http_error(401))
        self.assertTrue(info['is_tournament'])
        self.assertEqual(logs.records[0].levelname, 'ERROR')

    def test_transient_error_with_cache_logs_warning(self):
        self._call([True, True])
        self.clock[0] += 400
        with self.assertLogs('scrim-bot.notion_api', level='WARNING') as logs:
            self._call(http_error(503))
        self.assertEqual(logs.records[0].levelname, 'WARNING')

    def test_cache_from_previous_day_is_not_used_on_failure(self):
        self._call([True, True])
        self.now[0] = datetime(2026, 10, 7, 12, 0, tzinfo=KST)
        self.clock[0] += 10
        with self.assertLogs('scrim-bot.notion_api', level='WARNING'):
            info, calls = self._call(http_error(503))
        self.assertEqual(calls, 1)
        self.assertFalse(info['is_tournament'])

    def test_failure_is_not_retried_right_away(self):
        with self.assertLogs('scrim-bot.notion_api', level='WARNING'):
            self._call(requests.ConnectionError('down'))
        self.clock[0] += 10
        _, calls = self._call([True, True])
        self.assertEqual(calls, 0)
        self.clock[0] += notion_api._SERVER_INFO_RETRY_SECONDS
        info, calls = self._call([True, True])
        self.assertEqual(calls, 1)
        self.assertTrue(info['is_tournament'])


if __name__ == '__main__':
    unittest.main()
