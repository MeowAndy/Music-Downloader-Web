"""Offline NCM regression tests; no real tools, HTTP or account files are used."""
import hashlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ncm_api as ncm
import netease_api as api
import test_netease_api as helpers


NCM_BYTES = b'CTENFDAM' + b'private original container' * 80
MP3_BYTES = b'ID3' + b'm' * 2045
FLAC_BYTES = b'fLaC' + b'f' * 2044


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'original.ncm'
        self.source.write_bytes(NCM_BYTES)
        self.source_mtime = self.source.stat().st_mtime_ns
        self.resources = self.root / 'resources'
        self.resources.mkdir()
        self.tool = self.resources / ncm.TOOL_NAME
        self.tool.write_bytes(b'offline pinned converter fixture')
        self.workspaces = []
        self.outputs = [('song.mp3', MP3_BYTES)]
        self.progress = b'out_time_us=1200000\nprogress=end\n'
        self.converter_error = None
        self.decoder_error = None
        self.converter_returncode = 0
        self.decoder_returncode = 0
        self.mutate_staged = False
        self.slots = Mock()
        self.slots.acquire.return_value = True
        real_temporary_directory = tempfile.TemporaryDirectory

        def temporary_directory(*args, **kwargs):
            private = real_temporary_directory(*args, dir=self.root, **kwargs)
            self.workspaces.append(Path(private.name))
            return private

        for target, name, value in (
            (ncm.paths, 'RES_DIR', self.resources),
            (ncm.paths, 'find_exe', Mock(return_value='offline-ffmpeg.exe')),
            (ncm, 'TOOL_SHA256', hashlib.sha256(self.tool.read_bytes()).hexdigest()),
            (ncm, '_SLOTS', self.slots),
            (ncm.tempfile, 'TemporaryDirectory', temporary_directory),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.run = self.enterContext(patch.object(ncm.subprocess, 'run', side_effect=self.fake_run))

    def tearDown(self):
        self.assertEqual(NCM_BYTES, self.source.read_bytes())
        self.assertEqual(self.source_mtime, self.source.stat().st_mtime_ns)
        for workspace in self.workspaces:
            self.assertFalse(workspace.exists(), 'private conversion workspace leaked')
        self.assertEqual(self.slots.acquire.call_count if self.slots.acquire.return_value else 0,
                         self.slots.release.call_count)

    def fake_run(self, command, **kwargs):
        cwd = Path(kwargs['cwd'])
        self.assertIn(cwd, self.workspaces)
        self.assertIs(False, kwargs['shell'])
        self.assertEqual(subprocess.DEVNULL, kwargs['stdin'])
        self.assertTrue(kwargs['capture_output'])
        if command[0] == str(self.tool):
            self.assertEqual(120, kwargs['timeout'])
            self.assertEqual([str(self.tool), '-o', str(cwd / 'output'), str(cwd / 'input.ncm')], command)
            staged = cwd / 'input.ncm'
            self.assertNotEqual(self.source, staged)
            self.assertEqual(NCM_BYTES, staged.read_bytes())
            self.assertFalse(staged.is_symlink())
            if self.mutate_staged:
                staged.write_bytes(b'converter may modify its own copy')
            if self.converter_error:
                raise self.converter_error
            for name, contents in self.outputs:
                (cwd / 'output' / name).write_bytes(contents)
            return SimpleNamespace(returncode=self.converter_returncode, stdout=b'', stderr=b'PRIVATE diagnostic')
        self.assertEqual('offline-ffmpeg.exe', command[0])
        self.assertEqual(180, kwargs['timeout'])
        self.assertEqual(['offline-ffmpeg.exe', '-hide_banner', '-nostdin', '-v', 'error', '-xerror',
                          '-err_detect', 'explode', '-i', str(cwd / 'output' / self.outputs[0][0]),
                          '-map', '0:a:0', '-vn', '-progress', 'pipe:1', '-f', 'null', '-'], command)
        if self.decoder_error:
            raise self.decoder_error
        return SimpleNamespace(returncode=self.decoder_returncode, stdout=self.progress, stderr=b'PRIVATE diagnostic')

    def assert_conversion_error(self):
        with self.assertRaises(ncm.ConversionError) as caught:
            with ncm.extracted_audio(self.source):
                self.fail('invalid conversion must not yield audio')
        self.assertNotIn('PRIVATE', str(caught.exception))
        return caught.exception

    def test_mp3_and_flac_outputs_are_validated_and_temporary(self):
        for name, contents in [('song.mp3', MP3_BYTES), ('song.flac', FLAC_BYTES)]:
            with self.subTest(name=name):
                self.outputs = [(name, contents)]
                before = self.run.call_count
                with ncm.extracted_audio(self.source) as audio:
                    self.assertEqual(contents, audio.read_bytes())
                    self.assertEqual(name, audio.name)
                    self.assertEqual(NCM_BYTES, self.source.read_bytes())
                    self.assertEqual(before + 2, self.run.call_count)
                self.assertFalse(audio.exists())
                self.assertFalse(audio.parent.parent.exists())
        ncm.paths.find_exe.assert_called_with('ffmpeg.exe')
        self.slots.acquire.assert_called_with(timeout=5)

    def test_converter_can_modify_only_its_staged_copy(self):
        self.mutate_staged = True
        with ncm.extracted_audio(self.source) as audio:
            self.assertEqual(MP3_BYTES, audio.read_bytes())
            self.assertEqual(NCM_BYTES, self.source.read_bytes())

    def test_invalid_magic_is_rejected_before_tool_lookup_or_subprocess(self):
        invalid = self.root / 'renamed.ncm'
        invalid.write_bytes(b'not NCM!' + MP3_BYTES)
        with patch.object(ncm, '_tool') as tool:
            with self.assertRaises(ncm.ConversionError):
                with ncm.extracted_audio(invalid):
                    self.fail('invalid magic accepted')
            tool.assert_not_called()
        self.assertFalse(ncm.is_ncm(invalid))
        self.assertTrue(ncm.is_ncm(self.source))
        self.run.assert_not_called()
        self.slots.acquire.assert_not_called()
        self.assertEqual([], self.workspaces)

    def test_missing_empty_and_oversized_sources_are_rejected_before_subprocess(self):
        missing = self.root / 'missing.ncm'
        empty = self.root / 'empty.ncm'
        empty.touch()
        for source in (missing, empty, self.root):
            with self.subTest(source=source), self.assertRaises(ncm.ConversionError):
                with ncm.extracted_audio(source):
                    self.fail('invalid source accepted')
        with patch.object(ncm, 'MAX_BYTES', len(NCM_BYTES) - 1):
            self.assert_conversion_error()
        self.run.assert_not_called()
        self.slots.acquire.assert_not_called()

    def test_pinned_tool_hash_rejects_tampering_before_subprocess(self):
        self.tool.write_bytes(b'untrusted replacement executable')
        self.assert_conversion_error()
        self.run.assert_not_called()
        self.slots.acquire.assert_not_called()
        self.assertEqual([], self.workspaces)

    def test_missing_pinned_tool_does_not_fall_back_to_path(self):
        self.tool.unlink()
        self.assert_conversion_error()
        self.run.assert_not_called()
        ncm.paths.find_exe.assert_not_called()

    def test_busy_conversion_does_not_release_unacquired_slot(self):
        self.slots.acquire.return_value = False
        self.assert_conversion_error()
        self.run.assert_not_called()
        self.assertEqual([], self.workspaces)

    def test_converter_timeout_launch_error_and_failure_are_controlled(self):
        for error in (subprocess.TimeoutExpired('PRIVATE command', 120, output=b'PRIVATE'),
                      OSError('PRIVATE tool path')):
            with self.subTest(error=type(error).__name__):
                self.converter_error = error
                self.assert_conversion_error()
        self.converter_error = None
        self.converter_returncode = 7
        self.assert_conversion_error()
        ncm.paths.find_exe.assert_not_called()

    def test_decoder_timeout_launch_error_and_failure_are_controlled(self):
        for error in (subprocess.TimeoutExpired('PRIVATE command', 180), OSError('PRIVATE ffmpeg')):
            with self.subTest(error=type(error).__name__):
                self.decoder_error = error
                self.assert_conversion_error()
        self.decoder_error = None
        self.decoder_returncode = 1
        self.assert_conversion_error()

    def test_no_output_and_multiple_outputs_are_rejected_before_decode(self):
        for outputs in ([], [('one.mp3', MP3_BYTES), ('two.flac', FLAC_BYTES)]):
            with self.subTest(count=len(outputs)):
                self.outputs = outputs
                self.assert_conversion_error()
        self.assertEqual(2, self.run.call_count)
        ncm.paths.find_exe.assert_not_called()

    def test_invalid_output_extension_size_and_magic_are_rejected(self):
        for name, contents in [('song.wav', MP3_BYTES), ('song.mp3', b'ID3'),
                               ('song.mp3', b'not audio' * 200), ('song.flac', MP3_BYTES)]:
            with self.subTest(name=name, header=contents[:10]):
                self.outputs = [(name, contents)]
                self.assert_conversion_error()
        self.assertEqual(4, self.run.call_count)
        ncm.paths.find_exe.assert_not_called()

    def test_missing_ffmpeg_rejects_output_instead_of_trusting_magic(self):
        ncm.paths.find_exe.return_value = None
        self.assert_conversion_error()
        self.assertEqual(1, self.run.call_count)

    def test_full_decode_requires_positive_numeric_progress(self):
        for progress in (b'', b'progress=end\n', b'out_time_us=0\nprogress=end\n',
                         b'out_time_us=-1\n', b'out_time_us=N/A\n',
                         b'out_time_us=not-a-number\n', b'out_time_ms=1200000\n',
                         b'prefix_out_time_us=1200000\n'):
            with self.subTest(progress=progress):
                self.progress = progress
                self.assert_conversion_error()
        self.progress = b'out_time_us=N/A\nout_time_us=0\nout_time_us=1\nprogress=end\n'
        with ncm.extracted_audio(self.source) as audio:
            self.assertTrue(audio.is_file())

    def test_workspace_and_slot_are_cleaned_if_consumer_raises(self):
        with self.assertRaisesRegex(RuntimeError, 'consumer failed'):
            with ncm.extracted_audio(self.source) as audio:
                self.assertTrue(audio.exists())
                raise RuntimeError('consumer failed')
        self.assertFalse(audio.exists())
        self.slots.release.assert_called_once_with()


class NetEaseNcmTests(unittest.TestCase):
    # Reuse fixtures without inheriting (and rerunning) the unrelated ApiTests suite.
    detail = helpers.ApiTests.detail
    download_body = helpers.ApiTests.download_body
    public_dns = helpers.ApiTests.public_dns
    assertCode = helpers.ApiTests.assertCode

    def setUp(self):
        helpers.ApiTests.setUp(self)
        self.output = self.root / 'output'
        self.enterContext(patch.object(api, 'get_cookie', return_value='MUSIC_U=offline-test'))
        self.credentials = self.enterContext(patch.object(
            api, '_load_credentials', side_effect=AssertionError('must not read credentials')))
        self.enterContext(patch.object(api, 'bridge_call', side_effect=AssertionError('must not run Node')))
        self.subprocess = self.enterContext(patch.object(
            ncm.subprocess, 'run', side_effect=AssertionError('must not execute real tools')))
        self.enterContext(patch.object(api.requests, 'Session', side_effect=AssertionError('must not use network')))
        self.converted = self.root / 'decoded.mp3'
        self.converted.write_bytes(MP3_BYTES)
        self.converted_sources = []

    def tearDown(self):
        self.credentials.assert_not_called()
        self.subprocess.assert_not_called()
        if self.output.exists():
            self.assertFalse(list(self.output.glob('.netease-*')), 'partial download leaked')

    @contextmanager
    def fake_extracted_audio(self, source):
        source = Path(source)
        self.assertEqual(NCM_BYTES, source.read_bytes())
        self.assertEqual('.ncm', source.suffix)
        self.assertEqual(self.output, source.parent)
        self.converted_sources.append(source)
        try:
            yield self.converted
        finally:
            self.assertEqual(NCM_BYTES, source.read_bytes())

    def download_payload(self, payload=NCM_BYTES, advertised='ncm'):
        response = helpers.FakeResponse(payload, headers={'Content-Length': str(len(payload))})
        session = helpers.FakeSession([response])
        body = self.download_body(type=advertised, size=len(payload), md5=hashlib.md5(payload).hexdigest())
        with patch.object(api, '_body', side_effect=[self.detail(), body]), \
                patch.object(api.requests, 'Session', return_value=session), \
                patch.object(api.socket, 'getaddrinfo', side_effect=self.public_dns):
            try:
                return api.download('1', 'hq')
            finally:
                self.assertTrue(response.closed)
                self.assertEqual(1, len(session.calls))

    def test_ncm_response_converts_mp3_and_flac_and_retains_source(self):
        for extension, contents in [('mp3', MP3_BYTES), ('flac', FLAC_BYTES)]:
            with self.subTest(extension=extension):
                self.converted = self.root / ('decoded.' + extension)
                self.converted.write_bytes(contents)
                with patch.object(ncm, 'extracted_audio', side_effect=self.fake_extracted_audio) as converter:
                    result = self.download_payload()
                self.assertTrue(result['converted'])
                self.assertEqual(extension, result['format'])
                self.assertEqual('hq', result['quality'])
                self.assertEqual(api.QUALITY_NAMES['hq'], result['quality_name'])
                self.assertFalse(result['lyrics'])
                self.assertEqual(len(contents), result['size'])
                self.assertEqual(contents, (self.output / result['name']).read_bytes())
                self.assertEqual(NCM_BYTES, (self.output / result['original_name']).read_bytes())
                self.assertNotEqual(result['original_name'], result['name'])
                self.assertNotIn('conversion_error', result)
                converter.assert_called_once_with(self.output / result['original_name'])
        self.assertEqual(4, len(list(self.output.iterdir())))

    def test_conversion_failure_returns_saved_ncm_without_private_diagnostics(self):
        for error in (ncm.ConversionError('PRIVATE diagnostic'), OSError('PRIVATE workspace')):
            with self.subTest(error=type(error).__name__), \
                    patch.object(ncm, 'extracted_audio', side_effect=error) as converter:
                result = self.download_payload()
            self.assertEqual('.ncm', Path(result['name']).suffix)
            self.assertEqual(NCM_BYTES, (self.output / result['name']).read_bytes())
            self.assertEqual(len(NCM_BYTES), result['size'])
            self.assertIn('conversion_error', result)
            self.assertNotIn('PRIVATE', result['conversion_error'])
            self.assertNotIn('converted', result)
            self.assertNotIn('original_name', result)
            converter.assert_called_once_with(self.output / result['name'])
        self.assertEqual(2, len(list(self.output.iterdir())))

    def test_converted_commit_failure_retains_source_and_removes_partial(self):
        real_commit = api._commit_download

        def commit(part, stem, extension):
            if extension != 'ncm':
                raise api.ApiError('STORAGE_ERROR', 500)
            return real_commit(part, stem, extension)

        with patch.object(ncm, 'extracted_audio', side_effect=self.fake_extracted_audio), \
                patch.object(api, '_commit_download', side_effect=commit):
            result = self.download_payload()
        self.assertIn('conversion_error', result)
        self.assertEqual(NCM_BYTES, (self.output / result['name']).read_bytes())
        self.assertEqual([self.output / result['name']], list(self.output.iterdir()))

    def test_ordinary_mp3_and_flac_never_call_converter(self):
        for extension, payload in [('mp3', MP3_BYTES), ('flac', FLAC_BYTES)]:
            with self.subTest(extension=extension), patch.object(ncm, 'extracted_audio') as converter:
                result = self.download_payload(payload, extension)
            converter.assert_not_called()
            self.assertEqual('.' + extension, Path(result['name']).suffix)
            self.assertEqual(payload, (self.output / result['name']).read_bytes())
            self.assertEqual(len(payload), result['size'])
            self.assertNotIn('converted', result)
            self.assertNotIn('conversion_error', result)

    def test_declared_ncm_with_wrong_magic_is_rejected_and_stream_removed(self):
        with patch.object(ncm, 'extracted_audio') as converter:
            self.assertCode('DOWNLOAD_INVALID', self.download_payload, MP3_BYTES, 'ncm')
        converter.assert_not_called()
        self.assertEqual([], list(self.output.iterdir()))

    def test_actual_ncm_header_overrides_advertised_mp3_and_is_converted(self):
        with patch.object(ncm, 'extracted_audio', side_effect=self.fake_extracted_audio) as converter:
            result = self.download_payload(NCM_BYTES, 'mp3')
        self.assertTrue(result['converted'])
        self.assertEqual('.mp3', Path(result['name']).suffix)
        self.assertEqual('.ncm', Path(result['original_name']).suffix)
        self.assertEqual(NCM_BYTES, (self.output / result['original_name']).read_bytes())
        self.assertEqual(MP3_BYTES, (self.output / result['name']).read_bytes())
        converter.assert_called_once_with(self.output / result['original_name'])


if __name__ == '__main__':
    unittest.main()
