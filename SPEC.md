# Human intervention events (v0.1)

A harness-neutral way to log what a person contributes to an agent's work, so any tool can compute SIPS.
One JSON object per line. Harnesses that already keep transcripts do not need this: `sips` has adapters.

```json
{"session": "s1", "project": "billing-api", "ts": "2026-11-07T10:02:11Z", "event": "human",
 "kind": "prompt", "text": "never edit files under generated/"}
{"session": "s1", "ts": "2026-11-07T10:02:15Z", "event": "agent", "kind": "tool", "tool": "Edit", "target": "CLAUDE.md"}
{"session": "s1", "ts": "2026-11-07T10:03:40Z", "event": "human", "kind": "denial", "text": ""}
```

| Field | Values |
|---|---|
| `event` | `human` or `agent` |
| `kind` (human) | `prompt`, `interrupt` (stopped the agent), `denial` (refused a tool call), `approval` (approved a tool call, if the harness records it) |
| `kind` (agent) | `tool` or `text` |
| `tool`, `target` | tool name and the file or command it acted on |

Optional enrichment that `sips` computes when absent: `tier` (1–4), `feedback` (type), `info` (0–1), `persisted` (bool).

Approvals are the one signal most harnesses do not log today. If yours can, log them: they are where rubber-stamping shows.
