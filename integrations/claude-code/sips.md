---
description: Estimate your SIPS (how much leverage your interventions have) from your own Claude Code sessions
---
Run `sips-score analyze --harness claude-code --project "$(basename "$PWD")"` with Bash (install first with `pipx install git+https://github.com/ebaenamar/sips-score` if `sips-score` is missing).
Show the report as it is. Then pick the top finding and offer to apply it: for a repeated constraint, propose the exact line
to add to CLAUDE.md; for a repeated procedure, propose a custom command file in .claude/commands/. Do not apply anything
without asking.
