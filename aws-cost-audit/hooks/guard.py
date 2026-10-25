#!/usr/bin/env python3
"""PreToolUse guard for the aws-cost-audit plugin (design decision D1).

Reads the hook input on stdin, finds every `aws`, `kubectl`, `helm` and `eksctl` invocation in the
Bash command and DENIES the call when any of them is not on the read-only allowlist. "Read-only"
stops depending on the model following an instruction and becomes mechanical.

Allowed (read or local-only):
  aws   describe-* get-* list-* lookup-* search-* batch-get-* head-* | sts * | sso * | login |
        configure * | help | --version | s3 ls | s3api list-*/get-*/head-* | eks update-kubeconfig |
        logs start-query / stop-query / filter-log-events / tail | ec2 wait describe-*
  kubectl get describe top logs version config auth api-resources api-versions explain
        cluster-info port-forward diff kustomize options
  helm  get list status show history search version env repo (list) template
  eksctl get | utils describe-* | version
Everything else is denied with the reason and the way out.

Opt out for a session: AWS_COST_AUDIT_ALLOW_WRITES=1 (the plugin stays installed; the audit still
only reads, but the guard no longer enforces it). Exit code is always 0; the decision is the JSON on
stdout, as Claude Code expects from a PreToolUse hook.
"""
import json
import os
import re
import shlex
import sys

GLOBAL_OPTS_WITH_VALUE = {
    "aws": {"--profile", "--region", "--output", "--query", "--endpoint-url", "--cli-connect-timeout",
            "--cli-read-timeout", "--ca-bundle", "--color", "--cli-binary-format", "--max-items",
            "--starting-token", "--page-size"},
    "kubectl": {"--context", "-n", "--namespace", "--kubeconfig", "--cluster", "--user", "-s", "--server",
                "--request-timeout", "-o", "--output", "--selector", "-l", "--field-selector", "--sort-by",
                "--template"},
    "helm": {"--kube-context", "-n", "--namespace", "--kubeconfig"},
    "eksctl": {"--region", "--profile", "-r", "-p"},
}
WRAPPERS = {"sudo", "env", "command", "time", "nice", "nohup", "rtk"}  # `rtk proxy aws ...` is a passthrough wrapper
AWS_READ_OPS = re.compile(r"^(describe|get|list|lookup|search|batch-get|head)(-[a-z0-9-]+)?$")
AWS_ALLOW = {
    ("sts", None), ("sso", None), ("login", None), ("configure", None), ("help", None),
    ("s3", "ls"), ("eks", "update-kubeconfig"),
    ("logs", "start-query"), ("logs", "stop-query"), ("logs", "filter-log-events"), ("logs", "tail"),
}
KUBECTL_ALLOW = {"get", "describe", "top", "logs", "version", "config", "auth", "api-resources", "api-versions",
                 "explain", "cluster-info", "port-forward", "diff", "kustomize", "options", "help"}
HELM_ALLOW = {"get", "list", "ls", "status", "show", "history", "search", "version", "env", "repo", "template", "help"}
EKSCTL_ALLOW = {"get", "version", "help"}


def split_commands(command):
    """Split a shell line into simple commands on ; && || | newline and $( ... ) / backticks."""
    text = command.replace("$(", " ; ").replace("`", " ; ")
    return [part for part in re.split(r"\|\||&&|;|\||\n", text) if part.strip()]


def tokens_of(simple):
    try:
        toks = shlex.split(simple, posix=True)
    except ValueError:
        toks = simple.split()
    out = []
    for t in toks:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) and not out:  # leading env assignment
            continue
        out.append(t)
    return out


def invocations(command):
    """Yield (tool, args) for every aws/kubectl/helm/eksctl call found in the command."""
    for simple in split_commands(command):
        toks = tokens_of(simple)
        i = 0
        while i < len(toks):
            base = os.path.basename(toks[i]).lower()
            if base.endswith(".exe"):
                base = base[:-4]
            if base in WRAPPERS:
                i += 1
                if base == "rtk" and i < len(toks) and toks[i] == "proxy":
                    i += 1
                continue
            if base in ("aws", "kubectl", "helm", "eksctl"):
                yield base, toks[i + 1:]
                break
            i += 1


def strip_global_options(tool, args):
    skip_value = GLOBAL_OPTS_WITH_VALUE[tool]
    out = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in skip_value:
            i += 2
            continue
        if a.startswith("--") and "=" in a:
            i += 1
            continue
        if a.startswith("-") and a not in ("--version",):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def check_aws(args):
    pos = strip_global_options("aws", args)
    if not pos or pos[0] in ("--version", "help"):
        return None
    service, op = pos[0], (pos[1] if len(pos) > 1 else None)
    if (service, None) in AWS_ALLOW or (service, op) in AWS_ALLOW:
        return None
    if service == "s3api" and op and re.match(r"^(list|get|head)-", op):
        return None
    if service == "ec2" and op == "wait" and len(pos) > 2 and pos[2].startswith("describe"):
        return None
    if op and AWS_READ_OPS.match(op):
        return None
    return f"aws {service} {op or ''}".strip()


def check_kubectl(args):
    pos = strip_global_options("kubectl", args)
    if not pos:
        return None
    verb = pos[0]
    if verb in KUBECTL_ALLOW:
        return None
    return f"kubectl {verb}"


def check_helm(args):
    pos = strip_global_options("helm", args)
    if not pos or pos[0] in HELM_ALLOW:
        return None
    return f"helm {pos[0]}"


def check_eksctl(args):
    pos = strip_global_options("eksctl", args)
    if not pos or pos[0] in EKSCTL_ALLOW:
        return None
    if pos[0] == "utils" and len(pos) > 1 and pos[1].startswith("describe"):
        return None
    return f"eksctl {' '.join(pos[:2])}"


CHECKS = {"aws": check_aws, "kubectl": check_kubectl, "helm": check_helm, "eksctl": check_eksctl}


def offending(command):
    """Return the list of denied invocations in a command (empty = allowed)."""
    found = []
    for tool, args in invocations(command):
        verdict = CHECKS[tool](args)
        if verdict:
            found.append(verdict)
    return found


def decide(payload, env=os.environ):
    if payload.get("tool_name") != "Bash":
        return None
    if env.get("AWS_COST_AUDIT_ALLOW_WRITES") == "1":
        return None
    command = (payload.get("tool_input") or {}).get("command") or ""
    bad = offending(command)
    if not bad:
        return None
    return ("aws-cost-audit guard: the audit is read-only and this command contains a write or "
            "unknown operation: " + "; ".join(bad) + ". Use describe/get/list calls instead. "
            "If this session really needs to change the account, set AWS_COST_AUDIT_ALLOW_WRITES=1 "
            "or disable the plugin.")


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    reason = decide(payload)
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                  "permissionDecision": "deny",
                                                  "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
