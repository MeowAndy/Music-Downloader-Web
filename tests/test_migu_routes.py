"""Offline Migu routes using a fake adapter, without importing credentials."""
import concurrent.futures
import importlib.util
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class MiguRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        adapter = types.ModuleType('migu_api')
        class ApiError(RuntimeError):
            def __init__(self, code='UPSTREAM_ERROR', status=502):
                self.code, self.status = code, status
                super().__init__('咪咕请求失败')
        def validate_id(value):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]{1,32}', value):
                raise ApiError('INVALID_INPUT', 400)
            return value
        adapter.ApiError = ApiError
        adapter.validate_id = validate_id
        for name in ('search', 'download', 'status', 'lyric_text'):
            setattr(adapter, name, mock.Mock())
        cls.adapter = adapter
        lyric_spec = importlib.util.spec_from_file_location('migu_route_lyrics_fixture', ROOT / 'migu_lyrics.py')
        cls.lyrics = importlib.util.module_from_spec(lyric_spec)
        spec = importlib.util.spec_from_file_location('migu_route_fixture', ROOT / 'app.py')
        cls.module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {'qq_api': types.ModuleType('qq_api'),
                                          'qq_login': types.ModuleType('qq_login'),
                                          'migu_api': adapter}):
            lyric_spec.loader.exec_module(cls.lyrics)
            with mock.patch.dict(sys.modules, {'migu_lyrics': cls.lyrics}):
                spec.loader.exec_module(cls.module)

    def setUp(self):
        self.client = self.module.app.test_client()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name) / 'output'
        output_patch = mock.patch.object(self.module, 'OUTPUT_DIR', self.output)
        output_patch.start()
        self.addCleanup(output_patch.stop)
        self.lyric_data = {
            'source': 'migu', 'id': '001Ab', 'format': 'txt', 'has_lyrics': True,
            'content': '完整歌词\n', 'cached': True, 'warnings': [], 'truncated': False,
        }
        for name in ('search', 'download', 'status', 'lyric_text'):
            getattr(self.adapter, name).reset_mock(side_effect=True, return_value=True)

    def test_status_preserves_honest_unavailable_capabilities(self):
        data = {'capabilities': {'search': False, 'download': False, 'lyrics': False, 'login': False},
                'message': '当前咪咕接口不可用'}
        self.adapter.status.return_value = data
        response = self.client.get('/api/migu/status')
        self.assertEqual(200, response.status_code)
        self.assertEqual({'ok': True, **data}, response.json)
        self.adapter.status.assert_called_once_with()

    def test_status_exposes_download_without_claiming_login(self):
        data = {'capabilities': {'search': True, 'download': True, 'lyrics': True, 'login': False},
                'message': '官方下载接口已接通，逐首核验权限'}
        self.adapter.status.return_value = data
        response = self.client.get('/api/migu/status')
        self.assertEqual(200, response.status_code)
        self.assertEqual({'ok': True, **data}, response.json)

    def test_download_transaction_errors_preserved_without_lyric_attempt(self):
        with mock.patch.object(self.lyrics, 'render') as render:
            for code, status in (('PRICE_LOOKUP_FAILED', 503), ('MEMBERSHIP_REQUIRED', 403)):
                self.adapter.download.side_effect = self.adapter.ApiError(code, status)
                response = self.client.get('/api/migu/download?id=69905307315&lyrics=1')
                self.assertEqual(status, response.status_code)
                self.assertFalse(response.json['ok'])
                self.assertEqual(code, response.json['code'])
            render.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_search_keyword_limit_and_real_ids_pass_through(self):
        self.adapter.search.return_value = [{'id': '001Ab', 'name': '示例'}]
        response = self.client.get('/api/migu/search', query_string={'kw': ' 示例 '})
        self.assertEqual(200, response.status_code)
        self.assertEqual('001Ab', response.json['songs'][0]['id'])
        self.adapter.search.assert_called_once_with('示例', num=30)
        for value in ('1', '100'):
            self.client.get('/api/migu/search', query_string={'kw': 'x', 'num': value})
            self.adapter.search.assert_called_with('x', num=int(value))

    def test_invalid_search_input_never_calls_adapter(self):
        for value in ('0', '101', '-1', 'NaN', '', '١٢', '1.0', '9' * 5000):
            response = self.client.get('/api/migu/search', query_string={'kw': 'x', 'num': value})
            self.assertEqual(400, response.status_code)
            self.assertEqual('INVALID_INPUT', response.json['code'])
        for keyword in ('', '   '):
            self.assertEqual(400, self.client.get('/api/migu/search', query_string={'kw': keyword}).status_code)
        self.adapter.search.assert_not_called()

    def test_download_two_argument_contract_and_identity(self):
        self.adapter.download.return_value = {'name': 'song.mp3', 'size': 20}
        for sid in ('001Ab', '0001', '0'):
            response = self.client.get('/api/migu/download', query_string={'id': sid})
            self.assertEqual(200, response.status_code)
            self.assertTrue(response.json['ok'])
            self.adapter.download.assert_called_with(sid, 'standard')
        self.client.get('/api/migu/download?id=001Ab&quality=hq&lyrics_format=invalid')
        self.adapter.download.assert_called_with('001Ab', 'hq')
        self.adapter.lyric_text.assert_not_called()

    def test_api_errors_preserve_controlled_code_status(self):
        self.adapter.download.side_effect = self.adapter.ApiError('PERMISSION_DENIED', 403)
        response = self.client.get('/api/migu/download?id=001Ab')
        self.assertEqual(403, response.status_code)
        self.assertEqual('PERMISSION_DENIED', response.json['code'])

    def test_all_routes_guarded_before_adapter_or_lyrics(self):
        with mock.patch.object(self.lyrics, 'render') as render:
            for route in ('status', 'search?kw=x', 'download?id=001Ab',
                          'lyrics?id=001Ab', 'lyrics/download?id=001Ab'):
                for kwargs in ({'headers': {'Origin': 'https://other.example'}},
                               {'headers': {'Sec-Fetch-Site': 'cross-site'}},
                               {'base_url': 'http://evil.example'}):
                    with self.subTest(route=route, kwargs=kwargs):
                        response = self.client.get('/api/migu/' + route, **kwargs)
                        self.assertEqual(403, response.status_code)
                        self.assertNotIn('网易云', response.json['error'])
            render.assert_not_called()
        for name in ('search', 'download', 'status', 'lyric_text'):
            getattr(self.adapter, name).assert_not_called()

    def test_lyrics_preview_default_options_and_same_origin(self):
        with mock.patch.object(self.lyrics, 'render', return_value=self.lyric_data) as render:
            response = self.client.get('/api/migu/lyrics?id=001Ab', headers={'Origin': 'http://localhost'})
            self.assertEqual(200, response.status_code)
            self.assertEqual('migu', response.json['source'])
            self.assertEqual('001Ab', response.json['id'])
            self.assertNotIn('mid', response.json)
            render.assert_called_once_with('001Ab', fmt='txt', include_translation=False,
                                           include_romanization=False, preview=True)
            render.reset_mock()
            self.client.get('/api/migu/lyrics?id=001Ab&format=LRC&translation=1&romanization=1&preview=0')
            render.assert_called_once_with('001Ab', fmt='lrc', include_translation=True,
                                           include_romanization=True, preview=False)

    def test_invalid_ids_and_lyric_formats_do_not_reach_adapter_or_write(self):
        with mock.patch.object(self.lyrics, 'render') as render:
            for route in ('lyrics', 'lyrics/download', 'download'):
                for sid in ('', '-1', '../x', ' a', 'a\n', 'a,b', 'a' * 33, '１２３'):
                    with self.subTest(route=route, sid=sid):
                        response = self.client.get('/api/migu/' + route, query_string={'id': sid})
                        self.assertEqual(400, response.status_code)
                        self.assertEqual('INVALID_INPUT', response.json['code'])
            for route in ('lyrics', 'lyrics/download'):
                self.assertEqual(400, self.client.get('/api/migu/' + route,
                    query_string={'id': '001Ab', 'format': 'json'}).status_code)
            self.assertEqual(400, self.client.get('/api/migu/download', query_string={
                'id': '001Ab', 'lyrics': '1', 'lyrics_format': 'json'}).status_code)
        render.assert_not_called()
        self.adapter.download.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_full_lyric_download_safe_unique_filenames_and_metadata(self):
        with mock.patch.object(self.lyrics, 'render', return_value=self.lyric_data) as render:
            first = self.client.get('/api/migu/lyrics/download', query_string={
                'id': '001Ab', 'name': '../CON', 'singer': 'x\\y', 'preview': '1',
                'format': 'lrc', 'translation': '1', 'romanization': '1'})
            self.assertEqual(200, first.status_code)
            render.assert_called_once_with('001Ab', fmt='lrc', include_translation=True,
                                           include_romanization=True, preview=False)
            second = self.client.get('/api/migu/lyrics/download', query_string={
                'id': '001Ab', 'name': '../CON', 'singer': 'x\\y', 'format': 'lrc'})
        self.assertEqual(200, second.status_code)
        self.assertNotEqual(first.json['name'], second.json['name'])
        for data in (first.json, second.json):
            self.assertNotIn('/', data['name'])
            self.assertNotIn('\\', data['name'])
            path = self.output / data['name']
            self.assertEqual(self.output.resolve(), path.resolve().parent)
            self.assertEqual('完整歌词\n', path.read_text(encoding='utf-8'))
            self.assertEqual(len('完整歌词\n'.encode('utf-8')), data['size'])
            self.assertEqual('lrc', data['format'])
            self.assertTrue(data['cached'])

    def test_reserved_and_blank_names(self):
        with mock.patch.object(self.lyrics, 'render', return_value=self.lyric_data):
            for name, expected in (('CON', '_CON.txt'), ('...', 'Migu.txt'), ('COM1.txt', '_COM1.txt.txt')):
                response = self.client.get('/api/migu/lyrics/download', query_string={'id': '001Ab', 'name': name})
                self.assertEqual(expected, response.json['name'])

    def test_missing_lyrics_returns_404_without_writing(self):
        with mock.patch.object(self.lyrics, 'render', return_value={'has_lyrics': False}):
            response = self.client.get('/api/migu/lyrics/download?id=001Ab')
        self.assertEqual(404, response.status_code)
        self.assertEqual('LYRIC_NOT_FOUND', response.json['code'])
        self.assertFalse(self.output.exists())

    def test_errors_sanitized_and_no_provider_crossover(self):
        for route in ('lyrics?id=001Ab', 'lyrics/download?id=001Ab'):
            for exc, code in ((RuntimeError('Cookie=SECRET'), 'UPSTREAM_ERROR'),
                              (self.module.lyrics_api.LyricError('Cookie=SECRET'), 'LYRIC_ERROR'),
                              (self.adapter.ApiError('NETWORK_ERROR'), 'NETWORK_ERROR')):
                with self.subTest(route=route, code=code), \
                        mock.patch.object(self.lyrics, 'render', side_effect=exc), \
                        mock.patch.object(self.module, 'netease_error') as netease_error:
                    response = self.client.get('/api/migu/' + route)
                    self.assertEqual(502, response.status_code)
                    self.assertEqual(code, response.json['code'])
                    self.assertNotIn('SECRET', response.get_data(as_text=True))
                    netease_error.assert_not_called()
        self.adapter.search.side_effect = RuntimeError('token=SECRET')
        self.assertNotIn('SECRET', self.client.get('/api/migu/search?kw=x').get_data(as_text=True))
        self.adapter.status.side_effect = RuntimeError('token=SECRET')
        self.assertEqual(502, self.client.get('/api/migu/status').status_code)
        self.assertFalse(self.output.exists())

    def test_generic_atomic_saver_preserves_concurrent_existing_files(self):
        self.output.mkdir()
        (self.output / 'same.txt').write_text('original', encoding='utf-8')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.module._save_lyric(str(i), 'same', 'txt'), range(20)))
        self.assertEqual(20, len({item['name'] for item in results}))
        self.assertEqual('original', (self.output / 'same.txt').read_text(encoding='utf-8'))
        self.assertEqual({str(i) for i in range(20)}, {
            (self.output / item['name']).read_text(encoding='utf-8') for item in results})
        self.assertEqual(21, len(list(self.output.iterdir())))

    def test_sidecar_uses_final_audio_name_and_never_overwrites(self):
        self.output.mkdir()
        audio = self.output / 'Final Artist (1).mp3'
        old = self.output / 'Final Artist (1).lrc'
        old.write_text('old sidecar', encoding='utf-8')
        events = []
        def download(*args):
            events.append('audio')
            audio.write_bytes(b'complete audio')
            return {'name': audio.name, 'size': audio.stat().st_size}
        def render(*args, **kwargs):
            self.assertTrue(audio.is_file())
            events.append('lyrics')
            return self.lyric_data
        self.adapter.download.side_effect = download
        with mock.patch.object(self.lyrics, 'render', side_effect=render) as lyric:
            response = self.client.get('/api/migu/download', query_string={
                'id': '001Ab', 'quality': 'hq', 'lyrics': '1', 'lyrics_format': 'lrc',
                'translation': '1', 'romanization': '1', 'name': 'wrong title'})
        self.assertEqual(200, response.status_code)
        self.assertEqual(['audio', 'lyrics'], events)
        self.adapter.download.assert_called_once_with('001Ab', 'hq')
        lyric.assert_called_once_with('001Ab', fmt='lrc', include_translation=True,
                                      include_romanization=True, preview=False)
        self.assertTrue(response.json['lyrics'])
        self.assertEqual('Final Artist (1) (1).lrc', response.json['lyrics_name'])
        self.assertEqual('old sidecar', old.read_text(encoding='utf-8'))
        self.assertEqual('完整歌词\n', (self.output / response.json['lyrics_name']).read_text(encoding='utf-8'))
        self.assertEqual(b'complete audio', audio.read_bytes())

    def test_lyric_and_storage_errors_never_fail_or_remove_audio(self):
        self.output.mkdir()
        audio = self.output / 'audio.mp3'
        audio.write_bytes(b'keep audio')
        cases = [({'has_lyrics': False}, None), (None, RuntimeError('Cookie=SECRET')),
                 (None, self.module.lyrics_api.LyricError('token=SECRET')),
                 (None, self.adapter.ApiError('NETWORK_ERROR'))]
        self.adapter.download.return_value = {'name': audio.name, 'size': 10}
        for data, exc in cases:
            with self.subTest(exc=exc), mock.patch.object(self.lyrics, 'render', return_value=data, side_effect=exc):
                response = self.client.get('/api/migu/download?id=001Ab&lyrics=1')
                self.assertEqual(200, response.status_code)
                self.assertFalse(response.json['lyrics'])
                self.assertIn('lyrics_error', response.json)
                self.assertNotIn('SECRET', response.get_data(as_text=True))
                self.assertEqual(b'keep audio', audio.read_bytes())
        for audio_name in ('audio.mp3', '../escape.mp3', 'C:\\escape.mp3'):
            self.adapter.download.return_value = {'name': audio_name, 'size': 10}
            with mock.patch.object(self.lyrics, 'render', return_value=self.lyric_data), \
                    mock.patch.object(self.module.os, 'link', side_effect=OSError('Cookie=SECRET')):
                response = self.client.get('/api/migu/download?id=001Ab&lyrics=1')
                self.assertEqual(200, response.status_code)
                self.assertFalse(response.json['lyrics'])
                self.assertNotIn('SECRET', response.get_data(as_text=True))
        self.assertEqual([audio], list(self.output.iterdir()))

    def test_failed_audio_and_disabled_sidecar_never_fetch_lyrics(self):
        with mock.patch.object(self.lyrics, 'render') as render:
            self.adapter.download.side_effect = RuntimeError('token=SECRET')
            response = self.client.get('/api/migu/download?id=001Ab&lyrics=1')
            self.assertEqual(502, response.status_code)
            self.assertNotIn('SECRET', response.get_data(as_text=True))
            self.adapter.download.side_effect = None
            self.adapter.download.return_value = {'name': 'audio.mp3', 'size': 10}
            self.assertEqual(200, self.client.get('/api/migu/download?id=001Ab&lyrics_format=invalid').status_code)
            self.adapter.download.return_value = {'name': 'retained.audio', 'size': 10,
                                                  'conversion_error': '转换失败，保留原文件'}
            response = self.client.get('/api/migu/download?id=001Ab&lyrics=1')
            self.assertEqual(200, response.status_code)
            self.assertIn('conversion_error', response.json)
            self.assertFalse(response.json['lyrics'])
        render.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_atomic_publish_failure_sanitized_and_temp_cleaned(self):
        with mock.patch.object(self.lyrics, 'render', return_value=self.lyric_data), \
                mock.patch.object(self.module.os, 'link', side_effect=OSError('Cookie=SECRET')):
            response = self.client.get('/api/migu/lyrics/download?id=001Ab')
        self.assertEqual(502, response.status_code)
        self.assertNotIn('SECRET', response.get_data(as_text=True))
        self.assertEqual([], list(self.output.iterdir()))


if __name__ == '__main__':
    unittest.main()
