"""Unit tests for sweep-errors.py. Standard library only, no network.

What these protect, in order of importance:
  - facts only: masked signatures never carry values (CPF, ids, UUIDs, IPs, e-mail, tokens), no row
    is hidden (known rows are marked, still listed), silence is not "all fine";
  - the one ordering rule (count desc, first seen asc, then key) and reproducible output (goldens);
  - honest floors: search limits and span-set truncation are declared, not absorbed;
  - input guards: TraceQL/LogQL built only from validated names, a window is mandatory.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "trace-debug" / "scripts" / "sweep-errors.py"
FIXTURES = ROOT / "skills" / "error-sweep" / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"
spec = importlib.util.spec_from_file_location("sweep_errors", SCRIPT)
sw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sw)

CLEAN_ENV = {k: v for k, v in os.environ.items()
             if not k.startswith(("GRAFANA_", "LOKI_", "TEMPO_"))}


def run(*args, env=None, cwd=None):
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "sweep.json"
        r = subprocess.run([sys.executable, str(SCRIPT), *args, "--out", str(out)], capture_output=True, text=True,
                           timeout=60, env=env or CLEAN_ENV, cwd=cwd or tmp)
        doc = json.loads(out.read_text()) if out.exists() else None
    return r, doc


class GoldenTest(unittest.TestCase):
    def test_tables_match_goldens(self):
        update = os.environ.get("UPDATE_GOLDEN") == "1"
        for case in ("cascade", "timeouts", "silence"):
            with self.subTest(case=case):
                r, _ = run("--fixture", str(FIXTURES / case))
                self.assertEqual(r.returncode, 0, r.stderr)
                path = GOLDEN / f"sweep-{case}.txt"
                if update:
                    path.write_text(r.stdout, encoding="utf-8")
                self.assertEqual(path.read_text(encoding="utf-8"), r.stdout, f"{case}: golden drifted (UPDATE_GOLDEN=1 to accept)")

    def test_same_input_same_output(self):
        a, _ = run("--fixture", str(FIXTURES / "cascade"))
        b, _ = run("--fixture", str(FIXTURES / "cascade"))
        self.assertEqual(a.stdout, b.stdout)


class FactsOnlyTest(unittest.TestCase):
    def test_signature_masks_values_and_keeps_status_codes(self):
        cases = {
            "HTTP 500 for POST /payment request_id=af42cd3490ab cpf=123.456.789-42": "HTTP 500 for POST /payment request_id=<ID> cpf=<N>-<N>",
            "upstream customer-service returned 503": "upstream customer-service returned 503",
            "status: 404 for user 12345 at 10.0.0.8:5432": "status: 404 for user <N> at <IP>",
            "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d failed for bob@example.com": "<UUID> failed for <EMAIL>",
            "token=abc123 rejected": "token=*** rejected",
            "returned 5031 bytes": "returned <N> bytes",
        }
        for raw, want in cases.items():
            self.assertEqual(sw.signature(raw), want)
        for leaked in ("123.456.789", "12345", "af42cd3490ab", "bob@example.com", "abc123", "10.0.0.8"):
            self.assertNotIn(leaked, " ".join(sw.signature(r) for r in cases))

    def test_operation_masks_ids_but_keeps_shape(self):
        self.assertEqual(sw.op_signature("GET /customers/42?expand=orders"), "GET /customers/<N>?expand=<V>")
        self.assertEqual(sw.op_signature("/v1/clientes/cpf/:cpf"), "/v1/clientes/cpf/:cpf")
        self.assertEqual(sw.op_signature("GET /orders/a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"), "GET /orders/<UUID>")

    def test_cascade_rows_and_order(self):
        _, doc = run("--fixture", str(FIXTURES / "cascade"))
        rows = doc["rows"]
        self.assertEqual([r["n"] for r in rows], list(range(1, len(rows) + 1)), "one numbering across sources")
        tempo = [r for r in rows if r["source"] == "tempo"]
        self.assertEqual([r["count"] for r in tempo], [4, 4, 4, 4, 4, 4, 1])
        firsts = [r["first"] for r in tempo[:6]]
        self.assertEqual(firsts, sorted(firsts), "equal counts are ordered by first seen")
        self.assertEqual(tempo[0]["examples"][0], "2aa803b2e40c97a2490d754a465fe9de", "most recent example first")
        self.assertEqual(tempo[5]["operation"], "SELECT customer → postgresql")
        self.assertTrue(all(r["traces"] == 4 for r in tempo[:6]))
        loki = [r for r in rows if r["source"] == "loki"]
        self.assertEqual(len(loki), 3, "four customers, one signature per service")
        self.assertEqual(doc["sources"]["loki"]["dropped_below_error"], 4, "INFO lines mentioning an error are counted, not listed")
        self.assertNotIn("123.456.789", json.dumps(doc))

    def test_known_rows_are_marked_and_still_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            pri = Path(tmp) / "priors.md"
            pri.write_text("## Known non-anomalies\n- `POST /payment` is prose for the investigator\n\n"
                           "## Known errors\n- `GET /stock/<N>` delisted SKUs\n", encoding="utf-8")
            r, doc = run("--fixture", str(FIXTURES / "cascade"), "--priors", str(pri))
        known = [r for r in doc["rows"] if r["known"]]
        self.assertEqual([k["operation"] for k in known], ["GET /stock/<N>"], "only the Known errors section counts")
        self.assertIn(" known", r.stdout)
        self.assertIn("GET /stock/<N>", r.stdout)

    def test_silence_is_not_all_clear(self):
        r, doc = run("--fixture", str(FIXTURES / "silence"))
        self.assertEqual(doc["rows"], [])
        self.assertIn("which says nothing about what was not recorded", r.stdout)
        self.assertIn("Not covered:", r.stdout)
        self.assertNotIn("Next:", r.stdout, "nothing to pick, so no offer")


class FloorsTest(unittest.TestCase):
    def _span(self, sid="s1", name="GET /x", svc="svc"):
        return {"spanID": sid, "name": name, "startTimeUnixNano": "1789653751000000000", "durationNanos": "5000000",
                "attributes": [{"key": "status", "value": {"stringValue": "error"}},
                               {"key": "service.name", "value": {"stringValue": svc}}]}

    def test_limit_and_incomplete_search_are_floors(self):
        doc = {"traces": [{"traceID": "a" * 32, "spanSets": [{"spans": [self._span()], "matched": 1}]}],
               "metrics": {"completedJobs": 5, "totalJobs": 10}}
        _, summary = sw.tempo_rows(doc, limit=200, service=None)
        self.assertTrue(summary["truncated"])
        self.assertIn("floor", summary["notes"][0])
        doc["metrics"] = {"completedJobs": 10, "totalJobs": 10}
        self.assertTrue(sw.tempo_rows(doc, limit=1, service=None)[1]["truncated"], "limit reached")
        self.assertFalse(sw.tempo_rows(doc, limit=2, service=None)[1]["truncated"])

    def test_spanset_with_more_matches_than_returned(self):
        doc = {"traces": [{"traceID": "b" * 32, "spanSets": [{"spans": [self._span()], "matched": 9}]}]}
        _, summary = sw.tempo_rows(doc, limit=200, service=None)
        self.assertIn("span counts are a floor", " ".join(summary["notes"]))

    def test_short_trace_ids_regain_their_zeros(self):
        doc = {"traces": [{"traceID": "17c440bcef7f31f210bb561ee62c187", "spanSets": [{"spans": [self._span()], "matched": 1}]}]}
        rows, _ = sw.tempo_rows(doc, limit=200, service=None)
        self.assertIn("017c440bcef7f31f210bb561ee62c187", rows[0]["trace_ids"])


class GuardsTest(unittest.TestCase):
    def test_traceql_only_from_validated_service_names(self):
        self.assertEqual(sw.build_traceql(None).split(" | ")[0], "{ status = error }")
        self.assertIn('resource.service.name = "payment-service"', sw.build_traceql("payment-service"))
        for bad in ('x" || true || "', "a b", "", "x{y}"):
            self.assertFalse(sw.SERVICE_RE.match(bad), bad)
        r, _ = run("--fixture", str(FIXTURES / "cascade"), "--service", 'x" || true || "')
        self.assertEqual(r.returncode, 2)

    def test_logql_refuses_the_empty_selector(self):
        with self.assertRaises(ValueError):
            sw.build_error_logql("{}", "substring", "level")
        with self.assertRaises(ValueError):
            sw.build_error_logql('{app="x"}', "json", "level; drop")
        self.assertEqual(sw.build_error_logql('{app="x"}', "json", "severity"),
                         '{app="x"} | json | severity=~"(?i)(error|err|fatal|critical|crit|panic)"')

    def test_window_is_mandatory_and_absolute(self):
        r, _ = run("--source", "tempo")
        self.assertEqual(r.returncode, 2)
        self.assertIn("give a window", r.stderr)
        r, _ = run("--start", "2026-09-17T13:00:00Z", "--source", "tempo")
        self.assertEqual(r.returncode, 2)
        r, doc = run("--fixture", str(FIXTURES / "timeouts"), "--start", "2026-09-22T00:00:00Z", "--end", "2026-09-24T00:00:00Z")
        self.assertEqual(doc["window"]["start"], "2026-09-22T00:00:00.000Z")
        self.assertEqual(doc["window"]["chosen_by"], "explicit")

    def test_every_source_failing_exits_1(self):
        r, doc = run("--last", "1h")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(all(s["status"] == "error" for s in doc["sources"].values()))
        self.assertIn("no access configured", r.stdout)

    def test_service_filter_applies_to_rows(self):
        _, doc = run("--fixture", str(FIXTURES / "cascade"), "--service", "payment-service")
        self.assertTrue(doc["rows"])
        self.assertEqual({r["service"] for r in doc["rows"]}, {"payment-service"})


if __name__ == "__main__":
    unittest.main()
