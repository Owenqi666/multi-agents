"""Development-only recordings of local Codex and Claude protocols.

Only the user runs these commands. The ten turn-based steps issue model requests
and consume the user's quota. The two env steps and codex-sandbox do not issue
model requests. Implementers and automated tests use fake executables only.

Typical commands (after installing this project):

    python -m multi_agent.adapters.probe codex-env codex-sandbox claude-env
    python -m multi_agent.adapters.probe codex-turn codex-structured codex-readonly codex-cancel codex-bad-model claude-turn claude-structured claude-readonly claude-cancel claude-bad-model

Output is probe-runs/<UTC timestamp>/ by default, relative to the current working
directory; --out changes the root. Each step has a redacted JSONL file alongside
summary.json. Run the env steps with the other steps when collecting version and
authentication context for the same recording.

codex-sandbox executes a read and a write attempt under readOnly, then a write
control under workspaceWrite with the working directory in writableRoots.
Completed Codex turns are followed by paginated thread/items/list requests.
Cancellation waits only for the turn to end; the subsequent evidence collection
remains subject to the whole-step deadline.

Every Claude child receives the inherited environment with ANTHROPIC_API_KEY and
ANTHROPIC_AUTH_TOKEN removed. There is no need to unset these variables manually
when using this probe. The env_removed observation records variable names only;
Codex keeps the inherited environment. Official variable definitions:
https://code.claude.com/docs/en/env-vars.

Evidence boundaries:
* finished means the recording step finished, including failed model requests;
  it is not evidence that configuration or permissions passed verification.
* Recordings are raw material. Evidence is assessed when the protocol fact tables
  are updated, not by this probe.
* Claude has no model-catalog query here. Effective effort remains unconfirmed
  when no runtime-reported value can be found in the recording.
* Codex thread/start.reasoningEffort precedes turn/start's effort override and
  cannot establish the effective effort of that turn.

Protocol names come from the checked-in protocol/ schemas and help snapshots.
The sole command exception is `claude auth status`, documented in the official
CLI reference: https://code.claude.com/docs/en/cli-reference#cli-commands.
"""
