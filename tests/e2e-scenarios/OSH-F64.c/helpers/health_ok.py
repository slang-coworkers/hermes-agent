"""Exit 0 when the dashboard on 127.0.0.1:<port> reports /api/health ok, else 1."""

import json
import sys
import urllib.request

port = sys.argv[1]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=5) as resp:
        sys.exit(0 if json.load(resp).get("ok") is True else 1)
except (OSError, ValueError):
    sys.exit(1)
