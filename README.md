# sips

**How much leverage do your interventions have when you work with an AI agent?**

`sips` reads the session logs your agent harness already keeps (Claude Code, Codex CLI, or any chat-format JSON) and
estimates your **Sustainable Intervention Point Score**, from the framework in
[Baena, Torres & Chíncaro, BiAlign@CHI'26](https://philab.xyz/papers/sustainable-intervention-points.pdf).
It tells you where your effort goes, why each intervention got the tier it did, and what to change.

Everything runs on your machine. Nothing is uploaded. Secrets are masked before anything is printed.

```
pipx install git+https://github.com/ebaenamar/sips-score
sips-score analyze                   # reads ~/.claude/projects and ~/.codex/sessions if present
```

No logs yet? Try the two example projects (opposite styles, same tasks):

```
python examples/make_examples.py
sips-score analyze --harness claude-code --path examples/low-leverage    # SIPS 42.4
sips-score analyze --harness claude-code --path examples/high-leverage   # SIPS 78.7
```

## Score your project (for hackathons)

No install: paste a public repo into the web portal (see `server/`, also an API: `POST /api/score {"repo": "https://github.com/owner/name"}`). The portal stores the repo URL and result; the CLI below keeps everything on your machine.

```
pipx install git+https://github.com/ebaenamar/sips-score
cd your-project
sips-score repo .
```

`sips-score repo` reads your code and git history (it never runs anything) and answers two questions with file:line
evidence: **where does your solution put the human** (rules, examples and evals, oversight, per-step approval,
escalation, a learning loop), and **how was it built with AI** (AI co-authored commits, rules files, tests, and your Claude
Code sessions if they exist on this machine). It prints a SIPS design estimate, each factor with its reason, and the
changes that would raise it.

Escalation follows **HiL-Bench** (Trinh et al., 2026, [arXiv:2604.09408](https://arxiv.org/abs/2604.09408)): a good loop
asks a person when it hits a blocker it cannot resolve, and does not ask otherwise. If your loop escalates, run it on a
few scenarios, some with a hidden blocker (missing or contradictory information), log one line per run, and score it:

```
{"blocker": true,  "asked": true,  "question_ok": true}
{"blocker": false, "asked": true}
sips-score askf1 runs.jsonl        # Ask-F1 = harmonic mean of question precision and blocker recall
```

Try it first on the two example loops: `python examples/make_loop_examples.py` then
`sips-score repo examples/loops/designed-loop` (≈80) and `sips-score repo examples/loops/approve-every-step` (≈32).

## What you get

```
SIPS  66.5 / 100   (2821 human interventions, 240 sessions)

Where your interventions go
  Tier 1 Strategic control    ██··················   263 (  9%)  median leverage    7.0
  Tier 2 Calibration          ████················   523 ( 19%)  median leverage    2.8
  Tier 3 Informed oversight   █████···············   637 ( 23%)  median leverage    1.6
  Tier 4 Mechanical review    ██████████··········  1398 ( 50%)  median leverage    1.0

What would raise your score
  1. Turn repeated instructions into rules or commands (estimated SIPS 66.5 → 66.8)
     Why: Each repetition is a tier-4 action with leverage 1. Short procedures you repeat belong in a
          custom command (.claude/commands/ship.md); constraints belong in CLAUDE.md / AGENTS.md or a hook.
       · “aplica todo y sube a main” — said 29 times
```

Every component prints its formula and inputs. `sips-score explain <turn-id>` shows the rule and the words behind any single
assignment, the feedback type, the agent actions that followed and the leverage estimate.

## The idea in one paragraph

The paper ranks human interventions by **leverage**: how many downstream agent decisions one action changes.
Approving or fixing one output (tier 4) changes one decision. Writing a rule into `CLAUDE.md`, a test, or a hook
(tiers 1–2) changes every later session. People often *speak* at tier 1 ("never touch the migrations") but get tier-4
leverage because the rule lives only in the chat and has to be said again. `sips` measures that gap.

It also reads **how** you give feedback: "doesn't work" (information 0.15) and "fails because the token expires before
refresh" (0.85) are both corrections, but only the second teaches the agent something it can reuse, and keeps you reasoning.

## Closing the loop: the agent learns how well it has been serving you

The agent never sees its own track record. `sips-score` gives it back, using the feedback you already give in chat
(corrections, retries, interruptions, repeated rules). Nothing new to fill in; add `#bad <why>` or `#good` to any prompt
if you want to be explicit.

| Who | When | What they get |
|---|---|---|
| You | `sips-score analyze` | Score, why, and what to change |
| The agent | Start of every session | A short note: its rework rate here, rules it was told more than once, recurring complaints, rules not yet in CLAUDE.md |
| The agent | When your prompt repeats a past correction | "You were told this 5 times in this project: …" |

**Claude Code**: merge [`integrations/claude-code/settings.json`](integrations/claude-code/settings.json) into
`~/.claude/settings.json`. The session-start hook does the analysis (a few seconds) and saves a small redacted index in
`~/.cache/sips-score/`; the per-prompt hook only reads it (≈0.1 s). Hooks never block a session: on any error they add nothing.

**Codex and other harnesses**: `sips-score brief --write AGENTS.md` writes the note between `<!-- sips:start -->` markers
and leaves the rest of the file alone. Re-run it (or put it in a git hook) to refresh.

**Check it works**: compare before and after, e.g. `sips-score analyze --until 2026-11-10` vs `--since 2026-11-10`.
Repeated instructions and rework should go down. If they do not, the note is not helping, and that is worth knowing too.

## Commands

| Command | What it does |
|---|---|
| `sips-score analyze [--project X] [--json out.json] [--list]` | Score, components, tier mix, feedback types, weekly trend, per-project scores, findings with estimated impact |
| `sips-score explain <turn-id>` | Why one turn got its tier: rule, matched words, feedback type, reach, leverage |
| `sips-score label -n 40` | Label a random sample of your own turns (stored locally, redacted) |
| `sips-score brief [--write AGENTS.md]` | The note for the agent: its track record in this project |
| `sips-score hook session-start \| prompt` | Claude Code hooks (see above) |
| `sips-score eval [--llm claude\|codex\|"<cmd>"]` | Accuracy of the classifier against your labels, per-tier precision/recall, confusion matrix |

## Harnesses

- **Claude Code**: `~/.claude/projects/**.jsonl` (handles resumed sessions, which copy earlier messages into new files)
- **Codex CLI**: `~/.codex/sessions/**/rollout-*.jsonl`
- **Anything else**: `--harness generic --path chats.jsonl` with `{"role","content","tool_calls"?}` messages, or emit the
  open event format in [SPEC.md](SPEC.md)
- **Inside Claude Code**: copy `integrations/claude-code/sips.md` to `.claude/commands/` and run `/sips`

## How accurate is it? (read this)

The tier classifier uses explainable rules (English and Spanish). On **real, hand-labelled turns** it is not yet good:

| Set | Turns | Accuracy |
|---|---|---|
| Development set (rules were tuned on it) | 57 | 75% |
| First held-out set (before tuning on it) | 40 | 48% |
| Fresh test set (never seen) | 40 | **42%** |

It is reliable on approvals and mechanical turns (recall ~85–90%) and weak at telling calibration from oversight.
The behavioural signals that do not depend on fine tier semantics (repeated instructions, rules that never persisted,
fast approvals, feedback information, interruptions) are the most trustworthy part of the report.
`sips-score eval --llm claude` lets you check whether your own agent CLI classifies better; it sends redacted turns to that
provider, so only use it if you are fine with that. Details and every proxy in [METHOD.md](METHOD.md).

## Contribute

The most useful contribution is **labelled data**: run `sips-score label`, check the labels you are comfortable sharing,
and open a PR to `data/`. Rules improve against labels, not opinions. New harness adapters are one function in
`sips/adapters.py`.

MIT licence.
