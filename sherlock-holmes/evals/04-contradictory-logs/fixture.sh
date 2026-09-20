#!/usr/bin/env bash
# Seeds the empty eval workspace with the recorded Loki/Tempo responses for this case.
# Runs only when `claude plugin eval` is invoked with --scaffold.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p ./fixtures
cp "$HERE/../fixtures/04-contradictory-logs/"*.json ./fixtures/
rm -f ./fixtures/expected.json
ls -la ./fixtures
