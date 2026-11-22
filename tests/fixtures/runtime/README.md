# Runtime protocol fixtures

These JSON Lines files are public derivatives of real CLI probe runs captured on November 15, 2026. The adapter regressions replay selected messages locally. The [probe source](../../../src/multi_agent/adapters/probe/) and [protocol snapshots](../../../protocol/) document the requests and responses.

| Directory | Captured cases |
| --- | --- |
| [Codex CLI 0.169.0](codex-0.169.0/) | Environment, sandbox enforcement, ordinary and structured turns, read-only tools, cancellation, and an unavailable model |
| [Claude Code 2.1.325](claude-2.1.325/) | Environment, ordinary and structured turns, read-only tools, cancellation, an unavailable model, and expired authentication |

Each manifest records the originating run, CLI arguments, result, and record count. Each JSON line contains a sequence number, elapsed time, and event kind. Events include process launches, protocol messages, observations, signals, and exits.

The probe masked accounts, organization and installation identifiers, hostnames, balances, and working directories. Account-linked credit IDs were also redacted in the exported data. For public distribution, Claude initialization records omit the local skill, plugin, agent, command, and MCP inventories, additional directories, and messaging socket. Codex MCP startup events use stable placeholder names for servers. An omitted inventory is unknown. Event order, reported outcomes, and the protocol messages used by the tests are retained.
