# The report: structure, writing rules and what to record

The report is the deliverable. Whoever reads it did not see the audit and will decide what to
execute; the page must say what to do, in which order, how much it yields and **what happens to
what is running** when each item is applied.

Before writing: if the environment has artifact-design and data-visualization skills, load them.
An HTML page of its own (not a generic document or dashboard): the reader needs tables, a
forecast chart and short text together. Start from `assets/plan-template.pt-BR.html` (CSS,
composition by category, inline SVG stacked-bar chart with the optimistic marker, tooltip and
equivalent table) and replace the example data with measured data; translate its labels to the
user's language. Title: a short name ("AWS cuts in four stages"), eyebrow with customer, date and
"read-only audit".

## Sections, in this order

1. **Header + 3 tiles**: consolidated bill for the period; "can be cut now (stage 0)"; potential
   at the end of the horizon (value and % of the bill).
2. **Commitment callout**: why shutting down at night does (not) save, with the on-demand overflow
   figure. It is the sentence that prevents the reader's most common mistake.
3. **Where the money goes**: always the same categories (Compute, Network, Data & databases,
   Storage, SaaS & Marketplace, Commitments, Observability, Security, Tax, Other), as horizontal
   bars, coloured "varies with usage" × "fixed or contracted", summing the bill of the tile's
   period; right below, the table of the biggest lines (service or usage type, category, value,
   %). Both come ready from `scripts/ce_breakdown.py map`. Do not mix a 30-day window with the
   current month's pace; if you show the recent pace, label it.
4. **The largest category in detail**: this section changes with the company. If network, the
   InterZone × PublicIP table with volume, cost, monthly pace and cause, the timeline of the jumps
   and what is still unmeasured. If compute, instances/tasks/functions by average CPU and
   reserved × used. If data, instances by class, Multi-AZ, storage and snapshots. If SaaS,
   contracts, renewal dates and measured usage.
5. **The plan in four stages**: one card per stage with window, items (each with `USD x–y` or
   "cost ≈ 0 · protection") and facts: cumulative run-rate, effort, risk (chip low/medium/high with
   text), prerequisite or who decides, **impact on what is running**. Deferred items stay visible
   as "Deferred: … because …".
6. **Savings forecast per month**: stacked bars per front (Compute, Network, Data & storage,
   Contracts & SaaS), conservative range in the bars, optimistic marker, a milestone per month,
   and the table with the same numbers. The conservative/optimistic totals of the last month must
   match the tiles and stage 3.
7. **Levers, one by one**: table with lever, front, nominal/month, realizable today, effort, risk,
   stage, **impact on what is running**. Explain "nominal" × "realizable".
8. **Safe execution**: windows outside the measured peaks (cite the hours), one change at a time
   through IaC/GitOps with rollback, observe 30 minutes, export before deleting, measure before
   cutting, data and contracts only with a validated replacement.
9. **What not to do**: what looks obvious and does not pay, with the numeric reason.
10. **Assumptions and limits**: commitments, prices used, what is an estimate, what was not
    measured, windows and sources, "read-only; nothing was changed".
11. **Footer**: source (the technical note).

## Vocabulary for "impact on what is running" (use literally)

- `none` — does not touch production (metrics, loose volumes and EIPs, NAT without traffic,
  ECR/S3 policies that keep what is in use, log retention).
- `staging only` — non-production environment.
- `window + rollback` — touches the production path but reverts in seconds (routing, configmap);
  needs an off-peak hour and observation.
- `rollout per service` — restarts pods or tasks; one service at a time, off-peak.
- `migration; own plan` — data or contracts; dual write or an agreed window.

## Writing rules

- Plain language, short sentences, no dashes-as-asides and no explanatory parentheses.
- Every number has a unit and a window. Ranges always `x–y`. Percentages of the bill only at the
  top and at the end.
- Say "estimate" when it is one; say "measured" when it is. The share attributed by exclusion
  (e.g. a SaaS cache) carries the word "estimate" in the sentence itself.
- Never write "we can apply" in the audit's voice: the audit recommends, the team decides and
  executes.
- Revisions: add "revised on DD/MM" to the eyebrow and an item under Assumptions saying what
  changed and why. Do not erase history, correct it.

## Consistency before publishing

- Sum of stage 0 items = stage 0 run-rate (conservative and optimistic).
- Last month of the forecast = potential tile = stage 3.
- Compute levers added together ≤ on-demand overflow.
- Every row of the levers table has a stage (or "deferred") and an impact.
- Composition bars sum to the bill shown in the tile.
- No sentence suggests the audit changed anything.

## Record (mandatory)

- A technical note in the team's documentation: method, numbers, limits, append-only timeline
  with every revision and the report link.
- Notes for the next audit: access recipes that worked, pitfalls, commitment (SP) facts and the
  peak profile. Nothing the account already answers by itself.
- Tell the user the link is private until shared, when that is the case.
