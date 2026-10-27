"""Windows-only OS-level confinement primitives used by the sandbox executor (ctypes, no extra deps).

* ``WinJob``: a Job Object with a per-process and job memory cap, ``ActiveProcessLimit = 1`` (the
  sandboxed interpreter may not create a second process, which is what stops a fork bomb),
  a CPU-time cap, UI restrictions (no clipboard, no desktop/display changes, no foreign USER
  handles) and ``KILL_ON_JOB_CLOSE``.
* ``lower_to_low_integrity``: drops the (still suspended) child's primary token to the Low
  mandatory integrity level. Windows then refuses any WRITE from the child to a Medium-integrity
  object (the repo, ``data/db``, the user profile ...) and refuses to open the server process for
  termination. The sandbox working directory is labelled Low (``label_directory_low``) so the child
  can still write there. Reads are NOT restricted by integrity levels: that is why the read side
  relies on the in-child audit hook and why docs/sandbox.md recommends a container/AppContainer.

Every function raises ``OSError`` on failure; the executor treats any failure as "refuse to run".
"""
from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes

LOW_INTEGRITY_SID = "S-1-16-4096"
LOW_INTEGRITY_RID = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_ADJUST_DEFAULT = 0x0080
_TOKEN_INTEGRITY_LEVEL = 25
_SE_GROUP_INTEGRITY = 0x00000020

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)

_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.LocalFree.argtypes = [ctypes.c_void_p]
_k32.LocalFree.restype = ctypes.c_void_p
_adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
_adv.GetLengthSid.argtypes = [ctypes.c_void_p]
_adv.GetLengthSid.restype = wintypes.DWORD
_adv.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_adv.GetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
]
_adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
_adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
_adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)


def _last_error(what: str) -> OSError:
    return OSError(ctypes.get_last_error(), what)


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _Basic(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _Extended(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _Basic),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _UiRestrictions(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]


class WinJob:
    """Job Object wrapper. Construction applies every limit or raises OSError (fail closed)."""

    LIMIT_PROCESS_TIME = 0x00000002
    LIMIT_ACTIVE_PROCESS = 0x00000008
    LIMIT_PROCESS_MEMORY = 0x00000100
    LIMIT_JOB_MEMORY = 0x00000200
    LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    EXTENDED_LIMIT_INFORMATION = 9
    BASIC_UI_RESTRICTIONS = 4
    UI_ALL = 0x000000FF  # desktop, display settings, exit windows, global atoms, USER handles, clipboard r/w, sys params

    def __init__(self, memory_bytes: int, cpu_seconds: float) -> None:
        self._h = _k32.CreateJobObjectW(None, None)
        if not self._h:
            raise _last_error("CreateJobObjectW failed")
        try:
            info = _Extended()
            info.BasicLimitInformation.LimitFlags = (
                self.LIMIT_ACTIVE_PROCESS | self.LIMIT_PROCESS_MEMORY | self.LIMIT_JOB_MEMORY
                | self.LIMIT_KILL_ON_JOB_CLOSE | self.LIMIT_PROCESS_TIME
            )
            info.BasicLimitInformation.ActiveProcessLimit = 1
            info.BasicLimitInformation.PerProcessUserTimeLimit = int(cpu_seconds * 10_000_000)  # 100 ns units
            info.ProcessMemoryLimit = memory_bytes
            info.JobMemoryLimit = memory_bytes
            if not _k32.SetInformationJobObject(
                self._h, self.EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise _last_error("SetInformationJobObject(limits) failed")
            ui = _UiRestrictions(self.UI_ALL)
            if not _k32.SetInformationJobObject(self._h, self.BASIC_UI_RESTRICTIONS, ctypes.byref(ui), ctypes.sizeof(ui)):
                raise _last_error("SetInformationJobObject(ui) failed")
        except BaseException:
            self.close()
            raise

    def assign(self, process_handle: int) -> bool:
        return bool(_k32.AssignProcessToJobObject(self._h, int(process_handle)))

    def terminate(self) -> None:
        if self._h:
            _k32.TerminateJobObject(self._h, 1)

    def close(self) -> None:
        if self._h:
            _k32.CloseHandle(self._h)
            self._h = None


def _open_token(process_handle: int, access: int) -> wintypes.HANDLE:
    token = wintypes.HANDLE()
    if not _adv.OpenProcessToken(int(process_handle), access, ctypes.byref(token)):
        raise _last_error("OpenProcessToken failed")
    return token


def lower_to_low_integrity(process_handle: int) -> None:
    """Set the Low mandatory integrity level on ``process_handle``'s primary token and verify it."""
    sid = ctypes.c_void_p()
    if not _adv.ConvertStringSidToSidW(LOW_INTEGRITY_SID, ctypes.byref(sid)):
        raise _last_error("ConvertStringSidToSidW failed")
    try:
        token = _open_token(process_handle, _TOKEN_ADJUST_DEFAULT | _TOKEN_QUERY)
        try:
            class _Label(ctypes.Structure):
                _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

            label = _Label(sid.value, _SE_GROUP_INTEGRITY)
            size = ctypes.sizeof(label) + _adv.GetLengthSid(sid)
            if not _adv.SetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, ctypes.byref(label), size):
                raise _last_error("SetTokenInformation(TokenIntegrityLevel) failed")
            if _query_integrity_rid(token) != LOW_INTEGRITY_RID:
                raise OSError(0, "child token is not at Low integrity after SetTokenInformation")
        finally:
            _k32.CloseHandle(token)
    finally:
        _k32.LocalFree(sid)


def _query_integrity_rid(token: wintypes.HANDLE) -> int:
    needed = wintypes.DWORD(0)
    _adv.GetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(needed))
    buf = ctypes.create_string_buffer(needed.value)
    if not _adv.GetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, buf, needed, ctypes.byref(needed)):
        raise _last_error("GetTokenInformation(TokenIntegrityLevel) failed")
    sid = ctypes.c_void_p.from_buffer(buf).value
    count = _adv.GetSidSubAuthorityCount(sid).contents.value
    return int(_adv.GetSidSubAuthority(sid, count - 1).contents.value)


def process_integrity_rid(process_handle: int) -> int:
    """Mandatory integrity RID of a process (0x1000 Low, 0x2000 Medium, 0x3000 High)."""
    token = _open_token(process_handle, _TOKEN_QUERY)
    try:
        return _query_integrity_rid(token)
    finally:
        _k32.CloseHandle(token)


def label_directory_low(path: str) -> None:
    """Give ``path`` (and everything created under it) the Low integrity label so a Low child can write there."""
    proc = subprocess.run(
        ["icacls", path, "/setintegritylevel", "(OI)(CI)L"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if proc.returncode != 0:
        raise OSError(proc.returncode, "icacls /setintegritylevel failed")


def resume_process(handle: int) -> None:
    nt = ctypes.WinDLL("ntdll")
    nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
    status = nt.NtResumeProcess(int(handle))
    if status != 0:
        raise OSError(status, "NtResumeProcess failed")
