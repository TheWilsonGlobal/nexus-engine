"""Sandbox executor: run untrusted Python in an ephemeral, resource-capped child process.

READ THIS BEFORE TRUSTING IT. The honest security model, layer by layer (details and the residual
risks are in docs/sandbox.md):

0. Policy (``policy.py``, ``tools.py``): off by default, admin only, and only code the user typed.
1. Static gate (``static_gate.py``): AST allowlist/denylist, run before any process exists.
   Defence in depth. It is not the isolation.
2. OS-enforced, outside the child's control:
     * wall-clock timeout, whole tree killed;
     * memory cap and CPU-time cap;
     * process-creation cap: Windows Job Object ``ActiveProcessLimit = 1`` / POSIX ``RLIMIT_NPROC = 0``
       (this is what stops a fork bomb);
     * Windows only: the child's token is lowered to LOW INTEGRITY before it runs, so the kernel
       rejects its writes to anything outside the sandbox directory and its attempts to open the
       server process for termination;
     * fail closed: if a limit cannot be applied the code is not run.
3. In-child audit hook (``child_runner.py``): denies filesystem access outside the sandbox
   directory (writes) and the interpreter library (reads), network sockets, process creation,
   native-code loading. It runs INSIDE the process it polices: it is not a kernel boundary and a
   determined attacker with a Python or native-code bug can defeat it.
4. Scrubbed environment, fresh temp working directory, ``python -I -S``.

NOT enforced by the OS here: read isolation of the host filesystem (Windows integrity levels
restrict writes, not reads), and network egress (no firewall/namespace on Windows; Linux tries an
empty network namespace and reports it only if it worked). For hostile code run the whole thing in a
container / AppContainer / microVM; that is the recommended hardening.

Status is derived from the exit code and from structured events the runner sends over an inherited
control pipe, never from stderr text. The pipe is not a trust boundary: hostile code could forge
events on it, which can only change its own report.
"""
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Union

from nexus_engine.services.sandbox.static_gate import check_code

Status = Literal["ok", "error", "timeout", "blocked", "memory_exceeded"]
NetworkGuard = Literal["best_effort", "none"]

IS_WINDOWS = os.name == "nt"
_NOMEM_EXIT = (0xC0000017, 0xC00000FD, -1073741801)
_FATAL_EXIT = 98
_JOB_QUOTA_EXIT = 1816  # ERROR_NOT_ENOUGH_QUOTA: a Job Object CPU-time limit terminated the process
_RUNNER_PATH = Path(__file__).with_name("child_runner.py")
_MAX_FILE_BYTES = 16 * 1024 * 1024

LimitsEnforced = dict[str, Union[bool, str]]


def scan_code(code: str) -> Optional[str]:
    """Backwards-compatible name for the static gate: the refusal reason, or None when the code passes."""
    return check_code(code)


def _no_limits() -> LimitsEnforced:
    return {"timeout": False, "memory": False, "process_limit": False, "network_guard": "none"}


def sandbox_enabled_from_env() -> bool:
    return os.environ.get("SANDBOX_ENABLED", "").strip().lower() in ("1", "true", "yes")


@dataclass
class SandboxResult:
    status: Status
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    duration_ms: float = 0.0
    limits_enforced: LimitsEnforced = field(default_factory=_no_limits)
    # What else was in force for this run (filesystem guard, Windows low integrity, ...). Informational.
    isolation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _interpreter() -> str:
    """A venv's python.exe on Windows is a launcher that spawns the real interpreter as a
    child, which an ActiveProcessLimit of 1 would kill; use the base interpreter there."""
    if IS_WINDOWS and sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return getattr(sys, "_base_executable", sys.executable)
    return sys.executable


def _close_fd(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


class SandboxExecutor:
    def __init__(
        self,
        timeout_s: float = 5.0,
        memory_mb: int = 256,
        max_output_bytes: int = 64 * 1024,
        max_code_bytes: int = 32 * 1024,
        max_concurrent: int = 2,
        queue_wait_s: Optional[float] = None,
        enabled: Optional[bool] = None,
        _disable_python_guards: bool = False,
        _static_gate: bool = True,
    ) -> None:
        self.timeout_s = timeout_s
        self.memory_mb = memory_mb
        self.max_output_bytes = max_output_bytes
        self.max_code_bytes = max_code_bytes
        # Policy flag read by the tool layer (see policy.refusal_reason). Defaults to SANDBOX_ENABLED,
        # which defaults to off. ``run`` itself is a mechanism and does not consult it.
        self.enabled = sandbox_enabled_from_env() if enabled is None else enabled
        # Test-only switches: turn one layer off so a test can prove the layers below it hold on
        # their own. Never set outside tests.
        self._disable_python_guards = _disable_python_guards
        self._static_gate = _static_gate
        self.queue_wait_s = timeout_s if queue_wait_s is None else queue_wait_s
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self._caps: Optional[LimitsEnforced] = None

    # What *would* apply on this OS (used by the API for the idle state).
    def capabilities(self) -> LimitsEnforced:
        if self._caps is None:
            self._caps = self._probe()
        return dict(self._caps)

    def _probe(self) -> LimitsEnforced:
        mem = proc = False
        if IS_WINDOWS:
            try:
                from nexus_engine.services.sandbox.win_isolation import WinJob

                WinJob(self.memory_mb * 1024 * 1024, self.timeout_s + 1).close()
                mem = proc = True
            except OSError:
                pass
        else:
            try:
                import resource  # noqa: F401

                mem = proc = True
            except ImportError:
                pass
        return {
            "timeout": True,
            "memory": mem,
            "process_limit": proc,
            "network_guard": "none" if self._disable_python_guards else "best_effort",
        }

    # ------------------------------------------------------------------
    def run(self, code: str) -> SandboxResult:
        t0 = time.perf_counter()

        def done(res: SandboxResult) -> SandboxResult:
            res.duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            return res

        if len(code.encode("utf-8")) > self.max_code_bytes:
            return done(SandboxResult("error", stderr="code exceeds size limit"))
        if self._static_gate:
            reason = check_code(code)
            if reason:
                return done(SandboxResult("blocked", stderr=f"static gate rejected the code: {reason}"))
        if not self._sem.acquire(timeout=self.queue_wait_s):
            return done(SandboxResult("error", stderr="sandbox busy: too many concurrent runs"))
        try:
            return done(self._run_locked(code))
        finally:
            self._sem.release()

    def _run_locked(self, code: str) -> SandboxResult:
        with tempfile.TemporaryDirectory(prefix="cogni-sbx-", ignore_cleanup_errors=True) as tmp:
            return self._run_in(Path(tmp), code)

    @staticmethod
    def _refuse(message: str) -> SandboxResult:
        """Fail closed: nothing ran, so nothing may be claimed as enforced."""
        return SandboxResult("error", stderr=f"sandbox refused to run: {message}")

    def _run_in(self, tmp: Path, code: str) -> SandboxResult:
        mem_bytes = self.memory_mb * 1024 * 1024
        cpu_seconds = max(1, math.ceil(self.timeout_s) + 1)
        ctl = tmp / "ctl"  # the runner lives outside the child's working directory
        work = tmp / "work"
        ctl.mkdir()
        work.mkdir()
        if IS_WINDOWS:
            from nexus_engine.services.sandbox import win_isolation as win

            try:
                win.label_directory_low(str(work))  # before any file exists, so children inherit the label
            except OSError as exc:
                return self._refuse(f"could not label the working directory ({exc})")
        runner = ctl / "runner.py"
        runner.write_text(_RUNNER_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        (work / "main.py").write_text(code, encoding="utf-8")

        env = {"TMP": str(work), "TEMP": str(work), "TMPDIR": str(work)}
        if IS_WINDOWS:
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        cfg: dict[str, Any] = {
            "memory_bytes": mem_bytes,
            "cpu_seconds": cpu_seconds,
            "fsize_bytes": _MAX_FILE_BYTES,
            "guards": not self._disable_python_guards,
        }
        cmd = [_interpreter(), "-I", "-S", "-X", "utf8", str(runner), "main.py"]

        ctl_r, ctl_w = os.pipe()
        ctl_file: Any = None
        job: Any = None
        proc: Optional[subprocess.Popen[bytes]] = None
        os_enforced = False
        low_integrity = False
        threads: list[threading.Thread] = []
        bufs: dict[str, bytearray] = {"out": bytearray(), "err": bytearray(), "ctl": bytearray()}
        flags = {"overflow": False, "truncated": False}
        hard_cap = self.max_output_bytes * 8
        timed_out = False

        def kill_tree() -> None:
            if proc is None:
                return
            try:
                if IS_WINDOWS:
                    if job is not None and os_enforced:
                        job.terminate()
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True, check=False, timeout=5,
                    )
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass

        def pump(read: Any, key: str, cap: int, kill_on_flood: bool) -> None:
            total = 0
            while True:
                chunk = read(4096)
                if not chunk:
                    return
                total += len(chunk)
                room = cap - len(bufs[key])
                if room > 0:
                    bufs[key].extend(chunk[:room])
                if kill_on_flood:
                    if total > cap:
                        flags["truncated"] = True
                    if total > hard_cap and not flags["overflow"]:
                        flags["overflow"] = True
                        kill_tree()

        try:
            kwargs: dict[str, Any] = {}
            if IS_WINDOWS:
                import msvcrt

                os.set_inheritable(ctl_w, True)
                handle = msvcrt.get_osfhandle(ctl_w)
                cfg["ctl"] = str(handle)
                si = subprocess.STARTUPINFO()
                si.lpAttributeList = {"handle_list": [handle]}
                kwargs["startupinfo"] = si
                try:
                    job = win.WinJob(mem_bytes, cpu_seconds)
                except OSError as exc:
                    return self._refuse(f"could not create the Job Object ({exc})")
                kwargs["creationflags"] = 0x00000004 | 0x08000000  # CREATE_SUSPENDED | CREATE_NO_WINDOW
            else:
                cfg["ctl"] = str(ctl_w)
                kwargs["pass_fds"] = (ctl_w,)
                kwargs["start_new_session"] = True
            env["SANDBOX_CFG"] = json.dumps(cfg)

            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(work), env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, **kwargs,
                )
            except OSError as exc:
                return self._refuse(f"could not start the interpreter ({exc})")
            _close_fd(ctl_w)  # the child holds its own copy now
            ctl_w = None  # type: ignore[assignment]

            if IS_WINDOWS:
                try:
                    if not job.assign(proc._handle):  # type: ignore[attr-defined]
                        raise OSError(0, "AssignProcessToJobObject failed")
                    os_enforced = True
                    win.lower_to_low_integrity(proc._handle)  # type: ignore[attr-defined]
                    low_integrity = True
                    win.resume_process(proc._handle)  # type: ignore[attr-defined]
                except OSError as exc:
                    kill_tree()
                    proc.wait()
                    return self._refuse(f"could not apply the OS limits ({exc})")

            ctl_file = os.fdopen(ctl_r, "rb", buffering=0)
            ctl_r = None  # type: ignore[assignment]
            assert proc.stdout is not None and proc.stderr is not None
            threads = [
                threading.Thread(target=pump, args=(proc.stdout.read, "out", self.max_output_bytes, True), daemon=True),
                threading.Thread(target=pump, args=(proc.stderr.read, "err", self.max_output_bytes, True), daemon=True),
                threading.Thread(target=pump, args=(ctl_file.read, "ctl", 16 * 1024, False), daemon=True),
            ]
            for th in threads:
                th.start()

            try:
                proc.wait(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_tree()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            for th in threads:
                th.join(timeout=2)
        finally:
            if proc is not None and proc.poll() is None:
                kill_tree()
            _close_fd(ctl_w)
            if ctl_file is not None:
                ctl_file.close()
            else:
                _close_fd(ctl_r)
            if proc is not None:
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            if job is not None:
                job.close()

        return self._interpret(
            proc, bufs, flags, timed_out, os_enforced, low_integrity,
        )

    def _interpret(
        self,
        proc: subprocess.Popen[bytes],
        bufs: dict[str, bytearray],
        flags: dict[str, bool],
        timed_out: bool,
        os_enforced: bool,
        low_integrity: bool,
    ) -> SandboxResult:
        events: list[dict[str, Any]] = []
        for line in bytes(bufs["ctl"]).decode("utf-8", "replace").splitlines():
            try:
                ev = json.loads(line)
                if isinstance(ev, dict):
                    events.append(ev)
            except ValueError:
                pass
        ready = next((e for e in events if e.get("event") == "ready"), {})
        fatal = next((e for e in events if e.get("event") == "fatal"), None)
        blocked = any(e.get("event") == "blocked" for e in events)
        mem_err = any(e.get("event") == "memory_error" for e in events)

        stdout = bytes(bufs["out"]).decode("utf-8", "replace")
        stderr = bytes(bufs["err"]).decode("utf-8", "replace")
        if flags["truncated"]:
            stdout += "\n[output truncated]"
        code_ = proc.returncode
        limits: LimitsEnforced = {
            "timeout": True,
            "memory": bool(os_enforced or ready.get("memory")),
            "process_limit": bool(os_enforced or ready.get("process_limit")),
            "network_guard": "best_effort" if ready.get("network_guard") else "none",
        }
        isolation: dict[str, Any] = {
            "filesystem_guard": "audit_hook" if ready.get("filesystem_guard") else "none",
            "windows_low_integrity": low_integrity,
            "network_namespace": bool(ready.get("network_namespace")),
        }

        status: Status
        if fatal is not None or code_ == _FATAL_EXIT:
            reason = (fatal or {}).get("reason", "runner failed to start")
            return SandboxResult("error", "", f"sandbox refused to run: {reason}", code_, 0.0, _no_limits(), {})
        if not ready and not timed_out:
            # Fail closed: the runner never confirmed its guards, so do not trust (or return) the run.
            return SandboxResult(
                "error", "", (stderr.strip() + "\nsandbox runner did not confirm isolation").strip(),
                code_, 0.0, _no_limits(), {},
            )
        if timed_out:
            status = "timeout"
            stderr = (stderr + f"\nexecution exceeded {self.timeout_s}s and was killed").strip()
        elif blocked:
            status = "blocked"
        elif mem_err or code_ in _NOMEM_EXIT:
            status = "memory_exceeded"
        elif flags["overflow"]:
            status = "error"
            stderr = (stderr + "\noutput flood: process killed").strip()
        elif (not IS_WINDOWS and code_ == -int(signal.SIGXCPU)) or (IS_WINDOWS and code_ == _JOB_QUOTA_EXIT):
            status = "timeout"
            stderr = (stderr + "\nCPU-time limit exceeded").strip()
        elif code_ != 0:
            status = "error"
        else:
            status = "ok"
        return SandboxResult(status, stdout, stderr, code_, 0.0, limits, isolation)
