#!/bin/bash
# Deprecated: use run_market_intraday_watches.sh --market us_equity

set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$PROJECT_DIR/scripts/run_market_intraday_watches.sh" --market us_equity
