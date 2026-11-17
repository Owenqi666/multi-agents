"""Synthetic Codex command transport; configured only by an adjacent JSON file."""

import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path


def main():
    settings_path = Path(sys.argv[0]).with_suffix(".json")
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    mode = settings.get("mode", "success")
    lock = threading.Lock()
    cancelled = threading.Event()
    children = []
    workers = []

    def capture(kind, **fields):
        if settings.get("capture"):
            with lock, Path(settings["capture"]).open("a") as stream:
                stream.write(json.dumps({"kind": kind, **fields}) + "\n")

    def emit(message):
        capture("response" if "id" in message else "notification", message=message)
        raw = (json.dumps(message) + "\n").encode()
        with lock:
            os.write(1, raw)

    capture("spawn", argv=sys.argv[1:], cwd=os.getcwd(), pid=os.getpid())
    if sys.argv[1:] == ["--version"]:
        time.sleep(settings.get("version_delay", 0))
        print(
            settings.get(
                "version",
                "codex-cli 0.168.0" if mode == "wrong-version" else "codex-cli 0.169.0",
            ),
            flush=True,
        )
        return settings.get("version_exit", 0)
    if sys.argv[1:] != ["app-server", "--strict-config"]:
        return 2
    config_path = Path(os.environ["CODEX_HOME"]) / "config.toml"
    config = tomllib.loads(config_path.read_text())
    capture(
        "configuration",
        config=config,
        codex_home=os.environ["CODEX_HOME"],
        home=os.environ.get("HOME"),
    )

    def stop(*_):
        cancelled.set()
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)

    def command(request):
        params = request["params"]
        identifier = request["id"]
        process_id = params["processId"]
        if mode in ("oversized-line", "message-flood", "invalid-utf8"):
            if mode == "oversized-line":
                os.write(1, b"x" * (1024 * 1024 + 8192))
            elif mode == "message-flood":
                os.write(1, b"{}\n" * 9000)
            else:
                os.write(1, b"\xff\n")
            cancelled.wait(30)
            return
        if mode == "malformed":
            os.write(1, b'{"bad":1,"bad":2}\n')
            cancelled.wait(30)
            return
        if mode in ("wait", "cancel-late-zero"):
            cancelled.wait(settings.get("delay", 30))
            code = 0 if mode == "cancel-late-zero" else 143
        else:
            delay = settings.get("delay", 0)
            if delay:
                cancelled.wait(delay)
            environment = dict(
                config.get("shell_environment_policy", {}).get("set", {})
            )
            for key, value in params.get("env", {}).items():
                if value is None:
                    environment.pop(key, None)
                else:
                    environment[key] = value
            capture(
                "command",
                argv=params["command"],
                cwd=params["cwd"],
                env=environment,
                process_id=process_id,
                request_id=identifier,
            )
            if settings.get("execute", True):
                child = subprocess.Popen(
                    params["command"],
                    cwd=params["cwd"],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                children.append(child)
                cap = params["outputBytesCap"]

                def pump(stream, label):
                    count = 0
                    while data := stream.read(4096):
                        remaining = cap - count
                        chunk = data[: max(remaining, 0)]
                        count += len(chunk)
                        if remaining > 0:
                            emit(
                                {
                                    "method": "command/exec/outputDelta",
                                    "params": {
                                        "processId": process_id,
                                        "stream": label,
                                        "deltaBase64": base64.b64encode(chunk).decode(),
                                        "capReached": count == cap,
                                    },
                                }
                            )
                    stream.close()

                readers = [
                    threading.Thread(target=pump, args=(child.stdout, "stdout")),
                    threading.Thread(target=pump, args=(child.stderr, "stderr")),
                ]
                for reader in readers:
                    reader.start()
                code = child.wait()
                for reader in readers:
                    reader.join()
                if code < 0:
                    code = 128 - code
            else:
                code = 0
            code = settings.get("report_code", code)
        if mode == "bad-base64":
            emit(
                {
                    "method": "command/exec/outputDelta",
                    "params": {
                        "processId": process_id,
                        "stream": "stdout",
                        "deltaBase64": "???",
                        "capReached": False,
                    },
                }
            )
        if mode == "wrong-process":
            emit(
                {
                    "method": "command/exec/outputDelta",
                    "params": {
                        "processId": "different",
                        "stream": "stdout",
                        "deltaBase64": "YQ==",
                        "capReached": False,
                    },
                }
            )
        if mode == "cap":
            emit(
                {
                    "method": "command/exec/outputDelta",
                    "params": {
                        "processId": process_id,
                        "stream": "stdout",
                        "deltaBase64": "YQ==",
                        "capReached": True,
                    },
                }
            )
        if mode in ("missing-final", "no-final"):
            return
        response = {
            "id": identifier,
            "result": {"exitCode": code, "stdout": "", "stderr": ""},
        }
        emit(response)
        if mode == "duplicate-final":
            emit(response)

    for line in sys.stdin:
        request = json.loads(line)
        capture("request", message=request)
        method = request["method"]
        if method == "initialized":
            continue
        if method == "initialize":
            result = {"userAgent": "synthetic-codex/0.169.0"}
        elif method == "config/read":
            result = {
                "config": config,
                "origins": {},
                "layers": [
                    {
                        "name": {"type": "user", "file": str(config_path)},
                        "version": "synthetic-layer",
                        "config": config,
                        "disabledReason": None,
                    }
                ],
            }
            if mode == "bad-config":
                result["config"] = {**config, "approval_policy": "never-unless-fake"}
        elif method == "configRequirements/read":
            result = {"requirements": {} if mode == "requirements" else None}
        elif method == "command/exec":
            worker = threading.Thread(target=command, args=(request,))
            workers.append(worker)
            worker.start()
            continue
        elif method == "command/exec/terminate":
            stop()
            result = {}
        else:
            emit(
                {
                    "id": request["id"],
                    "error": {"code": -32601, "message": "unexpected fake method"},
                }
            )
            continue
        emit({"id": request["id"], "result": result})
    stop()
    for worker in workers:
        worker.join(timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
