"""Bootstrap that runs INSIDE the sandbox child process, before the user's code.

This file is never imported by the application. ``executor.py`` copies it next to (not into) the
child's working directory and starts ``python -I -S -X utf8 child_runner.py main.py``.

What it does, in order:

1. pops its control settings from the environment (so user code cannot read them);
2. applies the POSIX resource limits and FAILS CLOSED (exit 98, user code never runs) if any cannot
   be applied. On Windows the equivalent limits are applied by the parent through a Job Object
   before this process is resumed;
3. blocks the network at the Python level (socket monkeypatch + audit hook);
4. installs an audit hook that confines filesystem access to the sandbox working directory
   (writes) plus the interpreter's own library directories (reads), and refuses process
   creation, native code loading and a handful of introspection routes;
5. execs the user's code.

HONEST LIMITS: an audit hook runs inside the same process as the code it polices. It is a strong
speed bump, not a kernel boundary. It cannot see calls that do not raise an audit event (for
example ``os.stat``), and native code or an interpreter bug defeats it. The OS-level pieces
(rlimits / Job Object / Windows low integrity level) are what holds when this hook is bypassed.
See docs/sandbox.md.
"""
import json
import os
import sys
import traceback

_FATAL_EXIT = 98
_MEMORY_EXIT = 97

_ctl = None


def _note(**event):
    if _ctl is not None:
        try:
            os.write(_ctl, (json.dumps(event) + "\n").encode())
        except Exception:
            pass


def _fatal(reason):
    _note(event="fatal", reason=reason)
    try:
        sys.stderr.write("sandbox refused to start: " + reason + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(_FATAL_EXIT)


def _open_ctl(handle):
    if not handle:
        return None
    try:
        if os.name == "nt":
            import msvcrt

            return msvcrt.open_osfhandle(int(handle), os.O_WRONLY)
        return int(handle)
    except Exception:
        return None


def _apply_posix_limits(cfg):
    """Returns the dict of limits that are in force; raises if a required one cannot be applied."""
    try:
        import resource
    except ImportError:
        return None  # Windows: the parent's Job Object is the enforcement
    mem = int(cfg.get("memory_bytes") or 0)
    cpu = int(cfg.get("cpu_seconds") or 0)
    fsize = int(cfg.get("fsize_bytes") or 0)
    applied = {}
    if mem:
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        applied["memory"] = True
    if cpu:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        applied["cpu"] = True
    if fsize:
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        applied["fsize"] = True
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    applied["process_limit"] = True
    return applied


def _unshare_network():
    """Linux only, best effort: drop into an empty network namespace. Returns True when it worked."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        clone_newuser, clone_newnet = 0x10000000, 0x40000000
        return libc.unshare(clone_newuser | clone_newnet) == 0
    except Exception:
        return False
    finally:
        # ctypes must not stay cached in sys.modules: a cached module skips the "import" audit event.
        for name in [k for k in sys.modules if k == "_ctypes" or k == "ctypes" or k.startswith("ctypes.")]:
            del sys.modules[name]


def _install_guards(work, read_roots):
    """Install the socket patch and the audit hook. Everything the hook needs is closed over as
    immutable values; the function object itself is reachable only from the interpreter's hook list."""
    import socket

    def net_blocked(*_a, **_k):
        _note(event="blocked", kind="network")
        raise PermissionError("network access is disabled in the sandbox")

    for name in ("connect", "connect_ex", "sendto"):
        setattr(socket.socket, name, net_blocked)
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        setattr(socket, name, net_blocked)

    sep = os.sep
    realpath, normcase, fsdecode = os.path.realpath, os.path.normcase, os.fsdecode
    pathlike = os.PathLike
    work_n = normcase(realpath(work))
    roots_n = tuple(normcase(realpath(r)) for r in read_roots)
    o_write = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    note = _note

    net_events = frozenset({
        "socket.__new__", "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
        "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "socket.sendmsg",
        "socket.gethostname", "socket.sethostname", "socket.getservbyname", "socket.getservbyport",
    })
    proc_events = frozenset({
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
        "os.startfile", "os.add_dll_directory", "_posixsubprocess.fork_exec", "pty.spawn", "os.kill",
        "os.killpg", "signal.pthread_kill", "os.chroot", "os.symlink", "os.link", "os.mkfifo", "os.mknod",
        "sys._current_frames", "sys._current_exceptions", "sys.settrace", "sys.setprofile",
        "gc.get_objects", "gc.get_referrers", "gc.get_referents", "webbrowser.open", "urllib.Request",
        "sqlite3.connect", "sqlite3.connect/handle", "os.setuid", "os.setgid", "os.setgroups",
    })
    proc_prefixes = ("ctypes.", "_winapi.", "msvcrt.", "winreg.", "_posixshmem.", "mmap.")
    denied_modules = frozenset({
        "ctypes", "_ctypes", "gc", "subprocess", "_winapi", "_posixsubprocess", "multiprocessing", "_multiprocessing",
        "pty", "msvcrt", "winreg", "_winreg", "sqlite3", "_sqlite3", "webbrowser", "smtplib", "ftplib", "telnetlib",
        "urllib", "http", "socketserver", "asyncio", "_posixshmem", "mmap",
    })
    # events whose listed positional args are filesystem paths that must stay inside the work dir
    path_args = {
        "os.listdir": (0,), "os.scandir": (0,), "os.mkdir": (0,), "os.remove": (0,), "os.rmdir": (0,),
        "os.rename": (0, 1), "os.chmod": (0,), "os.chown": (0,), "os.chdir": (0,), "os.truncate": (0,),
        "os.utime": (0,), "os.setxattr": (0,), "os.getxattr": (0,), "os.listxattr": (0,), "os.removexattr": (0,),
        "shutil.copyfile": (0, 1), "shutil.copymode": (0, 1), "shutil.copystat": (0, 1), "shutil.copytree": (0, 1),
        "shutil.rmtree": (0,), "shutil.move": (0, 1), "shutil.make_archive": (0, 2, 3),
        "shutil.unpack_archive": (0, 1), "shutil.chown": (0,), "glob.glob": (0,), "glob.glob/2": (0, 2),
        "tempfile.mkdtemp": (0,), "tempfile.mkstemp": (0,),
    }

    def resolve(value):
        if isinstance(value, (str, bytes, pathlike)):
            return normcase(realpath(fsdecode(value)))
        return None  # an int fd, None, or something that is not a path

    def inside(path, root):
        return path == root or path.startswith(root + sep)

    def hook(event, args):
        if event in net_events:
            note(event="blocked", kind="network")
            raise PermissionError("network blocked: " + event)
        if event in proc_events or event.startswith(proc_prefixes):
            note(event="blocked", kind="process")
            raise PermissionError("process/native-code/introspection blocked: " + event)
        if event == "open":
            path = resolve(args[0])
            if path is None:
                return
            mode, flags = args[1], args[2]
            writing = (
                any(ch in mode for ch in "wax+") if isinstance(mode, str)
                else bool(isinstance(flags, int) and flags & o_write)
            )
            if inside(path, work_n):
                return
            if not writing and any(inside(path, r) for r in roots_n):
                return
            note(event="blocked", kind="filesystem")
            raise PermissionError("filesystem access outside the sandbox directory is blocked")
        if event in path_args:
            for index in path_args[event]:
                if index < len(args):
                    path = resolve(args[index])
                    if path is not None and not inside(path, work_n):
                        note(event="blocked", kind="filesystem")
                        raise PermissionError("filesystem access outside the sandbox directory is blocked")
            return
        if event == "import":
            module, filename = args[0], args[1]
            if str(module).split(".")[0] in denied_modules:
                note(event="blocked", kind="import")
                raise PermissionError("import of " + str(module) + " is blocked in the sandbox")
            if filename is not None:
                path = resolve(filename)
                if path is not None and not any(inside(path, r) for r in roots_n):
                    note(event="blocked", kind="import")
                    raise PermissionError("loading modules from outside the interpreter library is blocked")

    sys.addaudithook(hook)


def _library_roots():
    """Directories the interpreter itself imports from (read-only for user code). Taken from sys.path
    as it stands after startup under -I -S: stdlib, DLLs / lib-dynload, the stdlib zip. Anything that
    looks like a third-party site directory is excluded."""
    roots = []
    for entry in sys.path:
        if not entry or not os.path.exists(entry):
            continue
        parts = {p.lower() for p in os.path.normpath(entry).split(os.sep)}
        if parts & {"site-packages", "dist-packages"}:
            continue
        roots.append(entry)
    return tuple(roots)


def main():
    global _ctl
    cfg = json.loads(os.environ.pop("SANDBOX_CFG", "{}") or "{}")
    _ctl = _open_ctl(cfg.get("ctl"))
    source_name = sys.argv[1]
    work = os.getcwd()

    try:
        limits = _apply_posix_limits(cfg)
    except Exception as exc:  # fail closed: the promised limits are not in force
        _fatal("could not apply resource limits: " + type(exc).__name__)
        return
    netns = _unshare_network() if (limits is not None and cfg.get("guards", True)) else False

    with open(source_name, encoding="utf-8") as fh:
        source = fh.read()
    code = compile(source, "main.py", "exec")

    if cfg.get("guards", True):
        try:
            _install_guards(work, _library_roots())
        except Exception as exc:
            _fatal("could not install the audit hook: " + type(exc).__name__)
            return
    _note(
        event="ready",
        os_limits=bool(limits) if limits is not None else None,
        memory=bool(limits and limits.get("memory")),
        process_limit=bool(limits and limits.get("process_limit")),
        network_guard=bool(cfg.get("guards", True)),
        network_namespace=netns,
        filesystem_guard=bool(cfg.get("guards", True)),
    )

    sys.argv = ["main.py"]
    namespace = {"__name__": "__main__", "__builtins__": __builtins__}
    try:
        exec(code, namespace)  # noqa: S102 - this is the sandbox's purpose
    except SystemExit:
        raise
    except MemoryError:
        _note(event="memory_error")
        sys.stderr.write("MemoryError\n")
        sys.stderr.flush()
        os._exit(_MEMORY_EXIT)
    except BaseException as exc:  # report only the user's frames, never the runner's
        traceback.print_exception(type(exc), exc, exc.__traceback__.tb_next if exc.__traceback__ else None)
        sys.stderr.flush()
        os._exit(1)


main()
