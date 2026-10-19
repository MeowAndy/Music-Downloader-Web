"""Desktop-owned subprocesses; call initialize() before launching children.

Windows launches are suspended until assigned to a private KILL_ON_JOB_CLOSE
job, so even a launcher which immediately forks cannot escape cleanup. The host
is NEVER assigned to a job. close() is terminal and serialized with Popen.
Before explicit desktop initialization, wrappers retain ordinary subprocess
behavior (plus hiding/tracking); callers needing tree cleanup must initialize.
"""
import ctypes
from ctypes import wintypes
import os
import subprocess
import threading

_lock = threading.RLock()
_initialized = False
_closed = False
_job = None
_api = None
_processes = set()
_tree_jobs = {}


class ClosedError(RuntimeError):
    pass


class _BasicLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _BasicLimits),
                ('IoInfo', _IOCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t)]


class _WindowsJobs:
    def __init__(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.ntdll = ctypes.WinDLL('ntdll')
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        # Popen closes the primary thread handle; resume by the owned process
        # HANDLE rather than reopening a thread/PID which might have been reused.
        self.ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self.ntdll.NtResumeProcess.restype = wintypes.LONG

    def create(self):
        handle = self.kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits),
                                                   ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.kernel.CloseHandle(handle)
            raise ctypes.WinError(error)
        return handle

    def assign(self, job, process):
        if not self.kernel.AssignProcessToJobObject(job, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def resume(self, process):
        status = self.ntdll.NtResumeProcess(int(process._handle))
        if status < 0:
            raise OSError('Could not resume owned subprocess (NTSTATUS %08x)' % (status & 0xffffffff))

    def close(self, handle):
        if not self.kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def initialize():
    """Initialize once before desktop imports/launches; fail closed on job errors.

    Does not adopt existing services/processes. A closed lifecycle cannot reopen.
    Non-Windows callers get guarded direct-child cleanup, not Job Objects.
    """
    global _api, _job, _initialized
    with _lock:
        if _closed:
            raise ClosedError('Owned subprocess lifecycle is closed')
        if _initialized:
            return
        if os.name == 'nt':
            api = _WindowsJobs()
            job = api.create()
            _api, _job = api, job
        _initialized = True


def hidden_kwargs():
    """Fresh keyword arguments for console-free launches on Windows."""
    return {'creationflags': 0x08000000 if os.name == 'nt' else 0}


def popen(*args, own_tree=False, **kwargs):
    """Launch under the close guard; own_tree enables individual tree shutdown."""
    with _lock:
        if _closed:
            raise ClosedError('Owned subprocess lifecycle is closed')
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | hidden_kwargs()['creationflags']
        job = _api.create() if _api is not None and own_tree else _job
        if _api is not None:
            # Do not allow caller flags to opt out of ownership.
            kwargs['creationflags'] = (kwargs['creationflags'] & ~0x01000000) | 0x00000004
        process = None
        try:
            process = subprocess.Popen(*args, **kwargs)
            if _api is not None:
                _api.assign(job, process)
                _api.resume(process)
            # Reap finished direct children; keep private tree jobs until shutdown
            # even if the root launcher has already exited.
            _processes.difference_update(p for p in tuple(_processes) if p.poll() is not None)
            _processes.add(process)
            if own_tree and job is not None:
                _tree_jobs[process] = job
            return process
        except BaseException:
            if process is not None:
                try:
                    process.kill()
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if own_tree and job is not None:
                _api.close(job)
            raise


def terminate(process):
    """Stop ONLY a recorded process/tree, never discover/adopt by PID/name/port."""
    with _lock:
        job = _tree_jobs.pop(process, None)
        owned = process in _processes
        _processes.discard(process)
        if job is not None:
            _api.close(job)
        elif owned and process.poll() is None:
            process.kill()
        if owned or job is not None:
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass


def close():
    """Prevent all future launches and kill owned descendants; idempotent."""
    global _closed, _job
    with _lock:
        if _closed:
            return
        _closed = True
        errors = []
        for process in tuple(_tree_jobs):
            try:
                terminate(process)
            except OSError as exc:
                errors.append(exc)
        if _job is not None:
            job, _job = _job, None
            try:
                _api.close(job)
            except OSError as exc:
                errors.append(exc)
        for process in tuple(_processes):
            try:
                terminate(process)
            except OSError as exc:
                errors.append(exc)
        if errors:
            raise errors[0]


def run(*popenargs, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """subprocess.run-compatible bounded execution through the owned launcher.

    Before desktop initialization, keep ordinary run semantics for CLI callers
    and existing test mocks. Pre-initialization calls are not lifecycle-owned;
    desktop callers MUST initialize first for the atomic launch/close guarantee.
    """
    with _lock:
        if _closed:
            raise ClosedError('Owned subprocess lifecycle is closed')
        initialized = _initialized
    if not initialized:
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | hidden_kwargs()['creationflags']
        return subprocess.run(*popenargs, input=input, capture_output=capture_output,
                              timeout=timeout, check=check, **kwargs)
    if input is not None:
        if kwargs.get('stdin') is not None:
            raise ValueError('stdin and input arguments may not both be used')
        kwargs['stdin'] = subprocess.PIPE
    if capture_output:
        if kwargs.get('stdout') is not None or kwargs.get('stderr') is not None:
            raise ValueError('stdout and stderr arguments may not be used with capture_output')
        kwargs['stdout'] = kwargs['stderr'] = subprocess.PIPE
    with popen(*popenargs, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            exc.stdout, exc.stderr = process.communicate()
            raise
        except BaseException:
            process.kill()
            raise
        finally:
            with _lock:
                _processes.discard(process)
        returncode = process.poll()
        if check and returncode:
            raise subprocess.CalledProcessError(returncode, process.args, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(process.args, returncode, stdout, stderr)
