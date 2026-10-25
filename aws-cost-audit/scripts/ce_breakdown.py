#!/usr/bin/env python3
"""Cost Explorer breakdowns for a cost audit (read-only).

Usage:
  python3 -I ce_breakdown.py --profile PAYER_PROFILE [--start YYYY-MM-DD --end YYYY-MM-DD] CMD [args]

Commands:
  services                 cost by SERVICE (top 20) + total
  accounts                 cost by LINKED_ACCOUNT
  usage "SERVICE NAME"     cost + usage quantity by USAGE_TYPE for one service
  regional                 DataTransfer-Regional-Bytes by OPERATION (InterZone vs PublicIP) and account
  regional-trend           weekly GB/day by OPERATION since --start
  sp                       Savings Plans list, daily utilization and coverage
  marketplace              monthly cost of marketplace vendors and managed data services
  map                      the bill classified into fixed categories (Compute, Network, Data & databases,
                           Storage, SaaS & Marketplace, Commitments, Observability, Security, Tax, Other)
                           + top lines; prints the JSON arrays the report template expects (comp / lines).
                           Works for any account, with or without Kubernetes.

Why: Cost Explorer run from the payer account sees every linked account; from a
member account it sees only itself. MONTHLY granularity over a range that crosses a
month boundary returns one row per month per key (they are not duplicates). The
"regional" command is the one that splits "cross-AZ" from "public IP of vendors
inside the region" - two different problems billed under the same usage type.
"""
import argparse, collections, datetime, json, subprocess, sys

# --- bill classification (fixed categories; the report always uses these) ---------------------
CATEGORY_RULES = [  # (category, varies-with-usage?, lowercase keywords matched against the Cost Explorer SERVICE name)
    ("Commitments (SP/RI)", False, ["savings plan", "reserved"]),
    ("Compute", True, ["compute cloud - compute", "lambda", "container service", "kubernetes", "lightsail", "batch", "app runner", "elastic beanstalk", "api gateway", "step functions", "fargate"]),
    ("Data & databases", False, ["relational database", "dynamodb", "elasticache", "opensearch", "redshift", "documentdb", "neptune", "memorydb", "athena", "glue", "kinesis", "streaming for apache kafka", "quicksight", "emr", "timestream"]),
    ("Storage", True, ["simple storage service", "elastic file system", "backup", "glacier", "fsx", "container registry", "storage gateway"]),
    ("Network", True, ["load balancing", "cloudfront", "virtual private cloud", "route 53", "global accelerator", "direct connect", "vpn", "transit gateway", "network firewall"]),
    ("Observability", True, ["cloudwatch", "x-ray", "grafana", "prometheus"]),
    ("Security & identity", False, ["guardduty", "security hub", "inspector", "key management", "secrets manager", "certificate manager", "waf", "shield", "cognito", "iam"]),
    ("Tax", False, ["tax"]),
]

def classify_service(name, marketplace=()):
    """Category and varies-with-usage flag for a Cost Explorer SERVICE name. 'EC2 - Other' must go through classify_ec2_other."""
    if name in marketplace:
        return "SaaS & Marketplace", False
    low = name.lower()
    for category, variable, keys in CATEGORY_RULES:
        if any(k in low for k in keys):
            return category, variable
    return "Other", False

def classify_ec2_other(usage_type):
    """'EC2 - Other' hides storage and network: split it by usage type."""
    u = usage_type.lower()
    if "ebs" in u or "snapshot" in u:
        return "Storage", True
    if "datatransfer" in u or "nat" in u or "publicipv4" in u or "bytes" in u or "idleaddress" in u or "elasticip" in u:
        return "Network", True
    if "cpucredits" in u or "boxusage" in u or "spotusage" in u:
        return "Compute", True
    return "Other", False

def ce(profile, args):
    cmd = ["aws", "ce", "get-cost-and-usage", "--profile", profile, "--output", "json", "--no-cli-pager"] + args
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode:
        sys.exit("ERR: " + out.stderr.strip()[:400])
    return json.loads(out.stdout)

def rows(d, metric="UnblendedCost"):
    for r in d["ResultsByTime"]:
        for g in r["Groups"]:
            yield r["TimePeriod"]["Start"], g["Keys"], float(g["Metrics"][metric]["Amount"]), float(g["Metrics"].get("UsageQuantity", {}).get("Amount", 0))

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--profile", required=True)
    today = datetime.date.today()
    ap.add_argument("--start", default=(today - datetime.timedelta(days=30)).isoformat()); ap.add_argument("--end", default=today.isoformat())
    ap.add_argument("cmd"); ap.add_argument("arg", nargs="?")
    a = ap.parse_args(); tp = f"Start={a.start},End={a.end}"
    if a.cmd == "services":
        d = ce(a.profile, ["--time-period", tp, "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "--group-by", "Type=DIMENSION,Key=SERVICE"])
        agg = collections.Counter()
        for _, k, c, _ in rows(d): agg[k[0]] += c
        tot = sum(agg.values()); print(f"TOTAL {tot:,.0f} USD ({a.start}..{a.end})")
        for k, c in agg.most_common(20): print(f"{c:9,.0f}  {100*c/tot:5.1f}%  {k}")
    elif a.cmd == "accounts":
        d = ce(a.profile, ["--time-period", tp, "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "--group-by", "Type=DIMENSION,Key=LINKED_ACCOUNT"])
        agg = collections.Counter()
        for _, k, c, _ in rows(d): agg[k[0]] += c
        for k, c in agg.most_common(): print(f"{c:9,.0f}  {k}")
    elif a.cmd == "usage":
        f = json.dumps({"Dimensions": {"Key": "SERVICE", "Values": [a.arg]}})
        d = ce(a.profile, ["--time-period", tp, "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "UsageQuantity", "--filter", f, "--group-by", "Type=DIMENSION,Key=USAGE_TYPE"])
        agg = collections.defaultdict(lambda: [0, 0])
        for _, k, c, u in rows(d): agg[k[0]][0] += c; agg[k[0]][1] += u
        for k, (c, u) in sorted(agg.items(), key=lambda x: -x[1][0])[:25]: print(f"{c:9,.0f} USD {u:14,.0f} units  {k}")
    elif a.cmd == "regional":
        f = json.dumps({"Dimensions": {"Key": "USAGE_TYPE", "Values": ["DataTransfer-Regional-Bytes"]}})
        d = ce(a.profile, ["--time-period", tp, "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "UsageQuantity", "--filter", f, "--group-by", "Type=DIMENSION,Key=OPERATION", "Type=DIMENSION,Key=LINKED_ACCOUNT"])
        agg = collections.defaultdict(lambda: [0, 0])
        for _, k, c, u in rows(d): agg[tuple(k)][0] += c; agg[tuple(k)][1] += u
        days = (datetime.date.fromisoformat(a.end) - datetime.date.fromisoformat(a.start)).days or 1
        print(f"operation / account            USD      GB    GB/day   ({a.start}..{a.end})")
        for k, (c, u) in sorted(agg.items(), key=lambda x: -x[1][0]):
            if c >= 1: print(f"{k[0]:<22} {k[1]:<13} {c:8,.0f} {u:9,.0f} {u/days:8,.0f}")
        print("\nInterZone-* = crosses AZs inside the VPC (fix with zone-aware routing).")
        print("PublicIP-*  = traffic to/from public IPs inside the region (SaaS vendors on AWS, e.g. Redis Cloud, Confluent); fix with private connectivity or less volume.")
    elif a.cmd == "regional-trend":
        f = json.dumps({"Dimensions": {"Key": "USAGE_TYPE", "Values": ["DataTransfer-Regional-Bytes"]}})
        d = ce(a.profile, ["--time-period", tp, "--granularity", "DAILY", "--metrics", "UsageQuantity", "--filter", f, "--group-by", "Type=DIMENSION,Key=OPERATION"])
        wk = collections.defaultdict(lambda: collections.defaultdict(list)); ops = set()
        for day, k, _, u in rows(d, "UsageQuantity"):
            dd = datetime.date.fromisoformat(day); w = dd - datetime.timedelta(days=dd.weekday()); ops.add(k[0]); wk[w][k[0]].append(u)
        ops = sorted(o for o in ops if o in ("InterZone-In", "InterZone-Out", "PublicIP-In", "PublicIP-Out"))
        print("week        " + "".join(f"{o:>14}" for o in ops) + "   (avg GB/day)")
        for w in sorted(wk): print(str(w) + "  " + "".join(f"{sum(wk[w][o])/7:14,.0f}" for o in ops))
    elif a.cmd == "sp":
        out = subprocess.run(["aws", "savingsplans", "describe-savings-plans", "--profile", a.profile, "--region", "us-east-1", "--output", "json", "--no-cli-pager"], capture_output=True, text=True)
        for s in json.loads(out.stdout or "{}").get("savingsPlans", []):
            print({k: s.get(k) for k in ["savingsPlanType", "state", "commitment", "paymentOption", "termDurationInSeconds", "start", "end", "ec2InstanceFamily", "region"]})
        for name, cmd in [("utilization", "get-savings-plans-utilization"), ("coverage", "get-savings-plans-coverage")]:
            out = subprocess.run(["aws", "ce", cmd, "--profile", a.profile, "--time-period", tp, "--granularity", "DAILY", "--output", "json", "--no-cli-pager"], capture_output=True, text=True)
            if out.returncode: print(name, "ERR", out.stderr[:200]); continue
            d = json.loads(out.stdout)
            print(f"\n{name} (daily):")
            if name == "utilization":
                for r in d["SavingsPlansUtilizationsByTime"]:
                    u = r["Utilization"]; s = r.get("Savings", {})
                    print(r["TimePeriod"]["Start"], "util %5.1f%%" % float(u["UtilizationPercentage"]), "commit %.2f" % float(u["TotalCommitment"]), "unused %.2f" % float(u["UnusedCommitment"]), "OD-eq covered %.2f" % float(s.get("OnDemandCostEquivalent", 0)))
            else:
                for r in d["SavingsPlansCoverages"]:
                    c = r["Coverage"]; print(r["TimePeriod"]["Start"], "covered %.2f" % float(c["SpendCoveredBySavingsPlans"]), "on-demand %.2f" % float(c["OnDemandCost"]), "cov %.1f%%" % float(c["CoveragePercentage"]))
        print("\nRead: if utilization is ~100% every day and unused ~0, the commitment is saturated 24h; removing night-time capacity saves nothing until usage exceeds it.")
    elif a.cmd == "marketplace":
        f = json.dumps({"Or": [{"Dimensions": {"Key": "BILLING_ENTITY", "Values": ["AWS Marketplace"]}}, {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon ElastiCache", "Amazon MQ", "Amazon Managed Streaming for Apache Kafka", "Amazon Relational Database Service", "Amazon DocumentDB (with MongoDB compatibility)"]}}]})
        d = ce(a.profile, ["--time-period", tp, "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "--filter", f, "--group-by", "Type=DIMENSION,Key=SERVICE"])
        months = [r["TimePeriod"]["Start"][:7] for r in d["ResultsByTime"]]; svcs = sorted({g["Keys"][0] for r in d["ResultsByTime"] for g in r["Groups"]})
        print(f"{'service':<60}" + "".join(f"{m:>9}" for m in months))
        for s in svcs:
            print(f"{s[:60]:<60}" + "".join(f"{sum(float(g['Metrics']['UnblendedCost']['Amount']) for g in r['Groups'] if g['Keys'][0]==s):9,.0f}" for r in d["ResultsByTime"]))
    elif a.cmd == "map":
        d = ce(a.profile, ["--time-period", tp, "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "--group-by", "Type=DIMENSION,Key=SERVICE"])
        svc = collections.Counter()
        for _, k, c, _ in rows(d): svc[k[0]] += c
        f = json.dumps({"Dimensions": {"Key": "BILLING_ENTITY", "Values": ["AWS Marketplace"]}})
        dm = ce(a.profile, ["--time-period", tp, "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "--filter", f, "--group-by", "Type=DIMENSION,Key=SERVICE"])
        marketplace = {k[0] for _, k, c, _ in rows(dm) if c > 0}
        f = json.dumps({"Dimensions": {"Key": "SERVICE", "Values": ["EC2 - Other"]}})
        do = ce(a.profile, ["--time-period", tp, "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "--filter", f, "--group-by", "Type=DIMENSION,Key=USAGE_TYPE"])
        other = collections.Counter()
        for _, k, c, _ in rows(do): other[k[0]] += c
        cat = collections.Counter(); var = {}; lines = []
        def put(category, variable, label, cost):
            cat[category] += cost; var[category] = variable; lines.append((cost, label, category, variable))
        for name, cost in svc.items():
            if cost <= 0 or name == "EC2 - Other": continue
            category, variable = classify_service(name, marketplace); put(category, variable, name, cost)
        for ut, cost in other.items():
            if cost <= 0: continue
            category, variable = classify_ec2_other(ut); put(category, variable, ("EBS: " if category == "Storage" else "EC2-Other: ") + ut, cost)
        tot = sum(cat.values())
        print(f"TOTAL {tot:,.0f} USD ({a.start}..{a.end})\n")
        print("Category                     USD     %   varies with usage?")
        for k, c in cat.most_common(): print(f"{k:<26} {c:9,.0f} {100*c/tot:5.1f}%   {'yes' if var[k] else 'no'}")
        print("\nTop 15 lines (service or usage type):")
        for c, label, category, variable in sorted(lines, reverse=True)[:15]: print(f"{c:9,.0f} {100*c/tot:5.1f}%  {label[:60]:<60} [{category}]")
        print("\nJSON for the report template (var comp = categories; var lines = top lines):")
        print("var comp=" + json.dumps([[k, round(c), var[k]] for k, c in cat.most_common()], ensure_ascii=False) + ";")
        print("var lines=" + json.dumps([[label, category, round(c), variable] for c, label, category, variable in sorted(lines, reverse=True)[:12]], ensure_ascii=False) + ";")
        print("\nReading: the largest category is the first front of the plan; 'varies with usage' responds to volume cuts, the rest to contracts, right-sizing or shutdown.")
    else:
        sys.exit("unknown command")

if __name__ == "__main__":
    main()
