#!/bin/bash
# India Calan intraday watch (wrapper for run_intraday_watch.sh).

set -uo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run_intraday_watch.sh" calan
