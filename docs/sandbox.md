# Execution Sandbox: Architecture & Security Model

The `execute_sandbox_code` tool runs short Python snippets (calculations, small data transforms) that a user typed into their own message.

> [!IMPORTANT]
> **The sandbox is disabled by default** (`SANDBOX_ENABLED=false`). When enabled it is **admin-only** and runs **only code that is literally part of the admin's own message**. It is a layered mitigation, **not** a hostile-code boundary: see [What it does not guarantee](#what-it-does-not-guarantee). For hostile or multi-tenant code, run the workers inside a container / AppContainer / microVM; that is the recommended hardening.

---

## 1. Layers, outermost first

| # | Layer | Where | Nature |
| :-- | :--- | :--- | :--- |
| 0 | **Policy**: enabled flag, admin role, code-from-user check | `sandbox/policy.py`, `agent/tools.py`, `api/v1/agent.py` | Code-level, evaluated on every call |
| 1 | **Static gate**: AST allowlist/denylist | `sandbox/static_gate.py` | Defence in depth only; not the isolation |
| 2 | **OS limits** applied by the parent before user code runs | `sandbox/executor.py`, `sandbox/win_isolation.py` | Enforced by the kernel; fail closed |
| 3 | **In-child guards**: audit hook (filesystem, network, process, import) + socket patch | `sandbox/child_runner.py` | Soft: runs inside the process it polices |
| 4 | Scrubbed environment, fresh temp cwd, `python -I -S -X utf8` | `executor.py` | Hygiene |

### 0. Policy

1. `SANDBOX_ENABLED` (default `false`). While off, the tool returns `sandbox disabled: ...` and never reaches the executor; the tool is also not advertised to the LLM.
2. The caller must hold the **`admin`** role. This is a plain role check, independent of the flag, so enabling the sandbox never widens access. The identity is bound per request by `api/v1/agent.py` (`bind_principal`). No bound identity (a code path that forgot to authenticate) means refusal.
3. The code must come from **the user's own turn**: it must equal what the planner extracts from the message (a fenced block, `run: ...`, or `calculate ...`) or be a contiguous part of the message. Code chosen by the LLM from a fetched web page, a retrieved passage or any other tool output is refused (SEC-05, indirect prompt injection).

### 1. Static gate (`check_code`)

Rejects before any process exists: imports outside a small pure-computation allowlist (`math`, `json`, `re`, `decimal`, `statistics`, `itertools`, ...), so `os`, `sys`, `subprocess`, `socket`, `ctypes`, `importlib`, `pathlib`, `shutil` ... are all refused; the names `open`, `eval`, `exec`, `compile`, `__import__`, `getattr`, `globals`, ...; any dunder attribute access (`x.__class__`); frame/generator introspection attributes; string constants containing `__` (which would reach attributes through `str.format`); and code that does not parse. `importlib.import_module('o' + 's')` fails because neither `importlib` nor `__import__` is available.

Python cannot be sandboxed by reading its source. Treat this layer as a filter for the obvious and the lazy, nothing more.

### 2. OS limits (kernel-enforced, outside the child's control)

| Control | Windows | POSIX (implemented, **not exercised on the Windows dev host**) |
| :--- | :--- | :--- |
| Wall-clock timeout (`SANDBOX_TIMEOUT_S`) | `wait(timeout)`, Job Object terminate + `taskkill /T` | `wait(timeout)`, `killpg(SIGKILL)` |
| Memory (`SANDBOX_MEMORY_MB`) | Job Object `ProcessMemoryLimit` + `JobMemoryLimit` | `RLIMIT_AS` |
| CPU time | Job Object per-process user-time limit (timeout + 1 s) | `RLIMIT_CPU` |
| **Process creation / fork bomb** | Job Object `ActiveProcessLimit = 1` | `RLIMIT_NPROC = 0`, new session |
| Output flood | Truncated at 64 KiB, killed beyond 8x | same |
| File size | not limited (see residual risks) | `RLIMIT_FSIZE` 16 MiB |
| Open files / core dumps | n/a | `RLIMIT_NOFILE = 64`, `RLIMIT_CORE = 0` |
| UI | Job Object UI restrictions (no clipboard, desktop, display, global atoms, foreign USER handles) | n/a |
| **Filesystem writes** | **Low integrity level**: the child's token is lowered to Low before it is resumed, and its working directory is labelled Low. The kernel then refuses writes to every Medium-integrity object (the repo, `data/db`, the user profile). Verified with every Python guard off (`test_windows_kernel_denies_writes_even_with_every_python_guard_off`). It also denies memory writes, remote threads and suspend on the server process. | in-child audit hook only |
| Network | none at OS level on Windows. Linux: best-effort `unshare(CLONE_NEWUSER\|CLONE_NEWNET)` in the child, reported only if it worked | same |

**Fail closed.** If the Job Object cannot be created or assigned, the token cannot be lowered, the working directory cannot be labelled, the interpreter cannot start, or (POSIX) any rlimit cannot be applied, the run is refused with `sandbox refused to run: ...` and the user's code is never executed. If the runner does not confirm its guards over the control pipe, the result is discarded as an error. On Windows the child is created suspended and resumed only after the limits are in force. All handles (pipes, Job Object, process streams) are released in `finally` blocks (REV-31); `test_repeated_runs_and_failures_do_not_leak_handles` checks this.

### 3. In-child audit hook (`sys.addaudithook`)

Installed by the runner before user code, with `-I -S` (no site-packages, no user site, no `PYTHON*` variables):

* **Filesystem**: `open` is allowed inside the sandbox working directory (read/write) and read-only inside the interpreter's own library directories (taken from `sys.path`, site-packages excluded). `os.listdir/scandir/mkdir/remove/rename/...`, `shutil.*` and `glob` are checked against the working directory. Everything else raises `PermissionError`: the repo, `.env`, `data/db`, `C:\`, `/etc` are unreadable and unlistable, and paths are resolved with `realpath` first (`..`, symlinks, `\\?\`, 8.3 names).
* **Network**: `socket.*` creation/connect/bind/DNS events are denied, and the `socket` module's connect/DNS functions are patched. Reported as `network_guard: "best_effort"`, never as a boolean.
* **Processes and native code**: `subprocess.Popen`, `os.system/exec*/spawn*/fork*/kill`, `ctypes.*`, `_winapi.*`, `msvcrt.*`, `winreg.*`, symlink/link creation, `sqlite3`, `gc` introspection, `sys.settrace/setprofile/_current_frames`; the `import` event refuses `ctypes`, `subprocess`, `multiprocessing`, `gc`, `asyncio`, `urllib`, `http`, ... and refuses to load any extension module from outside the interpreter library (so writing a `.pyd`/`.so` into the work dir and importing it fails).
* The hook's state is immutable and closed over, the runner's globals hold nothing reusable, and a runner frame never reaches user code; this removes the easy routes to switching the hook off.

### 4. Environment

The child gets an empty environment (plus `TMP`/`TEMP`/`TMPDIR` pointing at its work dir and, on Windows, `SYSTEMROOT`): no API keys or any other server secret are inherited. The runner lives in a sibling directory the child cannot read.

---

## What it does not guarantee

Be explicit about these when deciding whether to enable it:

1. **The audit hook is not a kernel boundary.** It runs in the same process as the code it polices. A bug in CPython or a native extension, or any route that does not raise an audit event, can bypass it. `os.stat`, `os.path.exists` and similar calls raise no audit event, so the *existence* and size of host paths can be probed.
2. **Windows read isolation is the audit hook only.** Integrity levels restrict writes, not reads. If the hook is bypassed, a Low-integrity process can still read files that Medium-integrity users can read (including `.env` and the SQLite files).
3. **Windows has no OS-level network block.** Only the Python-level patch and audit hook stop sockets. Windows Firewall rules or an AppContainer without the `internetClient` capability would; neither is implemented.
4. **Windows low integrity does not stop `PROCESS_TERMINATE`.** Measured on this host: a Low-integrity child could open the Medium-integrity server process with `PROCESS_TERMINATE` (but not with write, thread or suspend rights). The audit hook denies `os.kill` and the `_winapi` import, and the static gate denies both, but a hook bypass could terminate the server.
5. **Disk quota (Windows).** There is no per-file size limit; a runaway writer is bounded only by the timeout and the free disk space.
6. **POSIX paths are implemented but were not run** on this Windows development host (no Linux available). The `resource.setrlimit` calls, `start_new_session`, `killpg` and the `unshare` attempt are untested here; treat POSIX as unverified until the Stage 2 tests have run on Linux CI.
7. **The control pipe is not a trust boundary.** Hostile code could write forged events to it, which can only change its own result report.

### Recommended hardening for hostile code

Run the worker (or just the sandbox child) in a container with `--network none --read-only --cap-drop ALL --pids-limit 64 --memory 256m --cpus 0.5` and a tmpfs working directory, under a separate unprivileged user, or in gVisor/Firecracker. On Windows use an AppContainer (no capabilities) with a firewall rule, or a Hyper-V isolated container. Keep `.env` and the database files outside any path the child's user can read. A restricted token with restricting SIDs (read isolation) and AppContainer are **not implemented** here.

---

## Configuration

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `SANDBOX_ENABLED` | `false` | Master switch. Even when `true`, only `admin` can run code. |
| `SANDBOX_TIMEOUT_S` | `5.0` | Wall-clock limit per run. |
| `SANDBOX_MEMORY_MB` | `256` | Memory cap per run. |
| `SANDBOX_MAX_CONCURRENT` | `2` | Concurrent sandbox children (semaphore). |

`Settings.sandbox_enabled` mirrors the flag. The executor itself reads `SANDBOX_ENABLED` when the container does not pass `enabled=` explicitly; see the Stage 2 report for the one-line `container.py` change that makes `Settings` authoritative.

## Status lifecycle

| Status | Meaning |
| :--- | :--- |
| `ok` | Ran within bounds; output is `stdout`. |
| `error` | Syntax/runtime error, **or a refusal** (`sandbox refused to run: ...`, `sandbox runner did not confirm isolation`, `sandbox busy`). |
| `timeout` | Wall-clock or CPU limit exceeded; the process tree was killed. |
| `memory_exceeded` | Memory cap hit. |
| `blocked` | Refused by the static gate before spawn, or the audit hook denied an operation (the event is reported even if the code caught the exception). |

Status comes from the exit code and structured events on an inherited control pipe, not from stdout/stderr text. `limits_enforced` (`timeout`, `memory`, `process_limit`, `network_guard`) reports what was actually in force; a result that never ran reports nothing as enforced. `SandboxResult.isolation` adds `filesystem_guard`, `windows_low_integrity`, `network_namespace`. When no sandbox tool ran in a turn the API reports `sandbox.status = "idle"`.

## Tests

- **SDK Contract Verification**: `tests/test_sdk_core.py` verifies standard sandbox instantiation, memory bounds, and code execution.
- **Deep OS Isolation & Penetration Harness**: In `nexus-sample`, `backend/tests/test_sandbox.py` (mechanics), `test_stage2_isolation.py` (host untouched: reading/listing/writing outside the work dir, `importlib` tricks, live-listener socket connect, subprocess, fork bomb, kernel-level write denial, fail-closed paths, handle leaks), and `test_stage2_policy.py` (disabled by default, admin-only, code-from-user, session ownership). Each isolation attack runs once with the static gate on and once with it off, proving the runtime layers on their own.
