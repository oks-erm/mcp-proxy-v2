#!/bin/sh
# Wrapper to surface startup errors (Cloud Run often truncates tracebacks)
set -e
export PORT="${PORT:-8080}"
exec python -c "
import sys
import traceback
try:
    import uvicorn
    from main import app
    uvicorn.run(app, host='0.0.0.0', port=int(\"$PORT\"), log_level='info')
except Exception as e:
    print('FATAL startup error:', e, file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
"
