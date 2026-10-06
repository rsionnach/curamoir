#!/usr/bin/env bash
# The quickstart an evaluator runs first. Must pass as shipped (E12).
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

python3 -c "
from sample.core.billing import line_total
total = line_total(100, 2)
assert total == 240.0, f'expected 240.0, got {total}'
print(f'sample quickstart OK: line_total(100, 2) = {total}')
"
