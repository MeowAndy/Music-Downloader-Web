"""Offline route contracts without importing the application's QQ credentials."""
import concurrent.futures
import errno
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netease_api
import netease_login


class NetEaseRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('netease_route_fixture', Path(__file__).parents[1] / 'app.py')
        cls.module = importlib.util.module_from_spec(spec)
        fake_qq = types.ModuleType('qq_api')
        fake_login = types.ModuleType('qq_login')
        with mock.patch.dict(sys.modules, {'qq_api': fake_qq, 'qq_login': fake_login}):
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
            'source': 'netease', 'id': '123', 'format': 'txt', 'has_lyrics': True,
            'content': '完整歌词\n', 'cached': True, 'warnings': [], 'truncated': False,
        }

    def test_anonymous_search_allowed(self):
        with mock.patch.object(netease_api, 'search', return_value=[{'id':'306930'}]) as search:
            response = self.client.get('/api/netease/search?kw=复刻回忆')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['songs'][0]['id'], '306930')
        search.assert_called_once_with('复刻回忆', num=30)

    def test_invalid_limit_rejected_before_adapter(self):
        with mock.patch.object(netease_api, 'search') as search:
            for value in ('0', '101', 'NaN', '-1'):
                self.assertEqual(self.client.get('/api/netease/search?kw=x&num=' + value).status_code, 400)
        search.assert_not_called()

    def test_denied_download_has_stable_code(self):
        with mock.patch.object(netease_api, 'download', side_effect=netease_api.ApiError('PERMISSION_DENIED',403)):
            response = self.client.get('/api/netease/download?id=306930&quality=sq')
        self.assertEqual(response.status_code,403)
        self.assertEqual(response.json['code'],'PERMISSION_DENIED')

    def test_download_success(self):
        with mock.patch.object(netease_api, 'download', return_value={'name':'song.mp3','size':20}) as download:
            response=self.client.get('/api/netease/download?id=3401888015')
        self.assertTrue(response.json['ok'])
        download.assert_called_once_with('3401888015','standard')

    def test_internal_error_is_redacted(self):
        with mock.patch.object(netease_api, 'search', side_effect=RuntimeError('MUSIC_U=SECRET')):
            response=self.client.get('/api/netease/search?kw=x')
        self.assertEqual(response.status_code,502)
        self.assertNotIn('SECRET',response.get_data(as_text=True))

    def test_origin_and_host_guards(self):
        with mock.patch.object(netease_login.manager,'start') as start:
            for kwargs in ({'headers':{'Origin':'https://other.example'}},
                           {'headers':{'Sec-Fetch-Site':'cross-site'}},
                           {'base_url':'http://evil.example'}):
                response=self.client.post('/api/netease/login/qr/start',**kwargs)
                self.assertEqual(response.status_code,403)
        start.assert_not_called()

    def test_same_origin_start(self):
        with mock.patch.object(netease_login.manager,'start',return_value=(None,{'id':'task','image':'data:image/png;base64,x'})):
            response=self.client.post('/api/netease/login/qr/start',headers={'Origin':'http://localhost'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['id'],'task')

    def test_missing_and_unknown_task(self):
        self.assertEqual(self.client.get('/api/netease/login/qr/status').status_code,400)
        with mock.patch.object(netease_login.manager,'status',return_value=None):
            self.assertEqual(self.client.get('/api/netease/login/qr/status?id=unknown').status_code,404)

    def test_cancel_and_logout(self):
        with mock.patch.object(netease_login.manager,'cancel') as cancel:
            response=self.client.post('/api/netease/login/qr/cancel',json={'id':'task'})
            self.assertTrue(response.json['ok']); cancel.assert_called_once_with('task')
        with mock.patch.object(netease_login.manager,'logout') as logout:
            self.assertFalse(self.client.post('/api/netease/logout').json['set']); logout.assert_called_once()

    def test_saved_status_never_echoes_cookie(self):
        with mock.patch.object(netease_api,'credential_status',return_value={'set':True,'account':{'nickname':'Tester'}}):
            response=self.client.get('/api/netease/login/status')
        self.assertEqual(set(response.json),{'ok','set','account'})
        self.assertEqual(response.json['account']['nickname'],'Tester')

    def test_lyric_preview_default_and_qq_options(self):
        with mock.patch.object(self.module.netease_lyrics, 'render', return_value=self.lyric_data) as render:
            response = self.client.get('/api/netease/lyrics?id=123')
            self.assertEqual(200, response.status_code)
            self.assertEqual('netease', response.json['source'])
            self.assertEqual('123', response.json['id'])
            self.assertNotIn('mid', response.json)
            render.assert_called_once_with('123', fmt='txt', include_translation=False,
                                           include_romanization=False, preview=True)
            render.reset_mock()
            self.client.get('/api/netease/lyrics?id=123&format=LRC&translation=1&romanization=1&preview=0')
            render.assert_called_once_with('123', fmt='lrc', include_translation=True,
                                           include_romanization=True, preview=False)

    def test_lyric_invalid_ids_formats_and_no_network(self):
        with mock.patch.object(self.module.netease_lyrics, 'render') as render:
            for route in ('/api/netease/lyrics', '/api/netease/lyrics/download'):
                for sid in ('', '0', '-1', '01', '1,2', ' 123', '1' * 21, '١٢٣'):
                    with self.subTest(route=route, sid=sid):
                        response = self.client.get(route, query_string={'id': sid})
                        self.assertEqual(400, response.status_code)
                        self.assertEqual('INVALID_INPUT', response.json['code'])
                response = self.client.get(route, query_string={'id': '123', 'format': 'json'})
                self.assertEqual(400, response.status_code)
        render.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_lyric_routes_origin_and_host_guards(self):
        with mock.patch.object(self.module.netease_lyrics, 'render') as render, \
                mock.patch.object(netease_api, 'download') as download:
            for route in ('/api/netease/lyrics?id=123', '/api/netease/lyrics/download?id=123',
                          '/api/netease/download?id=123&lyrics=1'):
                for kwargs in ({'headers': {'Origin': 'https://other.example'}},
                               {'headers': {'Sec-Fetch-Site': 'cross-site'}},
                               {'base_url': 'http://evil.example'}):
                    with self.subTest(route=route, kwargs=kwargs):
                        self.assertEqual(403, self.client.get(route, **kwargs).status_code)
        render.assert_not_called()
        download.assert_not_called()

    def test_lyric_same_origin_allowed(self):
        with mock.patch.object(self.module.netease_lyrics, 'render', return_value=self.lyric_data):
            response = self.client.get('/api/netease/lyrics?id=123',
                                       headers={'Origin': 'http://localhost'})
        self.assertEqual(200, response.status_code)

    def test_lyric_download_full_safe_unique_filenames_and_metadata(self):
        with mock.patch.object(self.module.netease_lyrics, 'render', return_value=self.lyric_data) as render:
            first = self.client.get('/api/netease/lyrics/download', query_string={
                'id': '123', 'name': '../CON', 'singer': 'x\\y', 'preview': '1',
                'format': 'lrc', 'translation': '1', 'romanization': '1'})
            self.assertEqual(200, first.status_code)
            render.assert_called_once_with('123', fmt='lrc', include_translation=True,
                                           include_romanization=True, preview=False)
            second = self.client.get('/api/netease/lyrics/download', query_string={
                'id': '123', 'name': '../CON', 'singer': 'x\\y', 'format': 'lrc'})
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
        self.assertEqual(2, len(list(self.output.iterdir())))

    def test_lyric_download_windows_reserved_and_empty_names_safe(self):
        with mock.patch.object(self.module.netease_lyrics, 'render', return_value=self.lyric_data):
            reserved = self.client.get('/api/netease/lyrics/download?id=123&name=CON')
            blank = self.client.get('/api/netease/lyrics/download?id=123&name=...')
        self.assertEqual('_CON.txt', reserved.json['name'])
        self.assertEqual('NetEase.txt', blank.json['name'])

    def test_lyric_download_missing_and_errors_do_not_write_files(self):
        with mock.patch.object(self.module.netease_lyrics, 'render', return_value={'has_lyrics': False}):
            response = self.client.get('/api/netease/lyrics/download?id=123')
        self.assertEqual(404, response.status_code)
        self.assertEqual('LYRIC_NOT_FOUND', response.json['code'])
        for route in ('/api/netease/lyrics?id=123', '/api/netease/lyrics/download?id=123'):
            for exc, code in ((RuntimeError('MUSIC_U=SECRET'), 'UPSTREAM_ERROR'),
                              (self.module.lyrics_api.LyricError('Cookie=SECRET'), 'LYRIC_ERROR'),
                              (netease_api.ApiError('NETWORK_ERROR'), 'NETWORK_ERROR')):
                with self.subTest(route=route, code=code), \
                        mock.patch.object(self.module.netease_lyrics, 'render', side_effect=exc):
                    response = self.client.get(route)
                    self.assertEqual(502, response.status_code)
                    self.assertEqual(code, response.json['code'])
                    self.assertNotIn('SECRET', response.get_data(as_text=True))
        self.assertFalse(self.output.exists())

    def test_atomic_lyric_save_never_replaces_concurrent_existing_files(self):
        self.output.mkdir()
        (self.output / 'same.txt').write_text('original', encoding='utf-8')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda i: self.module._save_netease_lyric(str(i), 'same', 'txt'), range(20)))
        self.assertEqual(20, len({item['name'] for item in results}))
        self.assertEqual('original', (self.output / 'same.txt').read_text(encoding='utf-8'))
        self.assertEqual({str(i) for i in range(20)}, {
            (self.output / item['name']).read_text(encoding='utf-8') for item in results})
        self.assertEqual(21, len(list(self.output.iterdir())))

    def test_unsupported_hard_links_fall_back_to_exclusive_atomic_replace(self):
        self.output.mkdir()
        existing = self.output / 'same.txt'
        existing.write_text('original', encoding='utf-8')
        real_replace = self.module.os.replace
        def replace(source, target):
            self.assertEqual(b'', Path(target).read_bytes())
            self.assertEqual(self.output, Path(source).parent)
            real_replace(source, target)
        with mock.patch.object(self.module.os, 'link', side_effect=OSError(errno.ENOTSUP, 'unsupported')), \
                mock.patch.object(self.module.os, 'replace', side_effect=replace) as publish:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(
                    lambda i: self.module._save_netease_lyric(str(i), 'same', 'txt'), range(20)))
        self.assertEqual(20, publish.call_count)
        self.assertEqual(20, len({item['name'] for item in results}))
        self.assertEqual('original', existing.read_text(encoding='utf-8'))
        self.assertEqual({str(i) for i in range(20)}, {
            (self.output / item['name']).read_text(encoding='utf-8') for item in results})
        self.assertEqual(21, len(list(self.output.iterdir())))

    def test_unsupported_link_replace_failure_cleans_reservation_and_temp(self):
        self.output.mkdir()
        existing = self.output / 'song.txt'
        existing.write_text('original', encoding='utf-8')
        with mock.patch.object(self.module.netease_lyrics, 'render', return_value=self.lyric_data), \
                mock.patch.object(self.module.os, 'link', side_effect=OSError(errno.ENOTSUP, 'unsupported')), \
                mock.patch.object(self.module.os, 'replace', side_effect=OSError('Cookie=SECRET')) as publish:
            response = self.client.get('/api/netease/lyrics/download?id=123&name=song')
        self.assertEqual(502, response.status_code)
        self.assertNotIn('SECRET', response.get_data(as_text=True))
        self.assertEqual('song (1).txt', Path(publish.call_args.args[1]).name)
        self.assertEqual('original', existing.read_text(encoding='utf-8'))
        self.assertEqual([existing], list(self.output.iterdir()))

    def test_windows_unsupported_link_error_uses_fallback(self):
        unsupported = OSError('hard links unsupported')
        unsupported.winerror = 50
        with mock.patch.object(self.module.os, 'link', side_effect=unsupported):
            result = self.module._save_netease_lyric('full content', 'song', 'txt')
        self.assertEqual('full content', (self.output / result['name']).read_text(encoding='utf-8'))
        self.assertEqual(1, len(list(self.output.iterdir())))

    def test_atomic_publish_failure_redacted_and_temporary_file_removed(self):
        with mock.patch.object(self.module.netease_lyrics, 'render', return_value=self.lyric_data), \
                mock.patch.object(self.module.os, 'link', side_effect=OSError('Cookie=SECRET')):
            response = self.client.get('/api/netease/lyrics/download?id=123')
        self.assertEqual(502, response.status_code)
        self.assertNotIn('SECRET', response.get_data(as_text=True))
        self.assertEqual([], list(self.output.iterdir()))

    def test_invalid_sidecar_format_and_id_rejected_before_audio(self):
        with mock.patch.object(netease_api, 'download') as download, \
                mock.patch.object(self.module.netease_lyrics, 'render') as render:
            for query in ({'id': '123', 'lyrics': '1', 'lyrics_format': 'json'},
                          {'id': '01', 'lyrics': '1'}):
                response = self.client.get('/api/netease/download', query_string=query)
                self.assertEqual(400, response.status_code)
                self.assertEqual('INVALID_INPUT', response.json['code'])
        download.assert_not_called()
        render.assert_not_called()

    def test_disabled_sidecar_preserves_two_argument_adapter_contract(self):
        with mock.patch.object(netease_api, 'download', return_value={
                'name': 'song.mp3', 'size': 20, 'lyrics': False}) as download, \
                mock.patch.object(self.module.netease_lyrics, 'render') as render:
            response = self.client.get('/api/netease/download?id=123&quality=sq&lyrics_format=invalid')
        self.assertEqual(200, response.status_code)
        download.assert_called_once_with('123', 'sq')
        render.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_sidecar_runs_after_audio_uses_final_name_and_preserves_old_sidecar(self):
        self.output.mkdir()
        audio = self.output / 'Final Artist (1).mp3'
        old = self.output / 'Final Artist (1).lrc'
        old.write_text('old sidecar', encoding='utf-8')
        events = []
        def download(*args):
            events.append('audio')
            audio.write_bytes(b'complete audio')
            return {'name': audio.name, 'size': audio.stat().st_size, 'lyrics': False}
        def render(*args, **kwargs):
            self.assertTrue(audio.is_file())
            events.append('lyrics')
            return self.lyric_data
        with mock.patch.object(netease_api, 'download', side_effect=download) as dl, \
                mock.patch.object(self.module.netease_lyrics, 'render', side_effect=render) as lyric:
            response = self.client.get('/api/netease/download', query_string={
                'id': '123', 'quality': 'hq', 'lyrics': '1', 'lyrics_format': 'lrc',
                'translation': '1', 'romanization': '1', 'name': 'wrong title'})
        self.assertEqual(200, response.status_code)
        self.assertEqual(['audio', 'lyrics'], events)
        dl.assert_called_once_with('123', 'hq')
        lyric.assert_called_once_with('123', fmt='lrc', include_translation=True,
                                      include_romanization=True, preview=False)
        self.assertTrue(response.json['lyrics'])
        self.assertEqual('Final Artist (1) (1).lrc', response.json['lyrics_name'])
        self.assertEqual('old sidecar', old.read_text(encoding='utf-8'))
        self.assertEqual('完整歌词\n', (self.output / response.json['lyrics_name']).read_text(encoding='utf-8'))
        self.assertEqual(b'complete audio', audio.read_bytes())

    def test_missing_lyric_and_lyric_errors_never_fail_or_remove_audio(self):
        self.output.mkdir()
        audio = self.output / 'audio.flac'
        audio.write_bytes(b'keep audio')
        cases = [({'has_lyrics': False}, None), (None, RuntimeError('MUSIC_U=SECRET')),
                 (None, self.module.lyrics_api.LyricError('Cookie=SECRET')),
                 (None, netease_api.ApiError('NETWORK_ERROR'))]
        for data, exc in cases:
            with self.subTest(exc=exc), \
                    mock.patch.object(netease_api, 'download', return_value={
                        'name': audio.name, 'size': 10, 'lyrics': False}), \
                    mock.patch.object(self.module.netease_lyrics, 'render', return_value=data, side_effect=exc):
                response = self.client.get('/api/netease/download?id=123&lyrics=1')
                self.assertEqual(200, response.status_code)
                self.assertTrue(response.json['ok'])
                self.assertFalse(response.json['lyrics'])
                self.assertIn('lyrics_error', response.json)
                self.assertNotIn('SECRET', response.get_data(as_text=True))
                self.assertEqual(b'keep audio', audio.read_bytes())
                self.assertEqual([audio], list(self.output.iterdir()))

    def test_sidecar_storage_failure_and_unsafe_final_name_are_isolated(self):
        self.output.mkdir()
        audio = self.output / 'audio.mp3'
        audio.write_bytes(b'keep audio')
        for audio_name in ('audio.mp3', '../escape.mp3', 'C:\\escape.mp3'):
            with self.subTest(name=audio_name), \
                    mock.patch.object(netease_api, 'download', return_value={
                        'name': audio_name, 'size': 20}), \
                    mock.patch.object(self.module.netease_lyrics, 'render', return_value=self.lyric_data), \
                    mock.patch.object(self.module.os, 'link', side_effect=OSError('Cookie=SECRET')):
                response = self.client.get('/api/netease/download?id=123&lyrics=1')
                self.assertEqual(200, response.status_code)
                self.assertTrue(response.json['ok'])
                self.assertIn('lyrics_error', response.json)
                self.assertFalse(response.json['lyrics'])
                self.assertNotIn('SECRET', response.get_data(as_text=True))
                self.assertEqual(b'keep audio', audio.read_bytes())
        self.assertEqual([audio], list(self.output.iterdir()))

    def test_audio_failure_never_attempts_lyrics(self):
        with mock.patch.object(netease_api, 'download', side_effect=RuntimeError('MUSIC_U=SECRET')), \
                mock.patch.object(self.module.netease_lyrics, 'render') as render:
            response = self.client.get('/api/netease/download?id=123&lyrics=1')
        self.assertEqual(502, response.status_code)
        self.assertNotIn('SECRET', response.get_data(as_text=True))
        render.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_ncm_conversion_failure_retains_result_and_skips_sidecar(self):
        result = {'name': 'retained.ncm', 'size': 30, 'lyrics': False,
                  'conversion_error': '音频转换失败，已保留 NCM 文件。', 'format': 'ncm'}
        with mock.patch.object(netease_api, 'download', return_value=result), \
                mock.patch.object(self.module.netease_lyrics, 'render') as render:
            response = self.client.get('/api/netease/download?id=123&lyrics=1')
        self.assertEqual(200, response.status_code)
        for key, value in result.items():
            self.assertEqual(value, response.json[key])
        self.assertIn('lyrics_error', response.json)
        render.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
