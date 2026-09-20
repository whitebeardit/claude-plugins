---
name: 01-downstream-503
description: "downstream PostgreSQL timeout propagates as 503 -> 502 -> 500"
tags: [trace-debug, offline]
runs: 1
max_turns: 40
timeout_seconds: 600
allowed_tools: [Read, Skill, Agent]
expected_outcome: "the report names the PostgreSQL connection timeout inside customer-service as the first anomalous event and the origin of the chain customer-service -> payment-service -> api, and treats the api HTTP 500 and the payment-service 502/503 as consequences."
---

We had an incident in production. Investigate trace `2aa803b2e40c97a2490d754a465fe9de` and tell me what happened, where it started, how it propagated and how confident you are.

Loki and Tempo are not reachable from this machine: the recorded API responses for this trace are in `./fixtures` (use them instead of the live services).

Reply with the full investigation report, not a summary of it.
