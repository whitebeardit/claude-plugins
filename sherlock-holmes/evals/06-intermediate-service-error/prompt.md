---
name: 06-intermediate-service-error
description: "NullPointerException in the intermediate pricing-service; downstream tax-service is healthy"
tags: [trace-debug, offline]
runs: 1
max_turns: 40
timeout_seconds: 600
allowed_tools: [Read, Skill, Agent]
expected_outcome: "the report names the NullPointerException in pricing-service (PriceCalculator.java:88) as the first anomalous event, states that tax-service answered 200 OK and is not the cause, and describes propagation pricing-service -> gateway-bff -> api."
---

We had an incident in production. Investigate trace `84c8614b9b6df5d3565fbdb3f2174e95` and tell me what happened, where it started, how it propagated and how confident you are.

Loki and Tempo are not reachable from this machine: the recorded API responses for this trace are in `./fixtures` (use them instead of the live services).

Reply with the full investigation report, not a summary of it.
