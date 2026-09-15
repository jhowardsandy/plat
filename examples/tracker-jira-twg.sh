#!/bin/sh
# Example [tracker] command for Jira via the `twg` CLI.
#
#   [tracker]
#   cmd = "/path/to/tracker-jira-twg.sh {anchor}"
#
# Plat only requires that a command take an anchor and print
#   {"key": "...", "status": "...", "summary": "...", "url": "..."}
# so any tracker with any CLI works. This one is an example, not a dependency.
set -e
ANCHOR="$1"
F=$(twg jira workitem get "$ANCHOR" --output json \
      --select "data.key,data.summary,data.status.name" 2>/dev/null \
    | grep -oE '"/[^"]*stdout\.json"' | tr -d '"')
[ -n "$F" ] || exit 1
python3 - "$F" "$ANCHOR" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))["data"][0]
print(json.dumps({"key": d["key"], "status": d["status"]["name"],
                  "summary": d.get("summary", ""),
                  "url": f"https://mediciland.atlassian.net/browse/{d['key']}"}))
PY
