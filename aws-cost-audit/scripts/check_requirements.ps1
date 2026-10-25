# Verifies what the aws-cost-audit skill needs on this Windows machine and in the account. Read-only.
# Usage (PowerShell 5.1 or 7):
#   .\scripts\check_requirements.ps1 -Profiles payer,infra [-Region us-east-1] [-KubeContext <ctx>]
# If scripts are blocked: powershell -ExecutionPolicy Bypass -File .\scripts\check_requirements.ps1 -Profiles payer
param(
  [string[]]$Profiles = @(),
  [string]$Region = "us-east-1",
  [string]$KubeContext = ""
)
$ErrorActionPreference = "Continue"
$env:AWS_PAGER = ""
$script:pass = 0; $script:warn = 0; $script:fail = 0
function Ok($m)  { Write-Host "PASS  $m"; $script:pass++ }
function Wrn($m) { Write-Host "WARN  $m"; $script:warn++ }
function Bad($m) { Write-Host "FAIL  $m"; $script:fail++ }
function Run([string[]]$CmdArgs) {
  # runs a native command, returns @{ok; out}; stderr is folded into out
  $out = & $CmdArgs[0] @($CmdArgs[1..($CmdArgs.Length-1)]) 2>&1 | ForEach-Object { "$_" }
  return @{ ok = ($LASTEXITCODE -eq 0); out = ($out -join "`n") }
}

Write-Host "== tools"
if (Get-Command aws -ErrorAction SilentlyContinue) {
  $v = (Run @("aws","--version")).out
  if ($v -like "aws-cli/2*") { Ok "aws cli v2 ($($v.Trim()))" } else { Bad "aws cli must be v2 (found: $v)" }
} else { Bad "aws cli not found (install AWS CLI v2 MSI: https://awscli.amazonaws.com/AWSCLIV2.msi)" }
$py = $null
foreach ($cand in @(@("py","-3"), @("python"), @("python3"))) {
  if (Get-Command $cand[0] -ErrorAction SilentlyContinue) {
    $r = Run ($cand + @("-c","import sys;print('%d.%d'%sys.version_info[:2]);sys.exit(0 if sys.version_info>=(3,8) else 1)"))
    if ($r.ok) { $py = ($cand -join " "); Ok "python $($r.out.Trim()) via '$py'  (run scripts as: $py -I scripts\<script>.py ...)"; break }
  }
}
if (-not $py) { Bad "python 3.8+ not found (install from python.org or 'winget install Python.Python.3.12')" }
if (Get-Command kubectl -ErrorAction SilentlyContinue) { Ok "kubectl found" } else { Wrn "kubectl not found (only needed for the EKS branch)" }
if (Get-Command bash -ErrorAction SilentlyContinue) { Ok "bash found (Git Bash or WSL): the .sh scripts will run" } else { Wrn "no bash: k8s_scaling_snapshot.sh needs Git Bash or WSL; everything else runs natively" }
if (Get-Command nslookup -ErrorAction SilentlyContinue) { Ok "nslookup available (resolve vendor hosts)" } else { Wrn "nslookup not found (optional)" }

if ($Profiles.Count -eq 0) { Write-Host "`nNo -Profiles given; skipping AWS access checks." }
foreach ($P in $Profiles) {
  Write-Host "`n== profile $P (region $Region)"
  $r = Run @("aws","sts","get-caller-identity","--profile",$P,"--output","text","--query","Account")
  if ($r.ok) { Ok "credentials valid, account $($r.out.Trim())" } else { Bad "sts get-caller-identity: $($r.out.Substring(0,[Math]::Min(200,$r.out.Length)))"; continue }
  $end = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd"); $start = (Get-Date).ToUniversalTime().AddDays(-3).ToString("yyyy-MM-dd")
  $r = Run @("aws","ce","get-cost-and-usage","--profile",$P,"--time-period","Start=$start,End=$end","--granularity","DAILY","--metrics","UnblendedCost","--output","text")
  if ($r.ok) { Ok "cost explorer readable (ce:GetCostAndUsage)" }
  elseif ($r.out -match "not enabled|DataUnavailable") { Bad "cost explorer not enabled in this (payer) account: enable it in Billing > Cost Explorer and wait up to 24h" }
  elseif ($r.out -match "AccessDenied|not authorized") { Bad "no permission for ce:GetCostAndUsage (attach AWSBillingReadOnlyAccess; in the payer activate 'IAM user and role access to Billing information')" }
  else { Bad "cost explorer: $($r.out.Substring(0,[Math]::Min(300,$r.out.Length)))" }
  $r = Run @("aws","ce","get-cost-and-usage","--profile",$P,"--time-period","Start=${start}T00:00:00Z,End=${start}T02:00:00Z","--granularity","HOURLY","--metrics","UnblendedCost","--output","text")
  if ($r.ok) { Ok "hourly granularity enabled (optional)" } else { Wrn "hourly granularity not enabled (optional; CloudWatch is used for hour-of-day profiles)" }
  $r = Run @("aws","savingsplans","describe-savings-plans","--profile",$P,"--region","us-east-1","--output","text","--query","length(savingsPlans)")
  if ($r.ok) { Ok "savings plans readable ($($r.out.Trim()) plan(s) visible from this account)" } else { Wrn "savingsplans:DescribeSavingsPlans failed (expected on member accounts; run on the payer)" }
  $r = Run @("aws","ce","get-savings-plans-utilization","--profile",$P,"--time-period","Start=$start,End=$end","--output","text")
  if ($r.ok) { Ok "ce:GetSavingsPlansUtilization" } else { Wrn "ce:GetSavingsPlansUtilization failed (run on the payer)" }
  $checks = @(
    @{ name="cloudwatch:ListMetrics"; args=@("cloudwatch","list-metrics","--namespace","AWS/EC2","--max-items","1"); hard=$true },
    @{ name="ec2:Describe* (NAT gateways)"; args=@("ec2","describe-nat-gateways","--query","length(NatGateways)"); hard=$true },
    @{ name="autoscaling:Describe*"; args=@("autoscaling","describe-auto-scaling-groups","--max-items","1"); hard=$false },
    @{ name="eks:ListClusters"; args=@("eks","list-clusters","--query","length(clusters)"); hard=$false },
    @{ name="ecs:ListClusters"; args=@("ecs","list-clusters","--query","length(clusterArns)"); hard=$false },
    @{ name="rds:Describe*"; args=@("rds","describe-db-instances","--query","length(DBInstances)"); hard=$false },
    @{ name="lambda:ListFunctions"; args=@("lambda","list-functions","--max-items","1"); hard=$false },
    @{ name="ecr:DescribeRepositories"; args=@("ecr","describe-repositories","--max-items","1"); hard=$false },
    @{ name="compute-optimizer status"; args=@("compute-optimizer","get-enrollment-status","--query","status"); hard=$false }
  )
  foreach ($c in $checks) {
    $r = Run (@("aws") + $c.args + @("--profile",$P,"--region",$Region,"--output","text"))
    $val = if ($r.out.Trim().Length -le 12) { " -> " + $r.out.Trim() } else { "" }
    if ($r.ok) { Ok "$($c.name)$val" } elseif ($c.hard) { Bad "$($c.name) denied" } else { Wrn "$($c.name) denied or empty (fine if the account does not use it)" }
  }
}

if ($KubeContext -ne "") {
  Write-Host "`n== kubernetes context $KubeContext"
  $r = Run @("kubectl","--context",$KubeContext,"get","nodes","--no-headers")
  if ($r.ok) { Ok "kubectl get nodes ($(($r.out -split "`n" | Where-Object { $_.Trim() -ne '' }).Count) nodes)" } else { Bad "kubectl get nodes: $($r.out.Substring(0,[Math]::Min(200,$r.out.Length)))" }
  $r = Run @("kubectl","--context",$KubeContext,"get","hpa","-A","--no-headers"); if ($r.ok) { Ok "HPAs readable" } else { Wrn "cannot list HPAs" }
  $r = Run @("kubectl","--context",$KubeContext,"auth","can-i","create","pods/portforward","-A"); if ($r.ok -and $r.out -match "yes") { Ok "port-forward allowed" } else { Wrn "port-forward may be denied (needed for Prometheus/Mimir profiles)" }
  $r = Run @("kubectl","--context",$KubeContext,"get","svc","-A","--no-headers")
  $m = ($r.out -split "`n") | Where-Object { $_ -match "mimir.*query-frontend|prometheus-operator-kube-p-prometheus|thanos-query|prometheus-server" } | ForEach-Object { ($_ -split "\s+")[0] + "/" + ($_ -split "\s+")[1] } | Select-Object -First 3
  if ($m) { Ok "metrics endpoint candidates: $($m -join ' ')" } else { Wrn "no Prometheus/Mimir service found by name (hour-of-day cluster profiles will need another source)" }
}

Write-Host "`n== summary: $script:pass pass, $script:warn warn, $script:fail fail"
if ($script:fail -eq 0) { Write-Host "Ready to run the audit (warnings only limit optional steps)." } else { Write-Host "Fix the FAIL items before running the audit." }
exit $script:fail
