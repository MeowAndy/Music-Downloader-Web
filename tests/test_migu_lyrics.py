"""Offline Migu lyrics contracts; adapters and QQ credentials are never imported."""
import importlib.util
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class MiguLyricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter = types.ModuleType('migu_api')
        class ApiError(RuntimeError):
            def __init__(self, code='UPSTREAM_ERROR', status=502):
                self.code, self.status = code, status
                super().__init__('咪咕请求失败')
        def validate_id(value):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]{1,32}', value):
                raise ApiError('INVALID_INPUT', 400)
            return value
        cls.adapter.ApiError = ApiError
        cls.adapter.validate_id = validate_id
        cls.adapter.lyric_text = mock.Mock()
        shared_spec = importlib.util.spec_from_file_location('migu_shared_fixture', ROOT / 'lyrics_api.py')
        cls.shared = importlib.util.module_from_spec(shared_spec)
        spec = importlib.util.spec_from_file_location('migu_lyric_fixture', ROOT / 'migu_lyrics.py')
        cls.api = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {'qq_api': types.ModuleType('qq_api'),
                                          'migu_shared_fixture': cls.shared}):
            shared_spec.loader.exec_module(cls.shared)
        with mock.patch.dict(sys.modules, {'lyrics_api': cls.shared, 'migu_api': cls.adapter}):
            spec.loader.exec_module(cls.api)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache = Path(temp.name) / 'lyrics_cache.json'
        cache_patch = mock.patch.object(self.shared, 'CACHE_FILE', self.cache)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        self.upstream = self.adapter.lyric_text
        self.upstream.reset_mock(side_effect=True, return_value=True)
        self.upstream.return_value = '\ufeff[ti:示例]\r\n[00:01.20]原文一\r\n[00:02.345]原文二'

    def test_plaintext_default_txt_preserves_string_identity(self):
        for sid in ('00123', 'A00b9', '0', 'Z' * 32):
            with self.subTest(sid=sid):
                result = self.api.render(sid)
                self.assertEqual('原文一\n原文二\n', result['content'])
                self.assertEqual('migu', result['source'])
                self.assertEqual(sid, result['id'])
                self.assertNotIn('mid', result)
                self.assertNotIn('songid', result)
                self.assertTrue(result['has_lyrics'])
                self.assertFalse(result['has_translation'])
                self.assertFalse(result['has_romanization'])
                self.assertFalse(result['cached'])
                self.assertFalse(result['truncated'])
        self.assertEqual([mock.call(sid) for sid in ('00123', 'A00b9', '0', 'Z' * 32)],
                         self.upstream.call_args_list)

    def test_lrc_uses_shared_renderer_and_plaintext_never_base64_decodes(self):
        doc = self.api.get_document('A01')
        expected = self.shared.render_document(doc, 'lrc', source='migu')
        result = self.api.render('A01', 'LRC')
        self.assertEqual(expected['content'], result['content'])
        self.assertIn('[00:02.34]原文二', result['content'])
        self.upstream.return_value = 'SGVsbG8=\nplain lyric'
        self.assertEqual('SGVsbG8=\nplain lyric\n', self.api.render('B02')['content'])
        self.assertEqual('SGVsbG8=\nplain lyric\n', self.api.render('B02', 'lrc')['content'])

    def test_optional_sections_warn_and_leave_original_only_without_cache_pollution(self):
        first = self.api.render('001', include_translation=True, include_romanization=True)
        self.assertEqual({'lyric': '原文一\n原文二'}, first['sections'])
        self.assertEqual('原文一\n原文二\n', first['content'])
        self.assertEqual(2, len(first['warnings']))
        self.assertIn('翻译', first['warnings'][0])
        self.assertIn('罗马音', first['warnings'][1])
        self.assertFalse(first['has_translation'])
        self.assertFalse(first['has_romanization'])
        self.assertEqual([], self.api.render('001')['warnings'])
        self.assertEqual([], self.api.get_document('001').warnings)

    def test_preview_shared_limits_and_full_render(self):
        self.upstream.return_value = '\n'.join('[00:01.00]' + '词' * 80 for _ in range(80))
        result = self.api.render('a', preview=True)
        self.assertTrue(result['truncated'])
        self.assertEqual(80, result['total_lines'])
        self.assertLessEqual(len(result['content']), 1601)
        self.assertLessEqual(len(result['sections']['lyric']), 901)
        self.assertEqual(80, len(self.api.render('a')['content'].splitlines()))

    def test_positive_cache_identity_ttl_and_normalized_fields_only(self):
        with mock.patch.object(self.shared.time, 'time', return_value=1000):
            self.api.get_document('001Ab')
            self.assertTrue(self.api.get_document('001Ab').cached)
        saved = json.loads(self.cache.read_text(encoding='utf-8'))
        self.assertEqual({}, saved['song_ids'])
        self.assertEqual({'migu:001Ab'}, set(saved['lyrics']))
        item = saved['lyrics']['migu:001Ab']
        self.assertEqual('ok', item['status'])
        self.assertEqual(7 * 24 * 3600, item['expires_at'] - item['fetched_at'])
        self.assertEqual({'id': '001Ab', 'lyric_lrc': '[ti:示例]\n[00:01.20]原文一\n[00:02.345]原文二'},
                         item['document'])
        self.upstream.assert_called_once_with('001Ab')

    def test_confirmed_empty_only_has_short_negative_ttl(self):
        for sid, value in (('A', ''), ('B', ' \r\n\t')):
            self.upstream.return_value = value
            with mock.patch.object(self.shared.time, 'time', return_value=1000):
                result = self.api.render(sid)
                self.assertFalse(result['has_lyrics'])
                self.assertTrue(result['warnings'])
                item = self.shared._cache_get('lyrics', 'migu:' + sid)
                self.assertEqual('empty', item['status'])
                self.assertEqual(15 * 60, item['expires_at'] - item['fetched_at'])
                self.assertTrue(self.api.get_document(sid).cached)

    def test_invalid_responses_errors_never_negative_cache(self):
        invalid = (None, False, 123, {}, [], 'bad\x00text', 'bad\x7ftext', '\ud800',
                   '[ti:metadata]\n[00:00.00]', '[ar:artist]')
        for value in invalid:
            with self.subTest(value=value):
                self.upstream.return_value = value
                with self.assertRaises(self.shared.LyricError):
                    self.api.get_document('A')
                self.assertFalse(self.cache.exists())
        for exc in (RuntimeError('Cookie=SECRET'), self.adapter.ApiError('NETWORK_ERROR')):
            self.upstream.side_effect = exc
            with self.assertRaises(type(exc)):
                self.api.get_document('A')
            self.assertFalse(self.cache.exists())

    def test_utf8_size_limit_and_allowed_controls(self):
        for text in ('a' * (self.api.MAX_FIELD_BYTES + 1),
                     '词' * (self.api.MAX_FIELD_BYTES // 3 + 1)):
            self.upstream.return_value = text
            with self.assertRaises(self.shared.LyricError):
                self.api.get_document('A')
            self.assertFalse(self.cache.exists())
        self.upstream.return_value = 'a' * self.api.MAX_FIELD_BYTES
        self.assertTrue(self.api.get_document('A').has_lyrics)
        self.upstream.return_value = 'a\tb\r\nc\rd'
        self.assertEqual('a\tb\nc\nd', self.api.get_document('B').lyric_lrc)

    def test_bad_ids_formats_do_not_call_adapter(self):
        for sid in (None, True, False, 1, -1, 1.5, '', 'a' * 33, ' a', 'a\n', 'a,b', '../a', '１２３'):
            with self.subTest(sid=sid), self.assertRaises(self.adapter.ApiError):
                self.api.get_document(sid)
        with self.assertRaises(self.shared.LyricError):
            self.api.render('A', 'json')
        self.upstream.assert_not_called()

    def test_expiry_force_and_failed_force_preserve_good_cache(self):
        with mock.patch.object(self.shared.time, 'time', return_value=1000):
            self.api.get_document('A')
            self.api.get_document('A', force=True)
        with mock.patch.object(self.shared.time, 'time', return_value=1000 + self.shared.LYRIC_TTL + 1):
            self.assertFalse(self.api.get_document('A').cached)
        self.assertEqual(3, self.upstream.call_count)
        before = self.cache.read_bytes()
        self.upstream.side_effect = self.adapter.ApiError()
        with self.assertRaises(self.adapter.ApiError):
            self.api.get_document('A', force=True)
        self.assertEqual(before, self.cache.read_bytes())

    def test_corrupt_cache_is_refetched(self):
        for document in ({'id': 'wrong', 'lyric_lrc': 'bad'}, {'id': 'A', 'lyric_lrc': []},
                         {'id': 'A', 'lyric_lrc': 'bad\x00'}, {'id': 'A', 'lyric_lrc': '[ti:title]'}):
            self.shared._cache_put('lyrics', 'migu:A', {'document': document, 'status': 'ok'}, 60)
            self.assertFalse(self.api.get_document('A').cached)
        self.assertEqual(4, self.upstream.call_count)

    def test_qq_and_netease_cache_entries_untouched(self):
        self.shared._cache_put('song_ids', 'qqmid', {'songid': 42}, 60)
        self.shared._cache_put('lyrics', 'qqmid', {'document': {
            'song_id': 42, 'lyric_lrc': '[00:01]QQ歌词', 'warnings': []}}, 60)
        self.shared._cache_put('lyrics', 'netease:42', {'document': {
            'song_id': 42, 'lyric_lrc': '[00:01]网易歌词'}}, 60)
        before = json.loads(self.cache.read_text(encoding='utf-8'))
        self.api.get_document('00042')
        after = json.loads(self.cache.read_text(encoding='utf-8'))
        self.assertEqual(before['song_ids'], after['song_ids'])
        for key in ('qqmid', 'netease:42'):
            self.assertEqual(before['lyrics'][key], after['lyrics'][key])
        qq = self.shared.render('qqmid')
        self.assertEqual('qqmid', qq['mid'])
        self.assertEqual(42, qq['songid'])
        self.assertNotIn('source', qq)
        self.assertEqual('QQ歌词\n', qq['content'])


if __name__ == '__main__':
    unittest.main()
