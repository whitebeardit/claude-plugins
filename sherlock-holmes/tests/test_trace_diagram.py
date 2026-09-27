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
        ret = [m for m in seq["messages"] if m["variant"] == "security" and m["from"] == "customers-db"][0]
        self.assertIn("connection timeout", ret.get("note", ""))
        self.assertTrue(ret["note"].startswith("[error] 14:02:33.031"))

    def test_no_message_is_invented_for_the_root_caller(self):
        for case, doc in self.docs.items():
            seq = td.build_sequence(doc)
            if not seq:
                continue
            root = seq["participants"][0]["id"]
            with self.subTest(case=case):
                self.assertFalse([m for m in seq["messages"] if m["to"] == root and m["variant"] not in ("return", "security", "emphasis")],
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


class RealTraceShapesTest(unittest.TestCase):
    """Shapes met on the first real trace (an AWS service, 2026-09-27) that the fixtures lack."""

    def test_long_hostname_is_split_into_label_and_sublabel(self):
        spans = [span("r", None, "svc", "GET /x", "SERVER", T0, T0 + 50_000_000),
                 span("c", "r", "svc", "POST", "CLIENT", T0 + 1_000_000, T0 + 9_000_000,
                      attrs={"net.peer.name": "dynamodb.us-east-2.amazonaws.com", "http.status_code": 200})]
        seq = td.build_sequence(doc_with(spans))
        p = [x for x in seq["participants"] if x["id"] != "svc"][0]
        self.assertEqual((p["label"], p["sublabel"]), ("dynamodb", "us-east-2.amazonaws.com"))
        for x in seq["participants"]:
            self.assertLessEqual(len(x["label"]), td.PARTICIPANT_CHARS)

    def test_rpc_service_names_the_callee_and_nested_http_span_is_folded(self):
        # AWS SDK: DynamoDB.GetItem (rpc.*, no host) → child HTTP CLIENT span with the host.
        spans = [span("r", None, "svc", "GET /x", "SERVER", T0, T0 + 50_000_000),
                 span("op", "r", "svc", "DynamoDB.GetItem", "CLIENT", T0 + 1_000_000, T0 + 9_000_000,
                      attrs={"rpc.system": "aws-api", "rpc.service": "DynamoDB", "rpc.method": "GetItem",
                             "db.system": "dynamodb", "db.operation": "GetItem", "http.status_code": 200}),
                 span("http", "op", "svc", "POST", "CLIENT", T0 + 2_000_000, T0 + 8_000_000,
                      attrs={"net.peer.name": "dynamodb.us-east-2.amazonaws.com", "http.status_code": 200})]
        seq = td.build_sequence(doc_with(spans))
        callee = [x for x in seq["participants"] if x["id"] != "svc"]
        self.assertEqual(len(callee), 1, "one callee, not one per layer")
        self.assertEqual((callee[0]["label"], callee[0]["type"], callee[0]["sublabel"]), ("DynamoDB", "database", "dynamodb"))
        self.assertEqual(len(seq["messages"]), 2, "the SDK call and its return; the HTTP layer is folded")
        self.assertIn("DynamoDB.GetItem", seq["messages"][0]["label"])
        fact = [c for c in seq["cards"] if c["title"] == "FACT · not drawn, on purpose"][0]
        self.assertIn("1 nested outbound span(s) folded", fact["items"][0])
        self.assertFalse([c for c in seq["cards"] if c["title"].startswith("UNKNOWN")], "folded is not unknown")

    def test_canvas_widens_with_the_participant_count(self):
        # archify shares the viewBox among participants; 5 of them in 900px left a 131px box
        # where even "data-enrichment-service" did not fit (real trace, 2026-09-27).
        spans = [span("r", None, "data-enrichment-service", "consume", "SERVER", T0, T0 + 50_000_000)]
        for i, peer in enumerate(["DynamoDB", "Data-Enrichment-BaseGlobal.fifo", "mostqiapi.com", "cache-service"]):
            spans.append(span(f"c{i}", "r", "data-enrichment-service", f"call {i}", "CLIENT", T0 + i * 1_000_000, T0 + i * 1_000_000 + 500_000,
                              attrs={"peer.service": peer}))
        seq = td.build_sequence(doc_with(spans))
        self.assertEqual(len(seq["participants"]), 5)
        self.assertEqual(seq["meta"]["viewBox"][0], 5 * td.PARTICIPANT_WIDTH)          # 1050: the showcase ceiling
        self.assertLessEqual(seq["meta"]["viewBox"][0], td.SHOWCASE_MAX_WIDTH)
        for p in seq["participants"]:
            self.assertLessEqual(len(p["label"]), td.PARTICIPANT_CHARS)
        self.assertEqual(seq["participants"][0]["label"], "data-enrichment-service", "23 chars fit a 163px box")
        compact = td.build_sequence(doc_with(spans), compact=True)
        self.assertGreater(compact["meta"]["viewBox"][0], seq["meta"]["viewBox"][0])
        for p in compact["participants"]:
            self.assertLessEqual(len(p["label"]), td.COMPACT_CHARS)

    def test_canvas_rule_measured_on_archify(self):
        # four participants keep the 900px canvas (goldens unchanged); five hit the showcase ceiling;
        # beyond that the canvas stays at the ceiling and the label cap shrinks with the box.
        self.assertEqual(td.canvas_and_label_cap(3), (900, 24))
        self.assertEqual(td.canvas_and_label_cap(4), (900, 24))
        self.assertEqual(td.canvas_and_label_cap(5), (1050, 23))
        width6, chars6 = td.canvas_and_label_cap(6)
        self.assertEqual(width6, 1050)
        self.assertLess(chars6, 23)
        self.assertGreaterEqual(chars6, td.COMPACT_CHARS)
        self.assertEqual(td.canvas_and_label_cap(2, compact=True), (1350, td.COMPACT_CHARS))

    def test_http_span_nested_in_a_queue_send_is_folded(self):
        spans = [span("r", None, "svc", "handle", "SERVER", T0, T0 + 50_000_000),
                 span("send", "r", "svc", "orders.fifo send", "PRODUCER", T0 + 1_000_000, T0 + 9_000_000,
                      attrs={"messaging.system": "aws_sqs", "messaging.destination.name": "orders.fifo"}),
                 span("http", "send", "svc", "POST", "CLIENT", T0 + 2_000_000, T0 + 8_000_000,
                      attrs={"net.peer.name": "sqs.us-east-2.amazonaws.com", "http.status_code": 200})]
        seq = td.build_sequence(doc_with(spans))
        self.assertEqual([p["label"] for p in seq["participants"]], ["svc", "orders.fifo"])
        self.assertEqual(len(seq["messages"]), 1)

    def test_db_system_alone_still_names_a_database(self):
        spans = [span("r", None, "svc", "GET /x", "SERVER", T0, T0 + 50_000_000),
                 span("q", "r", "svc", "SELECT users", "CLIENT", T0 + 1_000_000, T0 + 9_000_000, attrs={"db.system": "postgresql"})]
        seq = td.build_sequence(doc_with(spans))
        self.assertEqual([(x["label"], x["type"]) for x in seq["participants"]][1], ("postgresql", "database"))


class ErrorSemanticsTest(unittest.TestCase):
    """D23: the outcome carries the colour, the call stays neutral; everything comes from the span."""

    def _pair(self, code, error=False, child_error=None):
        spans = [span("r", None, "edge", "GET /x", "SERVER", T0, T0 + 50_000_000),
                 span("c", "r", "edge", "GET svc/y", "CLIENT", T0 + 1_000_000, T0 + 40_000_000, error,
                      {"peer.service": "svc", "http.status_code": code}),
                 span("s", "c", "svc", "GET /y", "SERVER", T0 + 2_000_000, T0 + 39_000_000,
                      error if child_error is None else child_error, {"http.status_code": code})]
        return td.build_sequence(doc_with(spans))

    def test_outcome_classes(self):
        self.assertEqual(td.outcome_variant(False, "200"), "return")
        self.assertEqual(td.outcome_variant(False, "503"), "security", "a 5xx is an error even without error status")
        self.assertEqual(td.outcome_variant(False, "404"), "emphasis")
        self.assertEqual(td.outcome_variant(True, "404"), "security", "recorded error status wins")
        self.assertEqual(td.outcome_variant(True, None), "security")
        self.assertEqual(td.outcome_variant(False, "nil"), "return")

    def test_call_is_neutral_and_the_return_is_coloured(self):
        for code, error, want in (("200", False, "return"), ("502", True, "security"), ("404", False, "emphasis")):
            with self.subTest(code=code):
                seq = self._pair(code, error)
                call, ret = seq["messages"]
                self.assertEqual(call["variant"], "default")
                self.assertEqual(ret["variant"], want)

    def test_server_span_with_error_status_gets_a_red_activation(self):
        seq = self._pair("500", error=True)
        types = {a["participant"]: a["type"] for a in seq["activations"]}
        self.assertEqual(types["svc"], "security")
        ok = self._pair("200")
        self.assertTrue(all(a["type"] == "backend" for a in ok["activations"]))

    def test_legend_names_the_colours_and_first_failure_card_is_rose(self):
        seq = self._pair("503", error=True)
        self.assertEqual(seq["meta"]["legend"]["mode"], "auto")
        self.assertEqual(seq["meta"]["legend"]["entries"]["security"]["label"], "5xx / error")
        doc = doc_with([span("r", None, "edge", "GET /x", "SERVER", T0, T0 + 5_000_000, True)])
        doc["facts"] = {"earliest_error_signals": [{"kind": "span_first_to_fail", "service": "edge", "operation": "GET /x",
                                                    "timestamp": ct.iso(T0), "ended": ct.iso(T0 + 5_000_000), "duration_ms": 5.0, "summary": "boom"}]}
        spans = doc["tempo"]["spans"] + [span("c", "r", "edge", "GET db", "CLIENT", T0, T0 + 1_000_000, attrs={"db.system": "postgresql"})]
        doc["tempo"]["spans"] = spans
        seq = td.build_sequence(doc)
        card = [c for c in seq["cards"] if c["title"] == "FACT · first span to fail"][0]
        self.assertEqual(card["dot"], "rose")

    def test_fixture_01_red_path(self):
        seq = td.build_sequence(collect("01-downstream-503", Path(tempfile.mkdtemp())))
        self.assertEqual([m["variant"] for m in seq["messages"]], ["default", "default", "default", "security", "security", "security"])
        red = {a["participant"] for a in seq["activations"] if a["type"] == "security"}
        self.assertEqual(red, {"api", "payment-service", "customer-service"}, "every server span with error status, root included")


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
        policy = [c for c in seq["cards"] if c["title"] == "FACT · not drawn, on purpose"][0]
        self.assertIn("20 of 60 messages omitted", " ".join(policy["items"]))
        self.assertFalse([c for c in seq["cards"] if c["title"].startswith("UNKNOWN")], "omission is a policy, not an unknown")
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
        self.assertEqual((r["status"], r["quality"], r["compact"]), ("generated", "standard", False))
        self.assertIn("evidence only", td.summary_line(r))

        # third attempt: the compact spec (shorter labels, wider canvas) when standard also fails
        calls.clear()
        def deliver_compact_only(archify, node, seq_path, html_path, quality):
            calls.append(quality)
            spec = json.loads(seq_path.read_text())
            if spec["meta"]["viewBox"][0] <= td.VIEWBOX_WIDTH:
                return {"ok": False, "error": "layout/constraint: label wider than the participant box"}
            html_path.write_text("<html>ok</html>")
            return {"ok": True, "receipt": {"validation": {"errors": 0}}}
        td.archify_status, td.deliver = fake_status, deliver_compact_only
        try:
            r = td.render(doc_with(spans), out)
        finally:
            td.archify_status, td.deliver = old
        self.assertEqual(calls, ["showcase", "standard", "standard"])
        self.assertEqual((r["status"], r["compact"]), ("generated", True))

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
