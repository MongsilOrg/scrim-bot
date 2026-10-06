import json
import unittest
from unittest import mock

from services import bser_api
from services.bser_api import BSERAPIClient


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


if __name__ == '__main__':
    unittest.main()
