"""Owned-process tests: no AI launch, downloads, or credentials.

Native Windows cases use isolated Python hosts and tiny sleeping children.
"""
import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import vocal_api as vocal

MODULE = Path(__file__).resolve().parents[1] / 'owned_processes.py'


def fresh_module():
    spec = importlib.util.spec_from_file_location('_isolated_owned_processes', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OwnedProcessesTests(unittest.TestCase):
    def setUp(self):
        self.owned = fresh_module()
        self.api = mock.Mock()
        self.api.create.side_effect = [101, 102, 103]

    def initialize(self):
        with mock.patch.object(self.owned, 'os', SimpleNamespace(name='nt')), \
                mock.patch.object(self.owned, '_WindowsJobs', return_value=self.api):
            self.owned.initialize()
            self.owned.initialize()
        self.api.create.assert_called_once_with()

    def test_four_api_modules_route_all_subprocess_launches_through_owner(self):
        # Parse instead of importing qq_api: import historically reads a cookie.
        for name in ('vocal_api.py', 'qq_api.py', 'ffmpeg_api.py', 'vip_api.py'):
            tree = ast.parse((MODULE.parent / name).read_text(encoding='utf-8'))
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute)
                     and node.func.attr in ('run', 'Popen', 'popen')
                     and isinstance(node.func.value, ast.Name)
                     and node.func.value.id in ('subprocess', 'owned_processes')]
            self.assertTrue(calls, name)
            for call in calls:
                self.assertEqual(call.func.value.id, 'owned_processes', name)

    def test_initialize_is_explicit_and_idempotent(self):
        self.assertFalse(self.owned._initialized)
        self.initialize()
        self.assertEqual(self.owned._job, 101)

    def test_launch_is_hidden_suspended_assigned_then_resumed(self):
        self.initialize()
        process = mock.Mock()
        process.poll.return_value = None
        events = []
        self.api.assign.side_effect = lambda job, proc: events.append(('assign', job, proc))
        self.api.resume.side_effect = lambda proc: events.append(('resume', proc))
        with mock.patch.object(self.owned, 'os', SimpleNamespace(name='nt')), \
                mock.patch.object(subprocess, 'Popen', side_effect=lambda *a, **kw:
                                  events.append(('popen', kw)) or process):
            self.assertIs(self.owned.popen(['test'], own_tree=True, creationflags=0x01000000), process)
        flags = events[0][1]['creationflags']
        self.assertTrue(flags & 0x08000000)
        self.assertTrue(flags & 0x00000004)
        self.assertFalse(flags & 0x01000000)  # no breakaway from our job
        self.assertEqual(events[1:], [('assign', 102, process), ('resume', process)])
        self.owned.terminate(process)
        self.api.close.assert_called_once_with(102)
        process.kill.assert_not_called()  # tree job owns descendants, not just root

    def test_assignment_failure_never_resumes_unowned_child(self):
        self.initialize()
        process = mock.Mock()
        self.api.assign.side_effect = OSError('test assignment failure')
        with mock.patch.object(subprocess, 'Popen', return_value=process), \
                self.assertRaises(OSError):
            self.owned.popen(['test'], own_tree=True)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=1)
        self.api.resume.assert_not_called()
        self.api.close.assert_called_once_with(102)
        self.assertFalse(self.owned._processes)

    def test_initialize_failure_is_not_silently_ignored(self):
        with mock.patch.object(self.owned, 'os', SimpleNamespace(name='nt')), \
                mock.patch.object(self.owned, '_WindowsJobs', side_effect=OSError('test')), \
                self.assertRaises(OSError):
            self.owned.initialize()
        self.assertFalse(self.owned._initialized)

    def test_close_terminal_idempotent_and_external_process_untouched(self):
        self.initialize()
        external = mock.Mock()
        self.owned.terminate(external)
        external.assert_not_called()
        external.kill.assert_not_called()
        external.poll.assert_not_called()
        self.owned.close()
        self.owned.close()
        self.api.close.assert_called_once_with(101)
        with mock.patch.object(subprocess, 'Popen') as popen, \
                mock.patch.object(subprocess, 'run') as run:
            for action in (self.owned.initialize, lambda: self.owned.popen(['test']),
                           lambda: self.owned.run(['test'])):
                with self.assertRaises(self.owned.ClosedError):
                    action()
        popen.assert_not_called()
        run.assert_not_called()

    def test_close_serializes_with_creation_and_captures_late_child(self):
        self.initialize()
        process = mock.Mock()
        process.poll.return_value = None
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def spawn(*args, **kwargs):
            entered.set()
            release.wait(5)
            return process

        def closing():
            try:
                self.owned.close()
            except Exception as exc:
                errors.append(exc)
            closed.set()

        with mock.patch.object(subprocess, 'Popen', side_effect=spawn):
            launch = threading.Thread(target=lambda: self.owned.popen(['test'], own_tree=True))
            close = threading.Thread(target=closing)
            try:
                launch.start()
                self.assertTrue(entered.wait(2))
                close.start()
                self.assertFalse(closed.wait(0.05))
            finally:
                release.set()
                launch.join(3)
                close.join(3)
        self.assertTrue(closed.is_set())
        self.assertFalse(errors)
        self.assertEqual(self.api.close.call_args_list, [mock.call(102), mock.call(101)])
        self.assertFalse(self.owned._processes)

    def test_uninitialized_run_preserves_mocks_and_hides_console(self):
        with mock.patch.object(self.owned, 'os', SimpleNamespace(name='nt')), \
                mock.patch.object(subprocess, 'run', return_value='test-result') as run:
            self.assertEqual(self.owned.run(['test'], capture_output=True, timeout=2), 'test-result')
        self.assertTrue(run.call_args.kwargs['creationflags'] & 0x08000000)

    def test_uninitialized_runs_do_not_serialize_cli_workers(self):
        barrier = threading.Barrier(4)
        errors = []

        def run(*args, **kwargs):
            barrier.wait(timeout=2)
            return 'result'

        def worker():
            try:
                self.owned.run(['test'])
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(subprocess, 'run', side_effect=run):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
        self.assertFalse(errors)

    def test_initialized_run_result_and_timeout(self):
        self.initialize()
        process = mock.MagicMock(args=['test'])
        process.__enter__.return_value = process
        process.poll.return_value = 0
        process.communicate.return_value = (b'output', b'error')
        with mock.patch.object(subprocess, 'Popen', return_value=process):
            result = self.owned.run(['test'], capture_output=True, timeout=2, check=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'output', b'error'))
        process.communicate.assert_called_once_with(None, timeout=2)
        process.communicate.side_effect = [subprocess.TimeoutExpired(['test'], 2), (b'partial', b'err')]
        with mock.patch.object(subprocess, 'Popen', return_value=process), \
                self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.owned.run(['test'], capture_output=True, timeout=2)
        process.kill.assert_called_once_with()
        self.assertEqual(caught.exception.output, b'partial')
        self.assertFalse(self.owned._processes)


class VocalShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / 'start.exe').write_bytes(b'not executable; mocked')
        for name, value in {
            'BASE_DIR': self.base, 'VOCAL_TOOL_DIR': self.base,
            'VOCAL_EXE': self.base / 'start.exe', '_engine_process': None,
            '_setup_thread': None, '_cancel_event': threading.Event(),
            '_engine_lock': threading.Lock(), '_launch_lock': threading.RLock(),
            '_state_lock': threading.RLock(), 'setup_state': dict(vocal.setup_state),
        }.items():
            self.enterContext(mock.patch.object(vocal, name, value))
        self.enterContext(mock.patch.object(vocal, '_refresh_tool'))
        self.enterContext(mock.patch.object(vocal, '_port_in_use', return_value=False))
        self.terminate = self.enterContext(mock.patch.object(vocal.owned_processes, 'terminate'))
        self.errors = []

    def ensure(self):
        try:
            vocal.ensure_running()
        except vocal.InstallerError as exc:
            self.errors.append(exc.code)

    def test_shutdown_does_not_wait_for_engine_lock_and_cancels_waiter(self):
        vocal._engine_lock.acquire()
        waiter = threading.Thread(target=self.ensure)
        try:
            waiter.start()
            start = time.monotonic()
            vocal.shutdown()
            self.assertLess(time.monotonic() - start, 0.25)
            waiter.join(2)
            self.assertFalse(waiter.is_alive())
            self.assertEqual(self.errors, ['cancelled'])
            self.terminate.assert_not_called()
        finally:
            vocal._engine_lock.release()
            waiter.join(2)

    def test_shutdown_during_http_readiness_does_not_wait_and_never_ready(self):
        entered, release = threading.Event(), threading.Event()
        process = mock.Mock()
        process.poll.return_value = None
        calls = 0

        def ready(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return False
            entered.set()
            release.wait(5)
            return True

        with mock.patch.object(vocal, 'running', side_effect=ready), \
                mock.patch.object(vocal, '_start_hidden', return_value=process):
            worker = threading.Thread(target=self.ensure)
            try:
                worker.start()
                self.assertTrue(entered.wait(2))
                start = time.monotonic()
                vocal.shutdown()
                self.assertLess(time.monotonic() - start, 0.25)
                self.terminate.assert_called_once_with(process)
            finally:
                release.set()
                worker.join(3)
        self.assertEqual(self.errors, ['cancelled'])
        self.assertEqual(vocal.setup_status()['error_code'], 'cancelled')

    def test_shutdown_before_late_launch_blocks_start(self):
        entered, release = threading.Event(), threading.Event()

        def port():
            entered.set()
            release.wait(5)
            return False

        with mock.patch.object(vocal, 'running', return_value=False), \
                mock.patch.object(vocal, '_port_in_use', side_effect=port), \
                mock.patch.object(vocal, '_start_hidden') as launch:
            worker = threading.Thread(target=self.ensure)
            try:
                worker.start()
                self.assertTrue(entered.wait(2))
                vocal.shutdown()
            finally:
                release.set()
                worker.join(3)
        launch.assert_not_called()
        self.assertEqual(self.errors, ['cancelled'])

    def test_shutdown_reused_external_service_never_terminates(self):
        with mock.patch.object(vocal, 'running', return_value=True):
            vocal.ensure_running()
        vocal.shutdown()
        vocal.shutdown()
        self.terminate.assert_not_called()
        with mock.patch.object(vocal.owned_processes, 'popen') as popen, \
                mock.patch.object(vocal, '_download_archive') as download:
            for action in (vocal.ensure_running, vocal.start_setup_async, vocal._start_hidden):
                with self.assertRaises(vocal.InstallerError) as caught:
                    action()
                self.assertEqual(caught.exception.code, 'cancelled')
            self.assertIsNone(vocal.auto_start_async())
        popen.assert_not_called()
        download.assert_not_called()


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object integration')
class WindowsIntegrationTests(unittest.TestCase):
    def run_host(self, script):
        result = subprocess.run([sys.executable, '-c', script], cwd=str(MODULE.parent),
                                capture_output=True, timeout=20, creationflags=0x08000000)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return json.loads(result.stdout)

    def test_native_close_kills_grandchildren_but_not_external_process(self):
        result = self.run_host(r'''
import json, subprocess, sys, time, ctypes
import owned_processes as owned
owned.initialize()
external = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'], creationflags=0x08000000)
try:
    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(15)']); print(p.pid,flush=True); time.sleep(15)"
    root = owned.popen([sys.executable, '-c', code], own_tree=True, stdout=subprocess.PIPE, text=True)
    pid = int(root.stdout.readline())
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.OpenProcess(0x00100000, False, pid)
    assert handle
    owned.terminate(root)
    child_stopped = kernel.WaitForSingleObject(handle, 3000) == 0
    kernel.CloseHandle(handle)
    owned.close()
    print(json.dumps({'root': root.poll() is not None, 'child': child_stopped, 'external_alive': external.poll() is None}))
finally:
    external.kill()
    external.wait()
''')
        self.assertEqual(result, {'root': True, 'child': True, 'external_alive': True})

    def test_native_host_exit_closes_inherited_job_handles(self):
        # An outer observer keeps a real process handle open across host exit;
        # this avoids guessing whether a PID has been reused.
        result = self.run_host(r'''
import json, subprocess, sys, ctypes
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
kernel.OpenProcess.restype = ctypes.c_void_p
kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
kernel.WaitForSingleObject.restype = ctypes.c_ulong
kernel.CloseHandle.argtypes = [ctypes.c_void_p]
kernel.CloseHandle.restype = ctypes.c_int
code = "import owned_processes as o,sys; o.initialize(); p=o.popen([sys.executable,'-c','import time; time.sleep(20)']); print(p.pid,flush=True); sys.stdin.readline()"
host = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, creationflags=0x08000000)
pid = int(host.stdout.readline())
handle = kernel.OpenProcess(0x00100000, False, pid)
assert handle
host.stdin.write(chr(10))
host.stdin.close()
host.wait(timeout=5)
stopped = kernel.WaitForSingleObject(handle, 3000) == 0
kernel.CloseHandle(handle)
print(json.dumps({'host': host.returncode == 0, 'child': stopped}))
''')
        self.assertEqual(result, {'host': True, 'child': True})

    def test_native_run_and_close_guard(self):
        result = self.run_host(r'''
import json, sys
import owned_processes as owned
owned.initialize()
r = owned.run([sys.executable, '-c', "print('hello')"], capture_output=True, timeout=3, check=True)
owned.close()
blocked = False
try:
    owned.popen([sys.executable, '-c', 'pass'])
except owned.ClosedError:
    blocked = True
print(json.dumps({'stdout': r.stdout.decode().strip(), 'blocked': blocked}))
''')
        self.assertEqual(result, {'stdout': 'hello', 'blocked': True})


if __name__ == '__main__':
    unittest.main()
