---
name: 05-incomplete-trace
description: "fulfillment-service has no logs and no spans; deadline exceeded on the call to it"
tags: [trace-debug, offline]
runs: 1
max_turns: 40
timeout_seconds: 600
allowed_tools: [Read, Skill, Agent]
expected_outcome: "the report identifies the 'context deadline exceeded' on order-service's call to fulfillment-service (POST /fulfil) as the first anomalous event, explicitly states that fulfillment-service has no log lines and no spans in the evidence (missing evidence), treats the api 504 as a consequence, and gives LOW or MEDIUM confidence."
---

We had an incident in production. Investigate trace `3d31213bebd065c9c19e80638a071b36` and tell me what happened, where it started, how it propagated and how confident you are.

Loki and Tempo are not reachable from this machine: the recorded API responses for this trace are in `./fixtures` (use them instead of the live services).

Reply with the full investigation report, not a summary of it.
