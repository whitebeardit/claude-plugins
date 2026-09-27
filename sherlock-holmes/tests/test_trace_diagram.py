"""Unit tests for trace-diagram.py. Standard library only; no archify and no network needed.

What these protect, in order of importance:
  - the diagram is evidence only: nothing invented (no caller for the root span), attributes
    outside the allowlist never reach the HTML, recorded timestamps only;
  - a diagram that cannot be produced never fails the investigation (skipped/failed, exit 0);
  - the golden specifications for the six bundled fixtures (regenerate with UPDATE_GOLDEN=1);
  - archify discovery and the showcase -> standard retry.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "trace-debug" / "scripts"
FIXTURES = ROOT / "evals" / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


td = load("trace_diagram", SCRIPTS / "trace-diagram.py")
ct = load("collect_trace", SCRIPTS / "collect-trace.py")

CASES = {(c.get("name") or c.get("id") or c.get("dir")): c["trace_id"] for c in json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))}


def collect(case: str, tmp: Path) -> dict:
    """Run the real collector on a bundled fixture and return its full JSON."""
    out = tmp / f"{case}.json"
    subprocess.run([sys.executable, str(SCRIPTS / "collect-trace.py"), "--trace-id", CASES[case],
                    "--fixture", str(FIXTURES / case), "--format", "json", "--out", str(out)],
                   capture_output=True, text=True, timeout=60)
    return json.loads(out.read_text(encoding="utf-8"))


def span(sid, parent, service, op, kind, start, end, error=False, attrs=None, status_message=None):
    ms = round((end - start) / 1e6, 1)
    return {"span_id": sid, "parent_span_id": parent, "service": service, "operation": op, "kind": kind,
            "start": ct.iso(start), "end": ct.iso(end), "duration_ms": ms, "status": "ERROR" if error else "OK",
            "status_message": status_message, "error": error, "attributes": attrs or {}, "exception": None, "events": []}


T0 = 1789653751024000000  # 2026-09-17T14:02:31.024Z


def doc_with(spans, log_events=None, missing_parent=None):
    starts = [ct.parse_time(s["start"]) for s in spans]
    ends = [ct.parse_time(s["end"]) for s in spans]
    return {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
            "tempo": {"status": "found", "spans": spans, "span_count": len(spans),
                      "services": sorted({s["service"] for s in spans}), "root_span": spans[0]["span_id"],
                      "trace_start": ct.iso(min(starts)), "trace_end": ct.iso(max(ends)),
                      "duration_ms": (max(ends) - min(starts)) / 1e6,
                      "errored_spans": [s["span_id"] for s in spans if s["error"]],
                      "spans_missing_parent": missing_parent or []},
            "loki": {"status": "ok", "events": log_events or []},
            "facts": {"earliest_error_signals": []}}


class GoldenTest(unittest.TestCase):
    """The six bundled fixtures produce exactly the specification checked into tests/golden/."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.docs = {case: collect(case, Path(cls.tmp.name)) for case in CASES}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_goldens(self):
        update = os.environ.get("UPDATE_GOLDEN") == "1"
        for case, doc in self.docs.items():
            with self.subTest(case=case):
                seq = td.build_sequence(doc)
                path = GOLDEN / f"{case}.sequence.json"
                if seq is None:
                    self.assertEqual(doc["tempo"]["status"], "not_found", "only a missing trace yields no diagram")
                    self.assertFalse(path.exists(), f"{path} must not exist for a case without spans")
                    continue
                text = json.dumps(seq, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
                if update:
                    GOLDEN.mkdir(exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                self.assertTrue(path.exists(), f"missing golden {path.name}: run with UPDATE_GOLDEN=1")
                self.assertEqual(path.read_text(encoding="utf-8"), text, f"{case}: golden drifted (UPDATE_GOLDEN=1 to accept)")

    def test_fixture_01_shape(self):
        seq = td.build_sequence(self.docs["01-downstream-503"])
        names = [p["label"] for p in seq["participants"]]
        self.assertEqual(names, ["api", "payment-service", "customer-service", "customers-db"])
        self.assertEqual([p["type"] for p in seq["participants"]][-1], "database")
        self.assertEqual(len(seq["messages"]), 6)          # three calls, three returns
        self.assertEqual([c["title"] for c in seq["cards"]], ["FACT · trace", "FACT · first span to fail"])
        self.assertEqual([v["label"] for v in seq["meta"]["views"]], ["Request path", "First span to fail"])
        # the error log line the collector joined to the SELECT span rides on its return message
        ret = [m for m in seq["messages"] if m["variant"] == "return" and m["from"] == "customers-db"][0]
        self.assertIn("connection timeout", ret.get("note", ""))
        self.assertTrue(ret["note"].startswith("[error] 14:02:33.031"))

    def test_no_message_is_invented_for_the_root_caller(self):
        for case, doc in self.docs.items():
            seq = td.build_sequence(doc)
            if not seq:
                continue
            root = seq["participants"][0]["id"]
            with self.subTest(case=case):
                self.assertFalse([m for m in seq["messages"] if m["to"] == root and m["variant"] != "return"],
                                 "nothing may call the root service: its caller is not in the trace")
                self.assertTrue(any(a["participant"] == root for a in seq["activations"]))

    def test_y_is_within_schema_and_monotonic(self):
        for case, doc in self.docs.items():
            seq = td.build_sequence(doc)
            if not seq:
                continue
            ys = [m["y"] for m in seq["messages"]]
            self.assertGreaterEqual(min(ys), 160, case)
            self.assertEqual(ys, sorted(ys), case)
            self.assertEqual(len(set(ys)), len(ys), case)
            self.assertGreater(seq["meta"]["viewBox"][1], max(ys), case)

    def test_ids_match_archify_pattern_and_views_use_them(self):
        import re
        for case, doc in self.docs.items():
            seq = td.build_sequence(doc)
            if not seq:
                continue
            ids = {p["id"] for p in seq["participants"]}
            for i in ids:
                self.assertRegex(i, r"^[a-zA-Z][a-zA-Z0-9_-]*$")
            for v in seq["meta"]["views"]:
                self.assertTrue(set(v["focus"]) <= ids, case)
                self.assertLessEqual(len(v["label"]), 48)
                self.assertLessEqual(len(v.get("note", "")), 140)
            for m in seq["messages"]:
                self.assertIn(m["from"], ids); self.assertIn(m["to"], ids)


class EvidenceOnlyTest(unittest.TestCase):
    def test_attributes_outside_the_allowlist_never_reach_the_spec(self):
        cpf = "123.456.789-09"
        spans = [
            span("a1", None, "api", "POST /payment", "SERVER", T0, T0 + 2_000_000_000, True,
                 {"http.status_code": 500, "http.url": f"http://api/payment?cpf={cpf}", "http.request.header.authorization": "Bearer SECRET"}),
            span("a2", "a1", "api", "SELECT customer", "CLIENT", T0 + 1_000_000, T0 + 1_900_000_000, True,
                 {"db.system": "postgresql", "net.peer.name": "customers-db",
                  "db.statement": f"SELECT * FROM customers WHERE cpf = '{cpf}'"},
                 status_message="password=hunter2 rejected"),
        ]
        seq = td.build_sequence(doc_with(spans))
        text = json.dumps(seq, ensure_ascii=False)
        self.assertNotIn(cpf, text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("SELECT * FROM", text)
        self.assertNotIn("hunter2", text)
        self.assertIn("password=***", text)

    def test_redaction_is_the_collectors(self):
        for s in ("token=abc123 x", "Authorization: Bearer zzz", "password=hunter2", "plain"):
            self.assertEqual(td.redact(s), ct.redact(s))

    def test_slug_is_stable_and_valid(self):
        taken = set()
        self.assertEqual(td.slug("payment-service", taken), "payment-service")
        self.assertEqual(td.slug("payment-service", taken), "payment-service-2")
        self.assertEqual(td.slug("api.internal:8080", taken), "api-internal-8080")
        self.assertEqual(td.slug("9front", taken), "p-9front")
        self.assertEqual(td.slug("", taken), "unknown")

    def test_unnamed_outbound_and_missing_parent_are_declared_unknown(self):
        spans = [
            span("r1", None, "api", "GET /x", "SERVER", T0, T0 + 100_000_000),
            span("r2", "r1", "api", "call something", "CLIENT", T0 + 1_000_000, T0 + 50_000_000),  # no peer attribute
            span("r3", "zz", "worker", "orphan", "SERVER", T0 + 2_000_000, T0 + 60_000_000),
        ]
        seq = td.build_sequence(doc_with(spans, missing_parent=["r3"]))
        unknown = [c for c in seq["cards"] if c["title"].startswith("UNKNOWN")][0]
        self.assertIn("1 span(s) whose parent is missing", unknown["items"][0])
        self.assertIn("1 outbound span(s) without a named destination", unknown["items"][1])
        self.assertEqual(seq["messages"], [])

    def test_messaging_spans_use_a_bus_participant_and_dashed_messages(self):
        spans = [
            span("m1", None, "api", "POST /orders", "SERVER", T0, T0 + 30_000_000),
            span("m2", "m1", "api", "orders publish", "PRODUCER", T0 + 1_000_000, T0 + 2_000_000, attrs={"messaging.system": "kafka", "messaging.destination.name": "orders"}),
            span("m3", "m2", "worker", "orders process", "CONSUMER", T0 + 5_000_000, T0 + 25_000_000, attrs={"messaging.system": "kafka", "messaging.destination.name": "orders"}),
        ]
        seq = td.build_sequence(doc_with(spans))
        bus = [p for p in seq["participants"] if p["type"] == "messagebus"]
        self.assertEqual([p["label"] for p in bus], ["orders"])
        self.assertEqual([m["variant"] for m in seq["messages"]], ["dashed", "dashed"])
        self.assertEqual((seq["messages"][0]["from"], seq["messages"][0]["to"]), ("api", "orders"))
        self.assertEqual((seq["messages"][1]["from"], seq["messages"][1]["to"]), ("orders", "worker"))


class CapTest(unittest.TestCase):
    def _wide_trace(self, calls: int, failing: int):
        spans = [span("root", None, "edge", "GET /wide", "SERVER", T0, T0 + calls * 20_000_000)]
        for i in range(calls):
            t = T0 + i * 20_000_000
            err = i == failing
            spans.append(span(f"c{i}", "root", "edge", f"GET svc{i}/x", "CLIENT", t, t + 10_000_000 + i * 1000, err, {"peer.service": f"svc{i}"}))
            spans.append(span(f"s{i}", f"c{i}", f"svc{i}", "GET /x", "SERVER", t + 1_000_000, t + 9_000_000, err))
        return doc_with(spans)

    def test_cap_keeps_the_error_path_and_declares_the_omission(self):
        seq = td.build_sequence(self._wide_trace(calls=30, failing=7), max_messages=40)
        self.assertLessEqual(len(seq["messages"]), 40)
        labels = " ".join(m["label"] for m in seq["messages"])
        self.assertIn("GET svc7/x", labels, "the failing call must survive the cap")
        unknown = [c for c in seq["cards"] if c["title"].startswith("UNKNOWN")][0]
        self.assertIn("20 of 60 messages omitted", " ".join(unknown["items"]))
        ys = [m["y"] for m in seq["messages"]]
        self.assertEqual(ys, sorted(ys))

    def test_under_the_cap_nothing_is_omitted(self):
        seq = td.build_sequence(self._wide_trace(calls=5, failing=2), max_messages=40)
        self.assertEqual(len(seq["messages"]), 10)
        self.assertFalse([c for c in seq["cards"] if "omitted" in " ".join(c["items"])])


class ArchifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)

    def tearDown(self):
        os.environ.clear(); os.environ.update(self.env)
        self.tmp.cleanup()

    def _no_archify(self):
        os.environ["HOME"] = self.tmp.name
        os.environ.pop("ARCHIFY_BIN", None)
        os.environ["PATH"] = self.tmp.name          # neither node nor archify
        os.chdir(self.tmp.name)

    def test_unavailable_says_why_and_never_raises(self):
        self._no_archify()
        st = td.archify_status()
        self.assertFalse(st["available"])
        self.assertIn("node not found", st["reason"])

    def test_env_var_wins_over_discovery(self):
        fake = Path(self.tmp.name) / "archify.mjs"; fake.write_text("// fake")
        os.environ["ARCHIFY_BIN"] = str(fake)
        path, why = td.find_archify()
        self.assertEqual(path, fake); self.assertEqual(why, "found")

    def test_render_skips_without_spans(self):
        r = td.render({"trace_id": "x", "tempo": {"status": "not_found", "spans": []}, "loki": {}, "facts": {}}, Path(self.tmp.name) / "x.html")
        self.assertEqual(r["status"], "skipped")
        self.assertIn("Tempo status is not_found", r["reason"])
        self.assertIn("not generated", td.summary_line(r))

    def test_render_skips_when_archify_is_missing_but_writes_the_spec(self):
        self._no_archify()
        spans = [span("a", None, "api", "GET /", "SERVER", T0, T0 + 1_000_000),
                 span("b", "a", "api", "GET db", "CLIENT", T0, T0 + 500_000, attrs={"peer.service": "db"})]
        out = Path(self.tmp.name) / "t.html"
        r = td.render(doc_with(spans), out)
        self.assertEqual(r["status"], "skipped")
        self.assertTrue(Path(r["sequence"]).is_file(), "the specification is still written for inspection")
        self.assertIsNone(r["html"])

    def test_render_retries_with_standard_and_never_raises(self):
        spans = [span("a", None, "api", "GET /", "SERVER", T0, T0 + 1_000_000),
                 span("b", "a", "api", "GET db", "CLIENT", T0, T0 + 500_000, attrs={"peer.service": "db"})]
        out = Path(self.tmp.name) / "t.html"
        calls = []

        def fake_status(explicit=None):
            return {"available": True, "archify": "/fake/archify.mjs", "node": "v22.0.0", "reason": None}

        def fake_deliver(archify, node, seq_path, html_path, quality):
            calls.append(quality)
            if quality == "showcase":
                return {"ok": False, "error": "composition showcase: 1 error"}
            html_path.write_text("<html>ok</html>")
            return {"ok": True, "receipt": {"validation": {"errors": 0}}}

        old = td.archify_status, td.deliver
        td.archify_status, td.deliver = fake_status, fake_deliver
        try:
            r = td.render(doc_with(spans), out)
        finally:
            td.archify_status, td.deliver = old
        self.assertEqual(calls, ["showcase", "standard"])
        self.assertEqual((r["status"], r["quality"]), ("generated", "standard"))
        self.assertIn("evidence only", td.summary_line(r))

        td.archify_status, td.deliver = fake_status, (lambda *a, **k: {"ok": False, "error": "boom"})
        try:
            r = td.render(doc_with(spans), out)
        finally:
            td.archify_status, td.deliver = old
        self.assertEqual(r["status"], "failed")
        self.assertIn("boom", r["reason"])


class CliTest(unittest.TestCase):
    def test_cli_exit_zero_and_one_line_without_archify(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = collect("01-downstream-503", Path(tmp))
            src = Path(tmp) / "in.json"; src.write_text(json.dumps(doc))
            env = dict(os.environ, HOME=tmp, PATH=tmp); env.pop("ARCHIFY_BIN", None)
            r = subprocess.run([sys.executable, str(SCRIPTS / "trace-diagram.py"), "--input", str(src)],
                               capture_output=True, text=True, timeout=60, env=env, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(r.stdout.startswith("diagram: not generated - "), r.stdout)
            self.assertEqual(len(r.stdout.strip().splitlines()), 1)
            self.assertTrue((Path(tmp) / "in.sequence.json").is_file())

    def test_cli_rejects_missing_input(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "trace-diagram.py"), "--input", "/nonexistent.json"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)

    def test_collector_doctor_reports_the_diagram_renderer(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "collect-trace.py"), "doctor", "--fixture", str(FIXTURES / "01-downstream-503")],
                           capture_output=True, text=True, timeout=60)
        report = json.loads(r.stdout)
        self.assertIn("diagram", report)
        self.assertIn("available", report["diagram"])


if __name__ == "__main__":
    unittest.main()
