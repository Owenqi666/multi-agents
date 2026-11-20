"""Compare offline samples while preserving their measured log sizes on disk."""

import re
from pathlib import PurePosixPath

_EVENT_LOG = re.compile(r"wire|(?:claude|codex)(?:-environment)?-[0-9a-f]{32}")


def _has_runtime_metadata(receipt, job_stdout):
    path = receipt.get("path")
    if isinstance(path, str) and path.endswith(".jsonl"):
        return _EVENT_LOG.fullmatch(PurePosixPath(path).stem) is not None

    identifier = receipt.get("artifact_id")
    if not isinstance(identifier, str):
        return False
    stream = receipt["stream"]
    if stream == "stdout":
        return identifier == job_stdout
    return identifier.startswith("art-") and _EVENT_LOG.fullmatch(stream) is not None


def comparison_value(value, *, job_stdout=None):
    """Normalize positive byte counts only on receipts for dynamic logs."""
    if isinstance(value, list):
        return [comparison_value(item, job_stdout=job_stdout) for item in value]
    if not isinstance(value, dict):
        return value

    result = {
        key: comparison_value(
            child,
            job_stdout=f"{value['job_id']}.stdout"
            if key == "artifacts"
            and "job_id" in value
            and isinstance(value.get("result"), dict)
            else None,
        )
        for key, child in value.items()
    }
    size = value.get("byte_count")
    if (
        type(size) is int
        and size > 0
        and "sha256" in value
        and "text" not in value
        and _has_runtime_metadata(value, job_stdout)
    ):
        result["byte_count"] = "<recorded runtime log size>"
    return result
