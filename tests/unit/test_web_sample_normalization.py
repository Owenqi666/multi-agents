"""Check cross-response identities in the offline HTTP samples."""

import base64
import json

from web_support.sample_normalization import SampleNormalizer


def _handle(locator):
    data = json.dumps(locator, separators=(",", ":")).encode()
    return "art-" + base64.urlsafe_b64encode(data).decode().rstrip("=")


def test_sample_handles_match_normalized_resources_paths_and_pagination(tmp_path):
    scenario = tmp_path / "offline-scenario"
    url = "http://127.0.0.1:53217"
    identity = "fc" * 16
    digest = "ab" * 32
    oid = "cd" * 20
    locator = ["task-test", identity, {"test_attempt": ["1"]}, "test-1-2-1", digest]
    handle = _handle(locator)
    recorder = SampleNormalizer()
    recorder.bind(origin=url, repository=str(scenario / "repository"))
    status = recorder.normalize(
        {
            "id": identity,
            "sha256": digest,
            "oid": oid,
            "job_url": f"{url}/api/jobs/{identity}",
            "path": f"{scenario}/data/work-{identity}",
            "request": {"model": "gpt-6.2-sol", "effort": "low"},
            "reported_model": None,
            "artifact_id": handle,
        }
    )
    assert status["job_url"] == f"http://127.0.0.1:41001/api/jobs/{status['id']}"
    assert status["path"] == f"/sample/scenario/data/work-{status['id']}"
    assert status["reported_model"] is None and status["request"]["effort"] == "low"
    assert (
        status["id"] != identity and status["sha256"] != digest and status["oid"] != oid
    )
    assert status["artifact_id"] == _handle(
        [
            "task-test",
            status["id"],
            {"test_attempt": ["1"]},
            "test-1-2-1",
            status["sha256"],
        ]
    )
    assert recorder.normalize(f"/api/artifacts/{handle}?offset=1") == (
        f"/api/artifacts/{status['artifact_id']}?offset=1"
    )


def test_embedded_evidence_has_same_normalized_identity_as_resource(tmp_path):
    recorder = SampleNormalizer()
    original = {"identity": [9, 54221], "path": str(tmp_path / "file")}
    recorder.bind(repository=str(tmp_path / "repository"))
    normalized = recorder.normalize(original)
    prompt = "Frozen implementation input (canonical JSON):\n" + json.dumps(original)
    embedded = recorder.normalize({"prompt": prompt})["prompt"]
    assert json.loads(embedded.splitlines()[-1]) == normalized
    raw = (str(tmp_path) + "/repository\n").encode().hex()
    assert bytes.fromhex(recorder.normalize({"hex": raw})["hex"]).decode() == (
        "/sample/scenario/repository\n"
    )
