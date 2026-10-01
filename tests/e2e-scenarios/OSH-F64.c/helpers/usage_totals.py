"""Print the model-call and USD totals across the gateway's default, orchestrator and builder stores."""

import os
import sqlite3

home = os.environ["HERMES_HOME"]
QUERY = (
    "select coalesce(sum(api_call_count), 0), "
    "coalesce(sum(case when actual_cost_usd > 0 then actual_cost_usd else estimated_cost_usd end), 0) "
    "from session_model_usage"
)
totals = [
    sqlite3.connect(path).execute(QUERY).fetchone()
    for path in (
        home + "/state.db",
        home + "/profiles/orchestrator/state.db",
        home + "/profiles/builder/state.db",
    )
    if os.path.exists(path)
]
print("calls", sum(c for c, _ in totals), "usd", round(sum(u for _, u in totals), 4))
