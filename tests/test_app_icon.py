"""Offline icon route tests with application dependencies and paths isolated."""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from flask import Blueprint


ROOT = Path(__file__).resolve().parents[1]
ICON_BYTES = b'\x00\x00\x01\x00\x01\x00test icon bytes\xff'


class AppIconTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.resources = root / 'resources'
        self.resources.mkdir()
        self.data = root / 'data'
        self.data.mkdir()
        self.icon = self.resources / 'app.ico'
        self.icon.write_bytes(ICON_BYTES)
        # A writable-data icon must never override the bundled resource.
        (self.data / 'app.ico').write_bytes(b'not the resource icon')

        dependencies = {name: types.ModuleType(name) for name in (
            'ffmpeg_api', 'kg_api', 'paths', 'qq_api', 'qq_login', 'lyrics_api',
            'netease_api', 'netease_login', 'netease_lyrics', 'migu_api',
            'migu_lyrics', 'migu_login', 'hifiti_api', 'vocal_api', 'vip_api',
        )}
        dependencies['paths'].APP_DIR = self.data
        dependencies['paths'].RES_DIR = self.resources
        dependencies['paths'].OUTPUT_DIR = self.data / 'output'
        for name in ('migu_login', 'hifiti_api'):
            dependencies[name].blueprint = Blueprint(name, __name__)

        spec = importlib.util.spec_from_file_location('app_icon_fixture', ROOT / 'app.py')
        self.module = importlib.util.module_from_spec(spec)
        # Never import real adapters or read user credentials during app loading.
        with mock.patch.dict(sys.modules, dependencies):
            spec.loader.exec_module(self.module)
        self.module.app.config['TESTING'] = True
        self.client = self.module.app.test_client()

    def test_serves_exact_resource_bytes(self):
        with self.client.get('/app.ico') as response:
            self.assertEqual(200, response.status_code)
            self.assertEqual(ICON_BYTES, response.data)

    def test_icon_content_type(self):
        with self.client.get('/app.ico') as response:
            self.assertEqual(200, response.status_code)
            self.assertEqual('image/x-icon', response.mimetype)

    def test_missing_resource_icon_returns_404_without_data_fallback(self):
        self.icon.unlink()
        with self.client.get('/app.ico') as response:
            self.assertEqual(404, response.status_code)

    def test_extra_segments_rejected(self):
        for path in ('/app.ico/', '/app.ico/extra', '/app.ico/app.ico'):
            with self.subTest(path=path), self.client.get(path) as response:
                self.assertEqual(404, response.status_code)

    def test_traversal_rejected(self):
        (self.resources / 'private.txt').write_bytes(b'private test resource')
        for path in ('/app.ico/../private.txt', '/app.ico/%2e%2e/private.txt',
                     '/app.ico/..%2fprivate.txt', '/app.ico/..%5cprivate.txt',
                     '/app.ico%2f..%2fprivate.txt', '/app.ico%5c..%5cprivate.txt'):
            with self.subTest(path=path), self.client.get(path) as response:
                self.assertEqual(404, response.status_code)
                self.assertNotIn(b'private test resource', response.data)

    def test_query_parameters_cannot_select_another_file(self):
        (self.resources / 'other.ico').write_bytes(b'another icon')
        with self.client.get('/app.ico', query_string={
                'path': '../data/app.ico', 'filename': 'other.ico',
                'name': 'other.ico'}) as response:
            self.assertEqual(200, response.status_code)
            self.assertEqual(ICON_BYTES, response.data)


if __name__ == '__main__':
    unittest.main()
