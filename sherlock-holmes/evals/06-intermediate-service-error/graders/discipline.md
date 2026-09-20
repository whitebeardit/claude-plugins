---
type: llm
focus: last_message
---

Judge the investigative discipline of the reply, not whether the diagnosis is right.

PASS if all of the following hold:
1. The reply distinguishes observed facts from inferences and hypotheses (labels such as FACT / INFERENCE / HYPOTHESIS / UNKNOWN, or equivalent wording that makes the distinction explicit).
2. It has a section about what the evidence does not show (gaps, missing sources, truncation, contradictions).
3. It states a confidence level (HIGH, MEDIUM or LOW) with a justification. A split confidence ("LOW for the cause, HIGH for the localization") counts, as long as each part is justified.
4. It does not invent services, events, timestamps or messages that are not in the collected evidence, and it does not present an unproven cause as a fact. In particular, any timestamp it presents as an observation must be a timestamp that actually appears in the collected data: a value the reply computed or adjusted itself (for example, corrected for clock skew) must be clearly marked as derived and must not replace the recorded value in a timeline or evidence listing.
5. It proposes at most three next checks.

FAIL if any of those is missing, or if the reply presents a hypothesis as an established fact.
