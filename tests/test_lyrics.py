# -*- coding: utf-8 -*-
"""歌词模块的无网络单元测试。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lyrics_api


class LyricsPureFunctionTests(unittest.TestCase):
    sample = (
        '[ti:示例]\n'
        '[ar:歌手]\n'
        '[00:01.20]第一句\n'
        '[00:02.345]第二句\n'
    )

    def test_lrc_to_text_removes_timestamps_and_metadata(self):
        self.assertEqual(lyrics_api.lrc_to_text(self.sample), '第一句\n第二句')
        self.assertNotIn('[00:', lyrics_api.lrc_to_text(self.sample))

    def test_normalize_lrc_preserves_timestamps(self):
        value = lyrics_api.normalize_lrc(self.sample)
        self.assertIn('[00:01.20]第一句', value)
        self.assertIn('[00:02.34]第二句', value)

    def test_document_can_render_optional_sections(self):
        doc = lyrics_api.LyricDocument(
            mid='mid', song_id=1,
            lyric_lrc=self.sample,
            translation_lrc='[00:01.20]translation',
            romanization_lrc='[00:01.20]romanization',
        )
        txt = doc.render_txt(include_translation=True, include_romanization=True)
        self.assertIn('[翻译]', txt)
        self.assertIn('translation', txt)
        self.assertIn('[罗马音]', txt)
        self.assertNotIn('[00:01', txt)
        lrc = doc.render_lrc(include_translation=True, include_romanization=True)
        self.assertIn('[00:01.20]第一句', lrc)
        self.assertIn('; 翻译', lrc)

    def test_render_uses_document_without_network(self):
        doc = lyrics_api.LyricDocument(mid='mid', song_id=1, lyric_lrc=self.sample)
        with mock.patch.object(lyrics_api, 'get_document', return_value=doc):
            result = lyrics_api.render('mid', fmt='txt', preview=False)
        self.assertEqual(result['content'], '第一句\n第二句\n')
        self.assertFalse(result['truncated'])


class LyricsCacheTests(unittest.TestCase):
    def test_cache_round_trip_uses_atomic_file(self):
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / 'lyrics_cache.json'
            with mock.patch.object(lyrics_api, 'CACHE_FILE', cache_path):
                lyrics_api._cache_put('song_ids', 'mid', {'songid': 42}, 60)
                item = lyrics_api._cache_get('song_ids', 'mid')
                self.assertEqual(item['songid'], 42)
                self.assertTrue(cache_path.is_file())
                self.assertNotIn('Cookie', json.dumps(json.loads(cache_path.read_text(encoding='utf-8'))))


if __name__ == '__main__':
    unittest.main()
