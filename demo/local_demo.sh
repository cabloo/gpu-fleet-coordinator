#!/usr/bin/env bash
# The real dispatcher and the real worker on ONE machine: no GPU, no ssh daemon, no cloud account.
#
#     demo/local_demo.sh [WORKDIR]
#
#   1. copies the coordinator into WORKDIR/coordinator, so the demo has a registry of its own
#   2. turns WORKDIR/box into "a box": demo/bin/ssh runs every remote command there
#   3. registers that box, starts the dispatcher, queues three seeds of demo/job
#   4. waits for them, then prints the queue, one task's history and its result
#
# Nothing here can rent a machine: demo/bin/vastai refuses every call and the budget is $0.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(dirname "$HERE")"
WORK="${1:-$(mktemp -d "${TMPDIR:-/tmp}/fleet-demo.XXXXXX")}"
PY="$(command -v "${PYTHON:-python3}")"
mkdir -p "$WORK/box" "$WORK/coordinator" "$WORK/project" "$WORK/home" "$WORK/bin"
# The coordinator finds its data root through git when it sits inside a repository, so a WORKDIR
# inside one would make the demo use THAT repository's experiments/ instead of its own.
if git -C "$WORK" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "demo: $WORK is inside a git work tree; pick a WORKDIR outside any repository" >&2; exit 2
fi
cp -r "$REPO/fleet" "$WORK/coordinator/fleet"
cp "$HERE/job/train.py" "$HERE/job/config.json" "$WORK/project/"
ln -sf "$PY" "$WORK/bin/python"; ln -sf "$PY" "$WORK/bin/python3"

# A demo must not be able to reach a real coordinator or a real account, whatever this shell carries.
unset COORD_API_URL COORD_API_CA COORD_API_CERT COORD_API_KEY COORD_ROLE VAST_API_KEY \
      DISPATCHER_NTFY_TOPIC DISPATCHER_SSH_KEY
export RUNQ_TRANSPORT=local RUNQ_ACTOR=demo HOME="$WORK/home" DEMO_BOX_HOME="$WORK/box"
export PATH="$HERE/bin:$WORK/bin:$PATH"
FLEET="$WORK/coordinator/fleet"; DATA="$WORK/coordinator/experiments"; DB="$DATA/runs.sqlite"
runq() { "$PY" "$FLEET/runq.py" "$@"; }
say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }

cleanup() {
  [ -n "${DISPATCHER_PID:-}" ] && kill "$DISPATCHER_PID" 2>/dev/null || true
  pkill -f "$WORK/box/spool_bin/spool_worker.py" 2>/dev/null || true
}
trap cleanup EXIT

say "settings: no compilation, a 2-second poll, a \$0 rental budget"
mkdir -p "$DATA"
"$PY" - "$FLEET" "$DB" <<'PYEOF'
import json, sys
sys.path.insert(0, sys.argv[1])
import registry_db
conn = registry_db.connect(sys.argv[2])
for key, value in {"bundle_compile": False, "poll_seconds": 2, "max_hourly_usd": 0.0}.items():
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))
conn.commit()
PYEOF

say "register the box (the coordinator installs and starts its worker over 'ssh')"
"$PY" "$FLEET/register_owned_box.py" --label demo-box --host localhost --port 22 --slots 3

say "start the dispatcher"
"$PY" "$FLEET/dispatcher.py" > "$WORK/dispatcher.log" 2>&1 &
DISPATCHER_PID=$!

say "queue three seeds of one config (the job answers --print-run-identity first)"
for seed in 0 1 2; do
  runq add --config "$WORK/project/config.json" --group demo --name "seed$seed" -- --set "seed=$seed"
done
say "queue the first one again: an identical run is refused before anything is spent"
runq add --config "$WORK/project/config.json" --group demo --name again -- --set seed=0 || echo "(exit $?: duplicate refused)"

say "wait for the three tasks"
deadline=$(( $(date +%s) + ${DEMO_TIMEOUT:-300} ))
while :; do
  states="$(runq ls --group demo --json | "$PY" -c 'import json,sys; print(" ".join(sorted(t["state"] for t in json.load(sys.stdin))))')"
  printf '\r   %-60s' "$states"
  case " $states " in *" queued "*|*" claimed "*|*" shipped "*|*" running "*) ;; *) break ;; esac
  [ "$(date +%s)" -lt "$deadline" ] || { echo; echo "timed out; see $WORK/dispatcher.log"; exit 1; }
  sleep 2
done
echo

say "the queue"
runq ls --group demo
say "what happened to seed0"
"$PY" - "$FLEET" "$DB" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import registry_db
conn = registry_db.connect(sys.argv[2])
tid = conn.execute("SELECT id FROM tasks WHERE grp='demo' AND name='seed0'").fetchone()["id"]
for e in conn.execute("SELECT t, event, detail FROM events WHERE task_id=? ORDER BY seq", (tid,)):
    print(f"   {e['t']}  {e['event']:<10} {str(e['detail'])[:90]}")
PYEOF
say "its result, pulled home from the box"
cat "$DATA/demo/seed0/results.json"; echo
[ "$states" = "done done done" ] || { echo "not every task finished: $states"; exit 1; }
echo
echo "all three done. The whole run is in $WORK"
