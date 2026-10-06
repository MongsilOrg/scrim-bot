import json
import unittest
from unittest import mock

from services import bser_api
from services.bser_api import UID_ERROR, UID_FOUND, UID_NOT_FOUND, BSERAPIClient


class FakeResponse:
    def __init__(self, status, body=None, *, raw=None, headers=None):
        self.status = status
        self.body = body
        self.raw = raw
        self.headers = headers or {}

    async def json(self, content_type=None):
        if self.raw is not None:
            return json.loads(self.raw)
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    closed = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def request(self, method, url, params=None, timeout=None):
        self.urls.append(url)
        return self.responses.pop(0)

    async def close(self):
        self.closed = True


def make_client(responses):
    client = BSERAPIClient()
    client.session = FakeSession(responses)
    return client


class BSERTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        BSERAPIClient._nickname_cache.clear()
        BSERAPIClient._mmr_cache.clear()
        patcher = mock.patch.object(bser_api.asyncio, 'sleep', new=mock.AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)


class RequestStatusTest(BSERTestCase):
    async def test_html_5xx_is_retried(self):
        client = make_client([
            FakeResponse(502, raw='<html>Bad Gateway</html>'),
            FakeResponse(200, {'code': 200, 'ok': True}),
        ])
        data = await client._request('GET', 'u')
        self.assertEqual(data, {'code': 200, 'ok': True})
        self.assertEqual(len(client.session.urls), 2)

    async def test_5xx_exhausted_returns_none(self):
        responses = [FakeResponse(503, raw='<html></html>') for _ in range(BSERAPIClient.MAX_RETRIES + 1)]
        client = make_client(responses)
        self.assertIsNone(await client._request('GET', 'u'))
        self.assertEqual(client.session.responses, [])

    async def test_non_json_body_returns_none(self):
        client = make_client([FakeResponse(200, raw='<html></html>')])
        self.assertIsNone(await client._request('GET', 'u'))

    async def test_4xx_body_is_returned_for_code_check(self):
        client = make_client([FakeResponse(403, {'code': 403, 'message': 'User Mismatch'})])
        self.assertEqual((await client._request('GET', 'u'))['code'], 403)

    async def test_429_retry_logs_count_at_info(self):
        client = make_client([
            FakeResponse(429, raw='', headers={'Retry-After': '1'}),
            FakeResponse(200, {'code': 200}),
        ])
        with self.assertLogs('scrim-bot.bser_api', level='DEBUG') as logs:
            await client._request('GET', 'u')
        self.assertEqual(logs.records[-1].levelname, 'INFO')
        self.assertIn('429 재시도 1회', logs.records[-1].getMessage())
        self.assertFalse([r for r in logs.records if r.levelname == 'WARNING'])


class MaintenanceCheckTest(BSERTestCase):
    async def test_uses_fixed_season(self):
        client = make_client([
            FakeResponse(200, {'code': 200, 'topRanks': [{'nickname': '백수'}]}),
            FakeResponse(200, {'code': 200, 'user': {'userId': 'u1'}}),
        ])
        self.assertFalse(await client.check_server_maintenance())
        self.assertIn(f'/rank/top/{BSERAPIClient.RANK_SEASON_ID}/', client.session.urls[0])
        self.assertFalse(any('Season' in url for url in client.session.urls))

    async def test_empty_ranking_is_maintenance(self):
        client = make_client([FakeResponse(200, {'code': 200, 'topRanks': []})])
        self.assertTrue(await client.check_server_maintenance())

    async def test_ranking_failure_is_maintenance(self):
        client = make_client([FakeResponse(404, {'code': 404, 'message': 'Not Found'})])
        self.assertTrue(await client.check_server_maintenance())

    async def test_top_nickname_404_is_maintenance(self):
        client = make_client([
            FakeResponse(200, {'code': 200, 'topRanks': [{'nickname': '백수'}]}),
            FakeResponse(200, {'code': 404, 'message': 'Not Found'}),
        ])
        self.assertTrue(await client.check_server_maintenance())


class UidLookupTest(BSERTestCase):
    async def test_not_found_and_error_are_distinct(self):
        client = make_client([FakeResponse(200, {'code': 404, 'message': 'Not Found'})])
        self.assertEqual(await client.lookup_user_uid('없는닉'), (UID_NOT_FOUND, None))

        responses = [FakeResponse(429, raw='') for _ in range(BSERAPIClient.MAX_RETRIES + 1)]
        client = make_client(responses)
        self.assertEqual(await client.lookup_user_uid('혼잡'), (UID_ERROR, None))

    async def test_get_user_uid_keeps_old_contract(self):
        client = make_client([
            FakeResponse(200, {'code': 200, 'user': {'userId': 'uid-1'}}),
            FakeResponse(200, {'code': 404}),
        ])
        self.assertEqual(await client.get_user_uid('있는닉'), 'uid-1')
        self.assertIsNone(await client.get_user_uid('없는닉2'))
        self.assertEqual(await client.lookup_user_uid('있는닉'), (UID_FOUND, 'uid-1'))

    async def test_mmr_uses_fixed_season(self):
        client = make_client([FakeResponse(200, {'code': 200, 'userRank': {'mmr': 7000}})])
        self.assertEqual(await client.get_user_mmr('uid-s'), 7000)
        self.assertIn(f'/rank/uid/uid-s/{BSERAPIClient.RANK_SEASON_ID}/3', client.session.urls[0])



if __name__ == '__main__':
    unittest.main()
