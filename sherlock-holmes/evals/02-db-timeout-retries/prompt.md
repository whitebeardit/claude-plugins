---
name: 02-db-timeout-retries
description: "Loki-only case: DB timeouts with retries, no trace in Tempo"
tags: [trace-debug, offline]
runs: 1
max_turns: 40
timeout_seconds: 600
allowed_tools: [Read, Skill, Agent]
expected_outcome: "the report identifies the first 'DB connection timeout (attempt 1/3)' in inventory-service as the first anomalous event, describes attempts 2/3 and 3/3 as retries of the same failure (not separate incidents), treats the order-service 503 as a consequence, and says the cause of the timeout is unknown from this evidence."
---

We had an incident in production. Investigate trace `df34d418bee785c2a1d46b9539d0ccc2` and tell me what happened, where it started, how it propagated and how confident you are.

Loki and Tempo are not reachable from this machine: the recorded API responses for this trace are in `./fixtures` (use them instead of the live services).

Reply with the full investigation report, not a summary of it.
