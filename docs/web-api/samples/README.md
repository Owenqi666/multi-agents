# Recorded HTTP scenarios

These four scenarios capture responses from a real local workbench service running against a temporary Git repository and SQLite database. Executable test substitutes supply the model responses. The frontend's offline demo replays the saved exchanges in the browser. [Run the demo](../../../frontend/README.md#run-the-recorded-demo)

| Scenario | What to inspect |
| --- | --- |
| [accept](accept/index.json) | A direct implementation and a delegated integration. Acceptance uses the review round bound to the tested candidate. |
| [rework](rework/index.json) | A reviewer reports a blocker, the implementation changes, and the revised candidate goes through testing and review. |
| [dual-partial](dual-partial/index.json) | Two seats review the same snapshot. The failed seat retries while the successful report remains available. |
| [cancel](cancel/index.json) | A job is cancelled and a separate job is interrupted by a service restart. The earlier result is still readable. |

## File format

In each scenario, `index.json` lists exchanges in order. An entry contains `seq`, `method`, `path`, `query`, `request_body`, `status` and `response_file`. A missing query or request body is JSON `null`. The query omits the leading question mark. Each numbered file, such as `001.json`, contains the corresponding response body.

The recorder normalizes IDs, commits, digests, timestamps, process and file identities, temporary paths and ports consistently across a scenario. Log `byte_count` retains the observed byte length. Rerecord comparisons account for variable-length metadata in CLI receipts and protocol logs. Fixed test output, pagination and exit codes are compared directly.

## Record locally

From the workbench repository root with the Python dependencies installed, write a fresh scenario to a separate directory.

```sh
uv run python tests/unit/web_support/record_samples.py \
  --scenario rework --output /tmp/multi-agents-recordings
```

Omit `--scenario` to run all four. Omitting `--output` writes to this checked-in sample directory. [Recorder source](../../../tests/unit/web_support/record_samples.py) · [Local service guide](../../workbench.md)
