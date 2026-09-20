---
type: llm
focus: last_message
weight: 2
---

Scenario: logs only say 'request failed'; the S3 AccessDenied is visible only in the span tree.

PASS if the report attributes the failure to the S3 GetObject AccessDenied (not authorized to perform s3:GetObject) inside report-service, states that cache-service succeeded, and relates the api 500 to it as a consequence.

FAIL if it stops at 'report-service request failed' without naming the S3 AccessDenied cause, or it blames api or cache-service, or it invents a cause other than the S3 authorization error.

Expected confidence: HIGH. A different confidence alone is not a FAIL unless the FAIL conditions say so; wrong origin or a fabricated cause is always a FAIL.
