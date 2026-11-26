"""Record normalized workspace API samples through the offline HTTP service."""

import argparse
import json
import select
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

from sample_normalization import SampleNormalizer
from sample_scenarios import SCENARIOS

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE.parents[2] / "docs" / "web-api" / "samples"
TIMEOUT = 60
TERMINAL = {"finished", "interrupted"}


@dataclass
class Exchange:
    method: str
    target: str
    request_body: dict | None
    status: int
    response: dict
    supplemental: bool = False
    file_number: int | None = None


class Recorder:
    def __init__(self, origin, process):
        self.origin = origin
        self.process = process
        self.target = urlsplit(origin)
        self.exchanges = []
        self._supplemental = False
        self.normalizer = SampleNormalizer()
        self.normalizer.bind(origin=origin)

    def send(self, method, target, body=None):
        connection = HTTPConnection(self.target.hostname, self.target.port, timeout=10)
        try:
            headers = {"Origin": self.origin}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            connection.request(method, target, payload, headers)
            response = connection.getresponse()
            return Exchange(method, target, body, response.status, json.load(response))
        finally:
            connection.close()

    def keep(self, entry, status):
        assert entry.status == status, (entry.method, entry.target, entry.response)
        entry.supplemental = self._supplemental
        self.exchanges.append(entry)
        return entry.response

    @contextmanager
    def supplement(self):
        """Add exchanges without renumbering existing files used by the UI tests."""
        previous = self._supplemental
        self._supplemental = True
        try:
            yield
        finally:
            self._supplemental = previous

    def request(self, method, target, body=None, *, status=200):
        return self.keep(self.send(method, target, body), status)

    def get(self, suffix, *, file_number=None):
        entry = self.send("GET", f"/api/{suffix}")
        entry.file_number = file_number
        return self.keep(entry, 200)

    def start(self, action, **args):
        queued = self.request(
            "POST", "/api/jobs", {"action": action, "args": args}, status=202
        )
        assert queued["state"] == "queued", queued
        return queued["job_id"]

    def wait(self, identifier, states=TERMINAL):
        deadline = time.monotonic() + TIMEOUT
        target = f"/api/jobs/{identifier}"
        while time.monotonic() < deadline:
            entry = self.send("GET", target)
            assert entry.status == 200, (target, entry.status, entry.response)
            state = entry.response["state"]
            if state in states:
                return self.keep(entry, 200)
            assert state not in TERMINAL, (states, entry.response)
            time.sleep(0.05)
        raise TimeoutError(f"{target} did not reach {states}")

    def finish(self, identifier, exit_code=0):
        result = self.wait(identifier)
        assert result["state"] == "finished" and result["exit_code"] == exit_code, (
            result
        )
        return result

    def job(self, action, *, exit_code=0, **args):
        return self.finish(self.start(action, **args), exit_code)["result"]

    def restart(self):
        self.process.send_signal(signal.SIGUSR1)
        restarted = read_origin(self.process)
        assert restarted == self.origin, (self.origin, restarted)

    def write(self, directory):
        normalize = self.normalizer.normalize
        index = []
        files = {}
        # Allocate legacy filenames and canonical IDs first. The index still
        # records the real HTTP order, including reads between two mutations.
        ordered = sorted(
            enumerate(self.exchanges, 1), key=lambda pair: pair[1].supplemental
        )
        # Pin filenames when a read moves to a different point in the replay.
        # Normalize in file order so existing responses keep their canonical IDs.
        reserved = {entry.file_number for _, entry in ordered if entry.file_number}
        available = iter(n for n in range(1, len(ordered) + 1) if n not in reserved)
        numbered = [
            (entry.file_number or next(available), seq, entry) for seq, entry in ordered
        ]
        for number, seq, entry in sorted(numbered):
            target = urlsplit(entry.target)
            name = f"{number:03}.json"
            index.append(
                {
                    "seq": seq,
                    "method": entry.method,
                    "path": normalize(target.path),
                    "query": normalize(target.query or None),
                    "request_body": normalize(entry.request_body),
                    "status": entry.status,
                    "response_file": name,
                }
            )
            files[name] = normalize(entry.response)
        files["index.json"] = sorted(index, key=lambda entry: entry["seq"])
        directory.mkdir(parents=True, exist_ok=True)
        for name, value in files.items():
            (directory / name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                encoding="utf-8",
            )
        for obsolete in directory.glob("[0-9][0-9][0-9].json"):
            if obsolete.name not in files:
                obsolete.unlink()


def read_origin(process):
    stdout = process.stdout
    readable, _, _ = select.select([stdout], [], [], TIMEOUT)
    if not readable:
        raise TimeoutError("The offline service did not announce its address")
    line = stdout.readline().decode("utf-8").strip()
    origin = urlsplit(line)
    if origin.scheme != "http" or origin.hostname != "127.0.0.1" or not origin.port:
        raise RuntimeError(f"Invalid offline service address: {line!r}")
    return line


def record(scenario, output):
    command = [
        sys.executable,
        str(HERE / "offline_env.py"),
        "--scenario",
        scenario,
        "--port",
        "0",
    ]
    with (
        tempfile.TemporaryFile() as stderr,
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr
        ) as process,
    ):
        try:
            recorder = Recorder(read_origin(process), process)
            SCENARIOS[scenario](recorder)
        except Exception:
            stderr.seek(0)
            sys.stderr.write(stderr.read().decode("utf-8", errors="replace"))
            raise
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
        assert process.returncode == 0, process.returncode
    recorder.write(output / scenario)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    scenarios = [args.scenario] if args.scenario else SCENARIOS
    for scenario in scenarios:
        record(scenario, args.output)


if __name__ == "__main__":
    main()
