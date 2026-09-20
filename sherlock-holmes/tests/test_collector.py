"""Unit tests for collect-trace.py. Standard library only: python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "trace-debug" / "scripts" / "collect-trace.py"
spec = importlib.util.spec_from_file_location("collect_trace", SCRIPT)
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

TID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_A = "00f067aa0ba902b7"
SPAN_B = "b7ad6b7169203331"
T0 = 1789653751024000000  # 2026-09-17T14:02:31.024Z


def b64(h: str) -> str:
    return base64.b64encode(bytes.fromhex(h)).decode()


def log(level, msg, service, span, **extra):
    d = {"level": level, "msg": msg, "trace_id": TID, "span_id": span, "service": service}
    d.update(extra)
    return json.dumps(d)


def loki_doc(streams):
    return {"status": "success", "data": {"resultType": "streams", "result": streams, "stats": {}}}


def tempo_doc():
    return {"batches": [
        {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]},
         "scopeSpans": [{"scope": {"name": "x"}, "spans": [{
             "traceId": b64(TID), "spanId": b64(SPAN_A), "name": "POST /payment", "kind": "SPAN_KIND_SERVER",
             "startTimeUnixNano": str(T0), "endTimeUnixNano": str(T0 + 2_500_000_000),
             "attributes": [{"key": "http.status_code", "value": {"intValue": "500"}},
                            {"key": "custom.noise", "value": {"stringValue": "x"}}],
             "status": {"code": "STATUS_CODE_ERROR", "message": "upstream failure"}}]}]},
        {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "customer-service"}}]},
         "scopeSpans": [{"scope": {"name": "x"}, "spans": [{
             "traceId": b64(TID), "spanId": b64(SPAN_B), "parentSpanId": b64(SPAN_A), "name": "SELECT customer",
             "kind": 3, "startTimeUnixNano": str(T0 + 100_000_000), "endTimeUnixNano": str(T0 + 2_100_000_000),
             "attributes": [{"key": "db.system", "value": {"stringValue": "postgresql"}}],
             "status": {"code": 2, "message": "timeout"},
             "events": [{"timeUnixNano": str(T0 + 2_100_000_000), "name": "exception", "attributes": [
                 {"key": "exception.type", "value": {"stringValue": "TimeoutError"}},
                 {"key": "exception.message", "value": {"stringValue": "pool wait exceeded"}}]}]}]}]},
    ]}


def sample_streams():
    return [
        {"stream": {"service_name": "api", "env": "prod"}, "values": [
            [str(T0), log("info", "POST /payment", "api", SPAN_A)],
            [str(T0 + 217_000_000), log("error", "HTTP 500", "api", SPAN_A, **{"http.status_code": 500})]]},
        {"stream": {"service_name": "customer-service", "env": "prod"}, "values": [
            [str(T0 + 119_000_000), log("error", "PostgreSQL connection timeout after 2000ms", "customer-service", SPAN_B, exception="TimeoutError: pool wait")],
            [str(T0 + 120_000_000), log("error", "PostgreSQL connection timeout after 2000ms", "customer-service", SPAN_B, exception="TimeoutError: pool wait")],
            [str(T0 + 123_000_000), log("warn", "HTTP 503", "customer-service", SPAN_B)]]},
    ]


def write_fixture(tmp: Path, with_tempo=True):
    (tmp / "loki.json").write_text(json.dumps(loki_doc(sample_streams())))
    if with_tempo:
        (tmp / "tempo.json").write_text(json.dumps(tempo_doc()))


def run_main(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ct.main(args)
    return code, buf.getvalue()


class TraceIdTests(unittest.TestCase):
    def test_accepts_32_and_16_hex(self):
        self.assertEqual(ct.normalise_trace_id(TID.upper()), TID)
        self.assertEqual(ct.normalise_trace_id(SPAN_A), SPAN_A)

    def test_accepts_traceparent(self):
        self.assertEqual(ct.normalise_trace_id(f"00-{TID}-{SPAN_A}-01"), TID)

    def test_rejects_numeric_document_like_input(self):
        with self.assertRaises(ValueError) as cm:
            ct.normalise_trace_id("12345678901")
        self.assertIn("not a trace id", str(cm.exception))

    def test_rejects_garbage(self):
        for bad in ("", "zz", "4bf9 2f35", "'; drop", "4bf92f3577b34da6a3ce929d0e0e47361"):
            with self.assertRaises(ValueError):
                ct.normalise_trace_id(bad)


class ParsingHelpersTests(unittest.TestCase):
    def test_norm_id_base64_and_hex(self):
        self.assertEqual(ct.norm_id(b64(SPAN_A), 8), SPAN_A)
        self.assertEqual(ct.norm_id(SPAN_A.upper(), 8), SPAN_A)
        self.assertIsNone(ct.norm_id("", 8))

    def test_durations_and_times(self):
        self.assertEqual(ct.parse_duration("2h"), 7200)
        self.assertEqual(ct.parse_duration("30s"), 30)
        with self.assertRaises(ValueError):
            ct.parse_duration("2 weeks")
        self.assertEqual(ct.parse_time("2026-09-17T14:02:31.024Z"), T0)
        self.assertEqual(ct.parse_time(str(T0 // 1_000_000_000)), (T0 // 1_000_000_000) * 1_000_000_000)
        self.assertEqual(ct.parse_time(str(T0 // 1_000_000)), (T0 // 1_000_000) * 1_000_000)
        self.assertEqual(ct.parse_time(str(T0)), T0)
        self.assertEqual(ct.iso(T0), "2026-09-17T14:02:31.024Z")

    def test_logql_modes(self):
        sel = '{env="prod"}'
        self.assertEqual(ct.build_logql(sel, TID, "substring", "trace_id"), f'{sel} |= "{TID}"')
        self.assertEqual(ct.build_logql(sel, TID, "metadata", "trace_id"), f'{sel} | trace_id="{TID}"')
        self.assertEqual(ct.build_logql(sel, TID, "json", "traceId"), f'{sel} | json | traceId="{TID}"')
        with self.assertRaises(ValueError):
            ct.build_logql('env="prod"', TID, "substring", "trace_id")

    def test_log_line_json_and_text(self):
        labels = {"service_name": "api"}
        ev = ct.parse_log_line(T0, log("ERROR", "boom", "ignored", SPAN_A, exception="Trace...", extra="v"), labels, ["service_name"], 100)
        self.assertEqual((ev["service"], ev["level"], ev["message"], ev["span_id"]), ("api", "error", "boom", SPAN_A))
        self.assertEqual(ev["fields"], {"extra": "v"})
        self.assertEqual(ev["format"], "json")
        ev2 = ct.parse_log_line(T0, "2026-09-17 14:02:31 WARN something odd span_id=" + SPAN_B, {"app": "web"}, ["service_name", "app"], 100)
        self.assertEqual((ev2["service"], ev2["level"], ev2["span_id"], ev2["format"]), ("web", "warn", SPAN_B, "text"))
        ev3 = ct.parse_log_line(T0, json.dumps({"level": 50, "message": "pino style"}), {}, ["service_name"], 100)
        self.assertEqual((ev3["level"], ev3["service"]), ("error", "unknown"))

    def test_redaction_and_truncation(self):
        self.assertIn("token=***", ct.redact("token=abc123 rest"))
        self.assertEqual(len(ct.truncate("x" * 50, 10)), 10)

    def test_dedupe_counts_repeats(self):
        evs = [ct.parse_log_line(T0 + i, log("error", "same", "svc", SPAN_A), {"service_name": "svc"}, ["service_name"], 100) for i in range(3)]
        evs.append(ct.parse_log_line(T0 + 9, log("info", "other", "svc", SPAN_A), {"service_name": "svc"}, ["service_name"], 100))
        out = ct.dedupe(evs)
        self.assertEqual([e["repeat"] for e in out], [3, 1])


class OtlpTests(unittest.TestCase):
    def test_parse_and_tree(self):
        spans = ct.parse_otlp(tempo_doc(), keep_all_attrs=False, max_chars=200)
        self.assertEqual(len(spans), 2)
        tree = ct.derive_tree(spans)
        by_id = {s["span_id"]: s for s in tree["ordered"]}
        self.assertEqual(tree["root_span"], SPAN_A)
        self.assertEqual(by_id[SPAN_B]["depth"], 1)
        self.assertEqual(by_id[SPAN_B]["kind"], "CLIENT")
        self.assertEqual(by_id[SPAN_B]["status"], "ERROR")
        self.assertEqual(by_id[SPAN_B]["exception"]["type"], "TimeoutError")
        self.assertEqual(by_id[SPAN_A]["attributes"], {"http.status_code": 500})
        self.assertEqual(by_id[SPAN_A]["attribute_keys_total"], 2)
        self.assertEqual(tree["spans_missing_parent"], [])

    def test_missing_parent_is_reported(self):
        doc = tempo_doc()
        doc["batches"][1]["scopeSpans"][0]["spans"][0]["parentSpanId"] = b64("aaaaaaaaaaaaaaaa")
        tree = ct.derive_tree(ct.parse_otlp(doc, False, 200))
        self.assertEqual(tree["spans_missing_parent"], [SPAN_B])

    def test_v2_shape(self):
        doc = {"trace": {"resourceSpans": tempo_doc()["batches"]}}
        self.assertEqual(len(ct.parse_otlp(doc, False, 200)), 2)


class EndToEndFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "out.json"

    def test_window_bounded_by_tempo_and_join(self):
        write_fixture(self.tmp)
        code, text = run_main(["--trace-id", TID, "--fixture", str(self.tmp), "--format", "json", "--out", str(self.out)])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["window"]["source"], "tempo")
        self.assertEqual(doc["window"]["start"], "2026-09-17T14:02:01.024Z")
        self.assertEqual(doc["tempo"]["status"], "found")
        self.assertEqual(doc["loki"]["returned"], 5)
        self.assertEqual(doc["loki"]["events"][1]["repeat"], 2)
        self.assertEqual(doc["loki"]["events"][1]["span"]["operation"], "SELECT customer")
        self.assertEqual(doc["facts"]["services"], ["api", "customer-service"])
        first = doc["facts"]["earliest_error_signals"][0]
        self.assertEqual((first["kind"], first["service"]), ("log", "customer-service"))
        self.assertTrue(self.out.exists())

    def test_tempo_missing_means_not_found_and_window_now(self):
        write_fixture(self.tmp, with_tempo=False)
        code, text = run_main(["--trace-id", TID, "--fixture", str(self.tmp), "--format", "json", "--out", str(self.out)])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["tempo"]["status"], "not_found")
        self.assertEqual(doc["window"]["source"], "now")
        self.assertIn("note", doc["window"])

    def test_around_bounds_window_explicitly(self):
        write_fixture(self.tmp, with_tempo=False)
        code, text = run_main(["--trace-id", TID, "--fixture", str(self.tmp), "--format", "json", "--out", str(self.out),
                               "--around", "2026-09-17T14:02:31Z", "--lookback", "1h"])
        doc = json.loads(text)
        self.assertEqual(doc["window"]["source"], "around")
        self.assertEqual(doc["window"]["start"], "2026-09-17T13:32:31.000Z")

    def test_truncation_is_explicit(self):
        write_fixture(self.tmp)
        code, text = run_main(["--trace-id", TID, "--fixture", str(self.tmp), "--format", "json", "--out", str(self.out), "--max-lines", "2"])
        doc = json.loads(text)
        self.assertTrue(doc["loki"]["truncated"])
        self.assertEqual(doc["loki"]["returned"], 2)
        code, prompt = run_main(["--trace-id", TID, "--fixture", str(self.tmp), "--out", str(self.out), "--max-lines", "2"])
        self.assertIn("TRUNCATED", prompt)
        self.assertIn("absence of later lines is NOT evidence", prompt)

    def test_prompt_cut_mentions_sources_and_tree(self):
        write_fixture(self.tmp)
        code, prompt = run_main(["--trace-id", TID, "--fixture", str(self.tmp), "--out", str(self.out)])
        self.assertIn("SPAN TREE (2 spans", prompt)
        self.assertIn("<span customer-service:SELECT customer>", prompt)
        self.assertIn("(x2)", prompt)
        self.assertIn("FULL JSON:", prompt)

    def test_invalid_id_exit_2(self):
        code, _ = run_main(["--trace-id", "not-a-trace", "--fixture", str(self.tmp)])
        self.assertEqual(code, 2)

    def test_missing_selector_without_fixture_refuses_to_query_everything(self):
        env = {k: v for k, v in os.environ.items()}
        for k in list(os.environ):
            if k.startswith(("LOKI_", "TEMPO_", "GRAFANA_")):
                del os.environ[k]
        os.environ["LOKI_URL"] = "http://loki.invalid:3100"
        os.environ["TEMPO_URL"] = "http://tempo.invalid:3200"
        try:
            code, text = run_main(["--trace-id", TID, "--source", "loki", "--format", "json", "--out", str(self.out), "--timeout", "1"])
            doc = json.loads(text)
            self.assertEqual(doc["loki"]["status"], "error")
            self.assertIn("LOKI_SELECTOR", doc["loki"]["error"])
            self.assertEqual(code, 1)
        finally:
            os.environ.clear()
            os.environ.update(env)


if __name__ == "__main__":
    unittest.main()
