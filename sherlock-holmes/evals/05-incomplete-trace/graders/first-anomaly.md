---
type: llm
focus: last_message
weight: 2
---

Scenario: fulfillment-service has no logs and no spans at all; order-service's client call to it ends with "context deadline exceeded" after ~29.8 s; api returns 504.

PASS if the report (a) identifies the "context deadline exceeded" on order-service's call to fulfillment-service (POST /fulfil) as the first anomalous event; (b) explicitly states that fulfillment-service has no log lines and no spans in the evidence, and treats what happened inside it as unknown; (c) treats the api 504 as a consequence; and (d) gives LOW or MEDIUM confidence about the cause, or a split confidence where anything said about fulfillment-service's internals is LOW.

Describing the observed behaviour of the call ("the call hung / never got a response within the deadline") is acceptable because the client span shows it. Labelled hypotheses about why are acceptable.

FAIL if it states as a fact that fulfillment-service crashed, was down, restarted, or did anything specific, or it blames the order-service database insert, or it gives HIGH confidence about the cause inside fulfillment-service, or it invents events, logs or spans for fulfillment-service.
