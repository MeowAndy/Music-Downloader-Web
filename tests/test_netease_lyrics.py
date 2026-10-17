"""Focused offline NetEase lyrics and shared QQ rendering compatibility tests."""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lyrics_api as shared
import netease_api
import netease_lyrics as api

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = {
    'code': 200,
    'lrc': {'lyric': '[ti:示例]\r\n[00:01.20]原文一\r\n[00:02.345]原文二'},
    'tlyric': {'lyric': '[00:01.20]翻译一'},
    'romalrc': {'lyric': '[00:01.20]romaji'},
    'cookie': 'MUSIC_U=body-secret',
    'lyricUser': {'nickname': 'not-for-cache'},
}


class NetEaseLyricTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache = Path(temp.name) / 'lyrics_cache.json'
        self.start_patch(patch.object(shared, 'CACHE_FILE', self.cache))
        self.bridge = self.start_patch(patch.object(netease_api, 'bridge_call'))
        self.bridge.return_value = {'ok': True, 'body': copy.deepcopy(SAMPLE),
                                    'cookies': ['MUSIC_U=header-secret']}

    def start_patch(self, value):
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def body(self, body):
        self.bridge.return_value = {'ok': True, 'body': body, 'cookies': []}

    def test_plaintext_fields_default_txt_and_provider_identity(self):
        result = api.render(123)
        self.bridge.assert_called_once_with('lyric', {'id': '123'}, None)
        self.assertEqual('原文一\n原文二\n', result['content'])
        self.assertEqual('netease', result['source'])
        self.assertEqual('123', result['id'])
        self.assertNotIn('mid', result)
        self.assertNotIn('songid', result)
        self.assertEqual({'lyric': '原文一\n原文二'}, result['sections'])
        for name in ('has_lyrics', 'has_translation', 'has_romanization'):
            self.assertTrue(result[name])
        self.assertFalse(result['cached'])
        self.assertFalse(result['truncated'])

    def test_translation_romanization_and_lrc_match_shared_renderer(self):
        for fmt in ('txt', 'lrc', 'LRC'):
            with self.subTest(fmt=fmt):
                result = api.render('123', fmt, True, True)
                doc = api.get_document('123')
                expected = shared.render_document(doc, fmt, True, True, source='netease')
                for key in ('content', 'sections', 'format', 'total_lines'):
                    self.assertEqual(expected[key], result[key])
                self.assertIn('翻译一', result['content'])
                self.assertIn('romaji', result['content'])
        self.assertIn('[00:02.34]原文二', result['content'])

    def test_preview_uses_shared_limits_and_preserves_total_lines(self):
        self.body({'code': 200, 'lrc': {'lyric': '\n'.join(
            '[00:01.00]' + '词' * 80 for _ in range(80))}})
        result = api.render('123', preview=True)
        self.assertTrue(result['truncated'])
        self.assertEqual(80, result['total_lines'])
        self.assertLessEqual(len(result['content']), 1601)
        self.assertLessEqual(len(result['sections']['lyric']), 901)
        self.assertEqual(80, api.render('123')['total_lines'])

    def test_plain_text_is_not_base64_decoded(self):
        self.body({'code': 200, 'lrc': {'lyric': 'SGVsbG8=\nplain lyric'}})
        self.assertEqual('SGVsbG8=\nplain lyric\n', api.render('123')['content'])

    def test_positive_cache_is_seven_days_and_contains_only_normalized_fields(self):
        with patch.object(shared.time, 'time', return_value=1000):
            api.get_document('123')
        saved = json.loads(self.cache.read_text(encoding='utf-8'))
        self.assertEqual(1, saved['version'])
        self.assertEqual({}, saved['song_ids'])
        item = saved['lyrics']['netease:123']
        self.assertEqual('ok', item['status'])
        self.assertEqual(7 * 24 * 3600, item['expires_at'] - item['fetched_at'])
        self.assertNotIn('secret', self.cache.read_text(encoding='utf-8'))
        self.assertNotIn('cookie', self.cache.read_text(encoding='utf-8').lower())
        self.assertNotIn('nickname', self.cache.read_text(encoding='utf-8'))
        with patch.object(shared.time, 'time', return_value=1001):
            self.assertTrue(api.get_document('123').cached)
        self.assertEqual(1, self.bridge.call_count)

    def test_negative_results_have_short_cache_and_no_optional_content(self):
        bodies = [
            {'nolyric': True}, {'uncollected': True}, {'lrc': {'lyric': ''}},
            {'lrc': {'lyric': '[ti:title]\n[00:00.00]'}},
            {'lrc': {'lyric': '[99:00.00]纯音乐，请欣赏'}},
            {'lrc': {'lyric': '[00:00.00]此歌曲为没有填词的纯音乐，请您欣赏'}},
            {'lrc': {'lyric': 'Instrumental'}},
        ]
        for i, body in enumerate(bodies, 1):
            with self.subTest(body=body), patch.object(shared.time, 'time', return_value=1000):
                self.body({'code': 200, 'tlyric': {'lyric': 'not a main lyric'}, **body})
                doc = api.get_document(i)
                self.assertFalse(doc.has_lyrics)
                self.assertFalse(doc.has_translation)
                self.assertTrue(doc.warnings)
                item = shared._cache_get('lyrics', 'netease:' + str(i))
                self.assertEqual('empty', item['status'])
                self.assertEqual(15 * 60, item['expires_at'] - item['fetched_at'])
                self.assertTrue(api.get_document(i).cached)

    def test_placeholder_with_real_lyrics_is_not_discarded(self):
        self.body({'code': 200, 'lrc': {'lyric': '[00:00]纯音乐，请欣赏\n[00:01]实际歌词'}})
        self.assertTrue(api.get_document('123').has_lyrics)

    def test_expiry_and_force_refetch(self):
        with patch.object(shared.time, 'time', return_value=1000):
            api.get_document('123')
            api.get_document('123', force=True)
        with patch.object(shared.time, 'time', return_value=1000 + shared.LYRIC_TTL + 1):
            self.assertFalse(api.get_document('123').cached)
        self.assertEqual(3, self.bridge.call_count)

    def test_errors_and_malformed_responses_are_not_negative_cached(self):
        invalid = [
            {'code': 500, 'lrc': {'lyric': ''}}, {'code': 200},
            {'code': 200, 'lrc': []}, {'code': 200, 'lrc': None},
            {'code': 200, 'lrc': 'text'},
            {'code': 200, 'lrc': {'lyric': 123}},
            {'code': 200, 'tlyric': {'lyric': False}},
            {'code': 200, 'romalrc': {'lyric': {}}},
            {'code': 200, 'nolyric': 'false'}, {'code': 200, 'uncollected': 1},
            {'code': 200, 'lrc': {'lyric': 'bad\x00text'}},
            {'code': 200, 'lrc': {'lyric': '\ud800'}},
        ]
        for body in invalid:
            with self.subTest(body=body):
                self.body(body)
                with self.assertRaises((shared.LyricError, netease_api.ApiError)):
                    api.get_document('123')
                self.assertFalse(self.cache.exists())
        self.bridge.side_effect = netease_api.ApiError('NETWORK_ERROR')
        with self.assertRaises(netease_api.ApiError):
            api.get_document('123')
        self.assertFalse(self.cache.exists())

    def test_size_limits_are_utf8_byte_based_and_include_all_sections(self):
        for body in (
            {'lrc': {'lyric': 'a' * (api.MAX_FIELD_BYTES + 1)}},
            {'lrc': {'lyric': '词' * (api.MAX_FIELD_BYTES // 3 + 1)}},
            {key: {'lyric': 'a' * (400 * 1024)} for key in ('lrc', 'tlyric', 'romalrc')},
        ):
            with self.subTest(keys=list(body)):
                self.body({'code': 200, **body})
                with self.assertRaises(shared.LyricError):
                    api.get_document('123')
                self.assertFalse(self.cache.exists())

    def test_bad_ids_and_formats_do_not_reach_bridge(self):
        for sid in (None, True, False, 0, -1, '0', '01', '1,2', ' 123', '١٢٣',
                    '1' * 21, '123\n', 1.5, {'id': 1}):
            with self.subTest(sid=sid), self.assertRaises(netease_api.ApiError):
                api.get_document(sid)
        with self.assertRaises(shared.LyricError):
            api.render('123', fmt='json')
        self.bridge.assert_not_called()

    def test_qq_cache_keys_and_render_response_are_unchanged(self):
        shared._cache_put('song_ids', 'qqmid', {'songid': 42, 'mid': 'qqmid'}, 60)
        shared._cache_put('lyrics', 'qqmid', {'document': {
            'song_id': 42, 'lyric_lrc': '[00:01]QQ歌词', 'warnings': []}}, 60)
        before = json.loads(self.cache.read_text(encoding='utf-8'))
        api.get_document('42')
        after = json.loads(self.cache.read_text(encoding='utf-8'))
        self.assertEqual(before['song_ids'], after['song_ids'])
        self.assertEqual(before['lyrics']['qqmid'], after['lyrics']['qqmid'])
        qq = shared.render('qqmid')
        self.assertEqual({'mid': 'qqmid', 'songid': 42, 'format': 'txt',
                          'content': 'QQ歌词\n', 'sections': {'lyric': 'QQ歌词'},
                          'has_lyrics': True, 'has_translation': False,
                          'has_romanization': False, 'cached': True, 'warnings': [],
                          'total_lines': 1, 'truncated': False}, qq)
        self.assertEqual(42, shared.resolve_song_id('qqmid'))
        self.assertEqual(1, self.bridge.call_count)

    def test_corrupt_provider_cache_is_refetched(self):
        for document in ({'song_id': 999}, {'song_id': 123, 'lyric_lrc': []},
                         {'song_id': 123, 'warnings': 'bad'}):
            shared._cache_put('lyrics', 'netease:123', {'document': document}, 60)
            self.assertFalse(api.get_document('123').cached)
        self.assertEqual(3, self.bridge.call_count)

    def test_force_error_does_not_replace_valid_cached_lyrics(self):
        api.get_document('123')
        before = self.cache.read_bytes()
        self.bridge.side_effect = netease_api.ApiError('NETWORK_ERROR')
        with self.assertRaises(netease_api.ApiError):
            api.get_document('123', force=True)
        self.assertEqual(before, self.cache.read_bytes())
        self.assertTrue(api.get_document('123').cached)


class NetEaseLyricBridgeTests(unittest.TestCase):
    def node(self, script):
        # Every script uses fake requests or argument validation; no HTTP is made.
        result = subprocess.run([shared.paths.node_exe(), '-e', script],
                                capture_output=True, timeout=15, cwd=ROOT,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', errors='replace'))

    def test_bridge_restricts_lyric_operation_to_single_canonical_string_id(self):
        self.node(r"""
const assert = require('node:assert/strict');
const {argsFor} = require('./vendor/netease_bridge');
assert.deepEqual(argsFor('lyric', {id:'123'}), {id:'123'});
assert.deepEqual(argsFor('lyric', {id:'12345678901234567890'}), {id:'12345678901234567890'});
for (const id of [0,123,true,null,[],{},'0','01','-1','1,2',' 1','1\n','1'.repeat(21)]) {
  assert.throws(() => argsFor('lyric',{id}), e => e.safeCode === 'INVALID_INPUT');
}
for (const extra of ['url','crypto','proxy','cookie','headers','domain','realIP','level','randomCNIP']) {
  assert.throws(() => argsFor('lyric',{id:'123',[extra]:'bad'}), e => e.safeCode === 'INVALID_INPUT');
}
assert.throws(() => argsFor('lyric',{}));
assert.throws(() => argsFor('arbitrary_module',{id:'123'}));
""")

    def test_pinned_module_uses_only_lyric_endpoint_and_fixed_versions(self):
        self.node(r"""
const assert = require('node:assert/strict');
const lyric = require('./vendor/netease/module/lyric');
let calls = 0;
lyric({id:'123',cookie:{},timeout:15000,randomCNIP:false}, (url,data,options) => {
  calls++;
  assert.equal(url,'/api/song/lyric');
  assert.deepEqual(data,{id:'123',tv:-1,lv:-1,rv:-1,kv:-1,_nmclfl:1});
  assert.equal(options.timeout,15000);
  assert.equal(options.randomCNIP,false);
  return {body:{code:200}};
});
assert.equal(calls,1);
""")

    def test_vendored_module_matches_recorded_pinned_sha256(self):
        provenance = json.loads((ROOT / 'vendor/netease/UPSTREAM.json').read_text(encoding='utf-8'))
        record = provenance['additional_modules']['module/lyric.js']
        digest = hashlib.sha256((ROOT / 'vendor/netease/module/lyric.js').read_bytes()).hexdigest()
        self.assertEqual(record['sha256'], digest)
        self.assertEqual(provenance['commit'], record['commit'])
        self.assertFalse(record['modified'])


if __name__ == '__main__':
    unittest.main()
