from __future__ import annotations

import fcntl
import hashlib
import os
import signal
import socket
import time
from pathlib import Path
from tempfile import gettempdir

import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def is_workspace_server(args: list[str], cwd: str, project_root: Path = PROJECT_ROOT) -> bool:
    if not args:
        return False
    executable = Path(args[0]).name
    if not (executable.startswith("python") or executable == "uvicorn"):
        return False
    root = str(project_root.resolve())
    if "app.main:app" in args:
        return Path(cwd).resolve() == project_root.resolve() or root in args
    return any(
        Path(arg).name == "main.py"
        and (Path(cwd).resolve() == project_root.resolve() or str(Path(arg).resolve()) == str(project_root / "main.py"))
        for arg in args[1:]
    )


def process_port(args: list[str], environment: dict[str, str], default: int = 8000) -> int | None:
    for index, arg in enumerate(args):
        if arg == "--port" and index + 1 < len(args):
            try:
                return int(args[index + 1])
            except ValueError:
                return None
        if arg.startswith("--port="):
            try:
                return int(arg.split("=", 1)[1])
            except ValueError:
                return None
    try:
        return int(environment.get("PORT", str(default)))
    except ValueError:
        return None


def _workspace_server_pids(port: int | None = None) -> list[int]:
    pids: set[int] = set()
    if port is not None:
        pid_path = _pid_path(port)
        try:
            managed_pid = int(pid_path.read_text().strip())
        except (FileNotFoundError, ValueError, OSError):
            managed_pid = None
        if managed_pid and _is_matching_server(managed_pid, port):
            pids.add(managed_pid)

    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdecimal():
            continue
        pid = int(process_dir.name)
        if pid == os.getpid():
            continue
        try:
            args = process_dir.joinpath("cmdline").read_bytes().decode().split("\0")
            args = [arg for arg in args if arg]
            cwd = str(process_dir.joinpath("cwd").resolve())
            raw_environment = process_dir.joinpath("environ").read_bytes().decode(errors="ignore")
            environment = dict(
                entry.split("=", 1)
                for entry in raw_environment.split("\0")
                if "=" in entry
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if is_workspace_server(args, cwd) and (port is None or process_port(args, environment) == port):
            pids.add(pid)
    return sorted(pids)


def _is_matching_server(pid: int, port: int) -> bool:
    process_dir = Path("/proc") / str(pid)
    try:
        args = [arg for arg in process_dir.joinpath("cmdline").read_bytes().decode().split("\0") if arg]
        cwd = str(process_dir.joinpath("cwd").resolve())
        raw_environment = process_dir.joinpath("environ").read_bytes().decode(errors="ignore")
        environment = dict(entry.split("=", 1) for entry in raw_environment.split("\0") if "=" in entry)
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return False
    return is_workspace_server(args, cwd) and process_port(args, environment) == port


def _pid_path(port: int) -> Path:
    project_key = hashlib.sha256(str(PROJECT_ROOT).encode()).hexdigest()[:16]
    return Path(gettempdir()) / f"fomoprofiletracker-{os.getuid()}-{project_key}-{port}.pid"


def _stop_previous_servers(port: int | None = None) -> None:
    pids = _workspace_server_pids(port)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if not any(_process_exists(pid) for pid in pids):
            return
        time.sleep(0.1)

    for pid in pids:
        if _process_exists(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_server(host: str, port: int) -> None:
    project_key = hashlib.sha256(str(PROJECT_ROOT).encode()).hexdigest()[:16]
    lock_name = f"fomoprofiletracker-{os.getuid()}-{project_key}.lock"
    lock_path = Path(gettempdir()) / lock_name
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        _stop_previous_servers()
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_socket.bind((host, port))
            server_socket.listen(2048)
            server_socket.setblocking(False)
            _pid_path(port).write_text(f"{os.getpid()}\n")
        except OSError:
            server_socket.close()
            raise
        fcntl.flock(lock_file, fcntl.LOCK_UN)

    try:
        config = uvicorn.Config("app.main:app", host=host, port=port, reload=False)
        uvicorn.Server(config).run(sockets=[server_socket])
    finally:
        try:
            if int(_pid_path(port).read_text().strip()) == os.getpid():
                _pid_path(port).unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass
