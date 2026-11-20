"""Launch a disposable workbench backed by real Git and synthetic model CLIs."""

import argparse
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from fake_runtime import install_transports
from offline_fixture import SCENARIOS, create_fixture


def _signal(pid, number):
    try:
        os.kill(pid, number)
    except ProcessLookupError:
        pass


def _hard_stop(process):
    # Product jobs and model runtimes start new process groups. Freezing the
    # service first also prevents an exiting job from recording a terminal state.
    descendants = {process.pid}
    _signal(process.pid, signal.SIGSTOP)
    while True:
        rows = subprocess.check_output(
            ["/bin/ps", "-axo", "pid=,ppid="], text=True, stdin=subprocess.DEVNULL
        )
        parents = [tuple(map(int, row.split())) for row in rows.splitlines()]
        children = {pid for pid, parent in parents if parent in descendants}
        new = children - descendants
        if not new:
            break
        for pid in new:
            _signal(pid, signal.SIGSTOP)
        descendants.update(new)
    for pid in descendants - {process.pid}:
        _signal(pid, signal.SIGKILL)
    _signal(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


def _stop(process, *, hard):
    if process.poll() is not None:
        return
    if hard:
        _hard_stop(process)
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _hard_stop(process)


def _launch(root, env, port, frontend_dist):
    arguments = [
        sys.executable,
        "-m",
        "multi_agent.web",
        "--repository",
        str(root / "repository"),
        "--data-dir",
        str(root / "data"),
        "--port",
        str(port),
        "--codex-bin",
        str(root / "bin" / "codex"),
        "--claude-bin",
        str(root / "bin" / "claude"),
    ]
    if frontend_dist is not None:
        arguments.extend(("--frontend-dist", str(frontend_dist)))
    return subprocess.Popen(
        arguments,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _serve(root, env, port, frontend_dist):
    requested = None

    def request(number, _frame):
        nonlocal requested
        if requested != signal.SIGINT:
            requested = number

    for number in (signal.SIGINT, signal.SIGUSR1, signal.SIGHUP):
        signal.signal(number, request)

    while requested != signal.SIGINT:
        with _launch(root, env, port, frontend_dist) as process:
            try:
                if not select.select([process.stdout], [], [], 10)[0]:
                    raise RuntimeError("offline service did not publish its address")
                origin = process.stdout.readline().strip()
                address = urlsplit(origin)
                if address.hostname != "127.0.0.1" or not address.port:
                    raise RuntimeError(
                        "offline service did not publish a loopback address"
                    )
                port = address.port
                print(origin, flush=True)
                while requested is None and process.poll() is None:
                    time.sleep(0.05)
                if requested is None:
                    raise RuntimeError(
                        f"offline service exited with status {process.returncode}"
                    )
                if requested == signal.SIGINT:
                    return
                hard = requested == signal.SIGUSR1
                requested = None
                _stop(process, hard=hard)
            finally:
                _stop(process, hard=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--frontend-dist", type=Path)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    with TemporaryDirectory(prefix="multi-agent-offline-") as directory:
        root = Path(directory).resolve()
        env = create_fixture(root, args.scenario)
        install_transports(root)
        try:
            _serve(root, env, args.port, args.frontend_dist)
        except (OSError, RuntimeError, ValueError) as error:
            parser.exit(1, f"offline environment: {error}\n")


if __name__ == "__main__":
    main()
