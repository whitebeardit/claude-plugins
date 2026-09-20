---
type: llm
focus: last_message
weight: 2
---

Scenario: downstream PostgreSQL timeout propagates as 503 -> 502 -> 500.

PASS if the report names the PostgreSQL connection timeout inside customer-service as the first anomalous event and the origin of the chain customer-service -> payment-service -> api, and treats the api HTTP 500 and the payment-service 502/503 as consequences.

FAIL if it names api or payment-service as where the problem started, or it asserts connection-pool exhaustion (or any other cause of the timeout) as a fact or as the probable cause without saying it is unproven.

Expected confidence: HIGH. A different confidence alone is not a FAIL unless the FAIL conditions say so; wrong origin or a fabricated cause is always a FAIL.
