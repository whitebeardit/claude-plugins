#!/usr/bin/env python3
"""Hour-of-day profile of CloudWatch metrics (read-only).

Usage:
  python3 -I cw_hod.py --profile PROFILE [--region us-east-1] [--days 14] [--tz -3] SPECS_JSON

SPECS_JSON is a JSON list; each item:
  {"ns":"AWS/AutoScaling","metric":"GroupInServiceInstances",
   "dims":{"AutoScalingGroupName":"<asg-name>"},"stat":"Average",
   "label":"prod ng-apps nodes","fmt":"%.1f","scale":1}

Prints, per metric: n, min/avg/max and a 24-row table (all / weekday / weekend)
in the requested local timezone. Why this exists: the AWS CLI prints timestamps in
the machine's local zone (e.g. -03:00), so naive "subtract 3h" code double-shifts.
This script parses the offset and converts explicitly. Calls `aws cloudwatch
get-metric-data` through subprocess so command-line wrappers do not mangle JSON.
"""
import argparse, collections, datetime, json, subprocess, sys

def gmd(profile, region, spec, days):
    end = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - datetime.timedelta(days=days)
    q = [{"Id": "m1", "MetricStat": {"Metric": {"Namespace": spec["ns"], "MetricName": spec["metric"],
          "Dimensions": [{"Name": k, "Value": v} for k, v in spec["dims"].items()]},
          "Period": spec.get("period", 3600), "Stat": spec.get("stat", "Average")}, "ReturnData": True}]
    cmd = ["aws", "cloudwatch", "get-metric-data", "--profile", profile, "--region", region,
           "--metric-data-queries", json.dumps(q), "--start-time", start.isoformat().replace("+00:00", "Z"),
           "--end-time", end.isoformat().replace("+00:00", "Z"), "--output", "json", "--no-cli-pager"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode:
        print("ERR", spec.get("label"), out.stderr[:300]); return []
    r = json.loads(out.stdout)["MetricDataResults"][0]
    return sorted((datetime.datetime.fromisoformat(t.replace("Z", "+00:00")), v) for t, v in zip(r["Timestamps"], r["Values"]))

def profile(pts, label, fmt, tz, scale):
    if not pts:
        print(f"\n## {label}: no datapoints (check dimensions with `aws cloudwatch list-metrics`)"); return
    zone = datetime.timezone(datetime.timedelta(hours=tz))
    byh, wk, we = (collections.defaultdict(list) for _ in range(3))
    for t, v in pts:
        lt = t.astimezone(zone); v *= scale
        byh[lt.hour].append(v); (we if lt.weekday() >= 5 else wk)[lt.hour].append(v)
    vals = [v * scale for _, v in pts]
    print(f"\n## {label}  (n={len(pts)}, min={fmt % min(vals)}, avg={fmt % (sum(vals)/len(vals))}, max={fmt % max(vals)})")
    print(f"h(UTC{tz:+d})   all   wkday  wkend")
    avg = lambda l: fmt % (sum(l)/len(l)) if l else "-"
    for h in range(24):
        print("%02d  %7s %7s %7s" % (h, avg(byh[h]), avg(wk[h]), avg(we[h])))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True); ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--days", type=int, default=14); ap.add_argument("--tz", type=int, default=-3)
    ap.add_argument("specs")
    a = ap.parse_args()
    specs = json.load(open(a.specs)) if a.specs.endswith(".json") else json.loads(a.specs)
    for spec in specs:
        profile(gmd(a.profile, a.region, spec, a.days), spec["label"], spec.get("fmt", "%.1f"), a.tz, spec.get("scale", 1))
