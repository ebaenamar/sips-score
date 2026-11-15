# Method

The paper defines SIPS for a configuration p (eq. 2):

    SIPS(p) = Q · L · λ · (1 − ε_h) · S(σ) / (1 + γ/100)

It assumes lab measurements (NASA-TLX for load, unassisted tests for skill). From session logs we can only estimate
each term. Every estimate below is printed with its inputs.

| Term | Paper | Estimate from logs |
|---|---|---|
| Q quality | output quality vs baseline | 1 − rework turns / turns after an agent output. Rework = outcome-only, restated, pattern-report, directive feedback, interrupts, denied tool calls |
| L leverage | downstream decisions changed per human action | per turn: persisted rule → reach × info + later sessions (30 days, max 20) × mean turns per session; repeated instruction → 1; otherwise 1 + (reach − 1) × information. Averaged as log10(1+L)/log10(101) |
| λ learning signal | share of interventions that improve the system | 0.5 × mean feedback information + 0.5 × share of stated rules that were written to a rules file or a test (smoothed) |
| γ cognitive load | NASA-TLX 0–100 | 100 × min(1, (interventions + interrupts) / agent actions / 0.5) |
| σ skill preservation | unassisted performance over time | 0.6 × share of prompts with reasoning or code + 0.4 × (1 − (approvals + fast approvals) / 2n); S(σ) as eq. 3 with σ_min 0.15, σ_base 0.5 |
| ε_h human error | rate of erroneous human input | share of prompts reversing your own earlier instruction |

**Displayed score.** The raw product sits near 0.05–0.10 for most people, which makes comparison hard. `sips` shows
100 × the geometric mean of the six factors (any factor at 0 still gives 0, as the paper requires) and prints the raw
eq. 2 value next to it.

**Persistence counts only if you asked.** An edit to CLAUDE.md, AGENTS.md, memory, hooks or settings is credited to a
turn only when that turn states a rule or mentions those files. The agent editing them on its own is not your intervention.

**Repeats.** Two substantive turns in the same project with Jaccard ≥ 0.5 on content words are the same instruction.
The later ones get leverage 1, and the cluster becomes a finding.

**Findings are counterfactual.** For repeated instructions and outcome-only feedback, `sips` recomputes the score as if
you had persisted the rule once, or explained the failure, and prints before → after.

## Validation so far

Three random samples of real Claude Code turns from one heavy user (Spanish and English), labelled by hand against the
paper's tier definitions. Labels were written before looking at the classifier output.

- Development set, 57 turns: 47% with the first rules, 75% after tuning on it (that 75% is optimistic).
- Held-out set, 40 turns: 48% before any tuning on it.
- Fresh test set, 40 turns: 42%.

Known weaknesses: text pasted inline into a prompt (an article, a review) is read as if you wrote it, so its cue words
count; declarative context ("the file is in X", "I get a 502") read as tier 4; long task specs split between
tiers 1 and 2; questions vs review requests. One labeller, one user: inter-rater agreement is not measured yet. This is
the main open problem and why `sips-score label` / `sips-score eval` exist.

The LLM path (`--llm`) has not been scored here: running it would send the author's private turns to a third-party model.
