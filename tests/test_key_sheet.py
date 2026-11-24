import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services import key_sheet

K1 = 'AAAAA-BBBBB-00001'
K2 = 'AAAAA-BBBBB-00002'
K3 = 'AAAAA-BBBBB-00003'


def cells(*values):
    return {'values': [{'formattedValue': v} if v else {} for v in values]}


def grid(rows, merges=None, title='시트1'):
    return {'properties': {'title': title, 'index': 0}, 'merges': merges or [], 'data': [{'rowData': rows}]}


def sheet():
    return grid([
        {},
        cells('', '토너먼트 키', '사용자'),
        cells('', K1, ' 가 ', '메모'),
        cells('', '2026/01/01 추가'),
        cells('', f' {K2} ', ''),
        cells('', K3),
    ], merges=[{'startRowIndex': 2, 'endRowIndex': 5, 'startColumnIndex': 3, 'endColumnIndex': 5}])


def reply(status=200, data=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data or {}
    return resp


def ranges(key, name):
    return {'valueRanges': [{'values': [[key]]} if key else {}, {'values': [[name]]} if name else {}]}


class ParseCase(unittest.TestCase):
    def test_헤더_아래의_키_행만_고르고_합친_메모를_행마다_적용(self):
        layout = key_sheet.parse(sheet())
        self.assertEqual((layout['title'], layout['key_col'], layout['name_col']), ('시트1', 1, 2))
        self.assertEqual(layout['rows'], [
            {'pos': 3, 'key': K1, 'name': ' 가 ', 'note': '메모', 'batch': '', 'color': ''},
            {'pos': 5, 'key': K2, 'name': '', 'note': '메모', 'batch': '2026/01/01 추가', 'color': ''},
            {'pos': 6, 'key': K3, 'name': '', 'note': '', 'batch': '2026/01/01 추가', 'color': ''},
        ])

    def test_키_칸에_칠한_색은_흰색이_아닐_때만_전달(self):
        def painted(back):
            return {'values': [{}, {'formattedValue': K1, 'effectiveFormat': {'backgroundColor': back}}, {}]}
        rows = key_sheet.parse(grid([cells('', '토너먼트 키', '사용자'), painted({'red': 1, 'green': 1, 'blue': 1}), painted({'green': 1}), painted({'red': 0.85, 'green': 0.9176, 'blue': 0.827})]))['rows']
        self.assertEqual([r['color'] for r in rows], ['', '#00ff00', '#d9ead3'])

    def test_헤더가_없으면_None(self):
        self.assertIsNone(key_sheet.parse(grid([cells('', K1, '가')])))
        self.assertIsNone(key_sheet.parse({}))

    def test_칸_주소는_탭_이름의_따옴표를_겹쳐_씀(self):
        self.assertEqual(key_sheet._a1({'title': "키's"}, 2, 14), "'키''s'!C14")
        self.assertEqual(key_sheet._a1({'title': 't'}, 27, 1), "'t'!AB1")


class SetCase(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        patcher = patch.object(key_sheet, '_open', return_value=self.session)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.layout = key_sheet.parse(sheet())
        self.row = self.layout['rows'][1]

    def test_키와_칸이_읽은_그대로일_때만_RAW로_씀(self):
        self.session.get.return_value = reply(data=ranges(K2, ''))
        self.session.put.return_value = reply()
        self.assertTrue(key_sheet._set(self.layout, self.row, '=새 사람'))
        self.assertEqual(self.session.get.call_args.kwargs['params'], {'ranges': ["'시트1'!B5", "'시트1'!C5"]})
        url = self.session.put.call_args.args[0]
        self.assertTrue(url.endswith('/values/%27%EC%8B%9C%ED%8A%B81%27%21C5'))
        self.assertEqual(self.session.put.call_args.kwargs['params'], {'valueInputOption': 'RAW'})
        self.assertEqual(self.session.put.call_args.kwargs['json'], {'values': [['=새 사람']]})

    def test_행이_밀렸거나_칸이_바뀌었거나_응답이_실패면_쓰지_않음(self):
        for data in (ranges(K3, ''), ranges(K2, '다른 사람'), ranges('', ''), {}):
            self.session.get.return_value = reply(data=data)
            self.assertFalse(key_sheet._set(self.layout, self.row, '새 사람'))
        self.session.get.return_value = reply(status=403)
        self.assertFalse(key_sheet._set(self.layout, self.row, '새 사람'))
        self.session.put.assert_not_called()
        self.session.get.return_value = reply(data=ranges(K2, ''))
        self.session.put.return_value = reply(status=500)
        self.assertFalse(key_sheet._set(self.layout, self.row, '새 사람'))


class ApplyCase(unittest.TestCase):
    def setUp(self):
        self.layout = key_sheet.parse(sheet())
        patcher = patch.object(key_sheet, '_set', return_value=True)
        self.set = patcher.start()
        self.addCleanup(patcher.stop)

    def changed(self):
        return [(c.args[1]['pos'], c.args[2]) for c in self.set.call_args_list]

    def test_이름은_키가_맞는_빈_칸에만_적음(self):
        done = key_sheet.apply(self.layout, {'write': [
            {'pos': 5, 'key': K2, 'name': '나'},
            {'pos': 3, 'key': K1, 'name': '덮어쓰기'},
            {'pos': 6, 'key': K2, 'name': '다른 행'},
            {'pos': 6, 'key': K3, 'name': ' '},
            {'pos': 99, 'key': K3, 'name': '없는 행'},
        ]})
        self.assertEqual((done, self.changed()), (1, [(5, '나')]))
        self.assertEqual(self.layout['rows'][1]['name'], '나')

    def test_비우기는_키가_맞고_이름이_있는_칸에만(self):
        done = key_sheet.apply(self.layout, {'wipe': [{'pos': 3, 'key': K1}, {'pos': 3, 'key': K1}, {'pos': 5, 'key': K2}, {'pos': 6, 'key': K1}]})
        self.assertEqual((done, self.changed()), (1, [(3, '')]))
        self.assertEqual(self.layout['rows'][0]['name'], '')

    def test_쓰기가_실패한_칸은_읽은_값을_그대로_둠(self):
        self.set.return_value = False
        self.assertEqual(key_sheet.apply(self.layout, {'write': [{'pos': 5, 'key': K2, 'name': '나'}], 'wipe': [{'pos': 3, 'key': K1}]}), 0)
        self.assertEqual([r['name'] for r in self.layout['rows']], [' 가 ', '', ''])

    def test_비울_칸이_상한을_넘으면_시트를_바꾸지_않음(self):
        rows = [{'pos': i, 'key': f'AAAAA-BBBBB-{i:05d}', 'name': '가', 'note': ''} for i in range(3, 33)]
        rows.append({'pos': 40, 'key': K1, 'name': '', 'note': ''})
        layout = {**self.layout, 'rows': rows}
        wipes = [{'pos': r['pos'], 'key': r['key']} for r in rows]
        write = [{'pos': 40, 'key': K1, 'name': '나'}]
        self.assertEqual(key_sheet.apply(layout, {'write': write, 'wipe': wipes}), 0)
        self.set.assert_not_called()
        # 이미 빈 칸과 키가 다른 칸은 상한에 세지 않음
        some = wipes[:key_sheet.WIPE_MAX] + [{'pos': 40, 'key': K1}, {'pos': 3, 'key': K2}, {'pos': 99, 'key': K3}]
        self.assertEqual(key_sheet.apply(layout, {'write': write, 'wipe': some}), key_sheet.WIPE_MAX + 1)


class SyncCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.layout = key_sheet.parse(sheet())
        self.request = AsyncMock()
        for target, value in (('_read', MagicMock(return_value=self.layout)), ('_set', MagicMock(return_value=True))):
            patcher = patch.object(key_sheet, target, value)
            setattr(self, target.strip('_'), patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch.object(key_sheet.labs_api, 'request', self.request)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_할_일이_없으면_시트를_읽지_않음(self):
        self.request.return_value = {'pending': 0}
        self.assertFalse(await key_sheet.sync())
        self.request.return_value = None
        self.assertFalse(await key_sheet.sync(force=True))
        self.read.assert_not_called()

    async def test_할_일이_있으면_시트를_보내고_바꾼_칸을_다시_보냄(self):
        ok = {'status': 'ok', 'keys': 3, 'added': 0, 'changed': 0}
        self.request.side_effect = [{'pending': 2}, {**ok, 'write': [{'pos': 5, 'key': K2, 'name': '나'}], 'wipe': [{'pos': 3, 'key': K1}]}, {**ok, 'write': [], 'wipe': []}]
        self.assertTrue(await key_sheet.sync())
        self.assertEqual([c.args[:2] for c in self.request.call_args_list], [('GET', '/api/bot/scrim-keys')] + [('PUT', '/api/bot/scrim-keys')] * 2)
        self.assertEqual([r['name'] for r in self.request.call_args_list[2].args[2]['rows']], ['', '나', ''])

    async def test_바꾼_칸이_없으면_한_번만_보냄(self):
        self.request.side_effect = [{'pending': 0}, {'status': 'ok', 'keys': 3, 'added': 3, 'changed': 0, 'write': [], 'wipe': []}]
        self.assertTrue(await key_sheet.sync(force=True))
        self.assertEqual(self.request.call_count, 2)
        self.set.assert_not_called()

    async def test_시트를_읽지_못하면_Labs에_보내지_않음(self):
        self.read.return_value = None
        self.request.return_value = {'pending': 1}
        self.assertFalse(await key_sheet.sync())
        self.assertEqual(self.request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
