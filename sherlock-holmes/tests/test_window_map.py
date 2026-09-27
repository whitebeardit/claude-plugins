"""Unit tests for window-map.py. Standard library only, no network, no archify.

What these protect:
  - evidence only: every arrow is a call some span recorded; the caller of a root span is counted,
    never drawn; an error inside a service with no call is declared, not hidden;
  - the specification for the two window fixtures (goldens, validated against archify in CI);
  - the sample, the fold of nested outbound spans, "nil" status codes and missing traces are declared;
  - a map never fails the sweep: skipped/failed outcomes exit 0 with a one-line reason.
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
SCRIPT = ROOT / "skills" / "trace-debug" / "scripts" / "window-map.py"
FIXTURES = ROOT / "skills" / "error-sweep" / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"
spec = importlib.util.spec_from_file_location("window_map", SCRIPT)
wm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wm)
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith(("GRAFANA_", "LOKI_", "TEMPO_", "ARCHIFY_"))}


def build(case: str, sample: int = 50, drop: tuple = ()):
    fixture = FIXTURES / case
    window = json.loads((fixture / "window.json").read_text())
    w = {"start_ns": wm.ct.parse_time(window["start"]), "end_ns": wm.ct.parse_time(window["end"]),
         "start": wm.sw.iso_ms(wm.ct.parse_time(window["start"])), "end": wm.sw.iso_ms(wm.ct.parse_time(window["end"]))}
    doc = json.loads((fixture / "tempo-search.json").read_text())
    ids, total = wm.sample_trace_ids(doc, sample)
    ep = {"mode": "none"}
    with tempfile.TemporaryDirectory() as tmp:
        # a fixture copy without some traces simulates Tempo not returning them
        fdir = fixture
        if drop:
            import shutil
            fdir = Path(tmp) / case
            shutil.copytree(fixture, fdir)
            for tid in drop:
                shutil.rmtree(fdir / "traces" / tid)
        found, missing = wm.fetch_traces(ep, ids, w, 5, fdir)
    agg, stats = {}, {"entries": {}, "folded": 0, "unnamed": 0, "errored_services": set()}
    for tid, t in found:
        wm.calls_of(tid, t, agg, stats)
    return wm.build_map(agg, stats, w, total, len(found), missing), stats


class GoldenTest(unittest.TestCase):
    def test_goldens(self):
        update = os.environ.get("UPDATE_GOLDEN") == "1"
        for case in ("cascade", "timeouts"):
            with self.subTest(case=case):
                seq, _ = build(case)
                text = json.dumps(seq, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
                path = GOLDEN / f"map-{case}.sequence.json"
                if update:
                    path.write_text(text, encoding="utf-8")
                self.assertEqual(path.read_text(encoding="utf-8"), text, f"{case}: golden drifted (UPDATE_GOLDEN=1 to accept)")


class EvidenceOnlyTest(unittest.TestCase):
    def test_cascade_arrows_are_the_recorded_calls(self):
        seq, stats = build("cascade")
        labels = [m["label"] for m in seq["messages"]]
        self.assertEqual(labels, ["4× POST payment-service/charge · 4 err 502",
                                  "4× GET customer-service/customers/<N> · 4 err 503",
                                  "4× SELECT customer · 4 err"])
        self.assertEqual([p["label"] for p in seq["participants"]], ["api", "payment-service", "customer-service", "customers-db"])
        root = seq["participants"][0]["id"]
        self.assertFalse([m for m in seq["messages"] if m["to"] == root], "the root's caller is not in the traces: no arrow into it")
        self.assertEqual(stats["entries"], {"api": 4, "inventory-service": 1})
        unknown = " ".join(" ".join(c["items"]) for c in seq["cards"] if c["title"].startswith("UNKNOWN"))
        self.assertIn("errors inside a service, no call recorded: inventory-service", unknown)
        self.assertTrue(all(m["y"] >= 160 for m in seq["messages"]))

    def test_timeouts_fold_sdk_http_and_drop_nil_codes(self):
        seq, stats = build("timeouts")
        labels = [m["label"] for m in seq["messages"]]
        self.assertEqual(labels, ["3× orders process", "6× DynamoDB.GetItem", "3× POST · 3 err"])
        self.assertEqual(stats["folded"], 6)
        self.assertEqual([m["variant"] for m in seq["messages"]], ["dashed", "default", "emphasis"])
        self.assertEqual(seq["participants"][0]["type"], "messagebus", "a queue that delivers is drawn as the sender")
        self.assertNotIn("nil", " ".join(labels))

    def test_sample_and_missing_traces_are_declared(self):
        seq, _ = build("cascade", sample=2)
        policy = " ".join(" ".join(c["items"]) for c in seq["cards"] if c["title"] == "FACT · not drawn, on purpose")
        self.assertIn("sample: 2 newest of 5 error traces", policy)
        seq, _ = build("cascade", drop=("5b1e0c2a9f7d4e3b8a6c1d2e3f4a5b6c",))
        unknown = " ".join(" ".join(c["items"]) for c in seq["cards"] if c["title"].startswith("UNKNOWN"))
        self.assertIn("1 sampled trace(s) not returned by Tempo", unknown)
        self.assertTrue(seq["messages"][0]["label"].startswith("3× "), "counts come only from traces that were returned")

    def test_arrow_cap_keeps_errors_first(self):
        agg = {("svc", f"dep{i}", f"GET /x{i}"): {"kind": "call", "ptype": "backend", "sub": None, "count": 1,
                                                  "errors": 1 if i == 45 else 0, "codes": {}, "traces": {"t"}, "first_ns": 1_000 + i}
               for i in range(50)}
        stats = {"entries": {}, "folded": 0, "unnamed": 0, "errored_services": set()}
        w = {"start": "2026-09-17T13:00:00.000Z", "end": "2026-09-17T14:00:00.000Z"}
        seq = wm.build_map(agg, stats, w, 1, 1, [])
        self.assertEqual(len(seq["messages"]), wm.MAX_ARROWS)
        self.assertTrue(any("err" in m["label"] for m in seq["messages"]), "the arrow with an error survives the cap")
        policy = " ".join(" ".join(c["items"]) for c in seq["cards"] if c["title"] == "FACT · not drawn, on purpose")
        self.assertIn("10 call kind(s) omitted", policy)


class SummaryTest(unittest.TestCase):
    def test_summary_carries_arrows_unknowns_and_the_reading_note(self):
        seq, _ = build("cascade")
        names = {p["id"]: p["label"] for p in seq["participants"]}
        r = {"status": "generated", "html": "/tmp/x.map.html", "arrows": len(seq["messages"]), "drawn_traces": 5,
             "quality": "showcase",
             "arrow_lines": [f"{names[m['from']]} → {names[m['to']]}   {m['label']}" for m in seq["messages"]],
             "not_in_map": [i for c in seq["cards"] if c["title"].startswith("UNKNOWN") for i in c["items"]], "on_purpose": []}
        text = wm.summary_line(r)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("map: generated /tmp/x.map.html (3 kind(s) of call from 5 trace(s)"))
        self.assertEqual(lines[1], "  1. api → payment-service   4× POST payment-service/charge · 4 err 502")
        self.assertIn("errors inside a service, no call recorded: inventory-service", text)
        self.assertIn("rows outnumber arrows", lines[-1])
        self.assertEqual(wm.summary_line({"status": "skipped", "reason": "x"}), "map: not generated - x")


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(CLEAN_ENV, HOME=tmp, PATH=tmp)       # no node, no archify
            return subprocess.run([sys.executable, str(SCRIPT), *args, "--out", str(Path(tmp) / "m.html")],
                                  capture_output=True, text=True, timeout=60, env=env, cwd=tmp)

    def test_without_archify_says_why_and_exits_0(self):
        r = self.run_cli("--fixture", str(FIXTURES / "cascade"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("map: not generated - "), r.stdout)

    def test_silence_has_nothing_to_map(self):
        r = self.run_cli("--fixture", str(FIXTURES / "silence"))
        self.assertEqual(r.returncode, 0)
        self.assertIn("no trace with an error span in this window", r.stdout)

    def test_bad_arguments_exit_2(self):
        self.assertEqual(self.run_cli("--fixture", str(FIXTURES / "cascade"), "--service", 'x" || true').returncode, 2)
        self.assertEqual(self.run_cli("--fixture", str(FIXTURES / "cascade"), "--sample", "0").returncode, 2)
        self.assertEqual(self.run_cli().returncode, 2, "no window")


if __name__ == "__main__":
    unittest.main()
