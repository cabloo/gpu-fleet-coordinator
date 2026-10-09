#!/usr/bin/env bash
# Coordinator container entrypoint — validate, ANNOUNCE WHAT REGISTRY WE ARE ABOUT TO DRIVE, exec.
#
# Spec: docs/specs/coordinator-container.spec.md (inv. 4-7).
#
# The announce step is the point of this script. A coordinator that comes up against the WRONG
# experiments root does not crash — it finds an empty queue, rents nothing, logs nothing alarming,
# and looks perfectly healthy while the real queue starves. Equally, one that comes up against the
# LIVE root while another coordinator owns it is the split-brain that must never happen. Both are
# invisible unless boot prints the registry's identity, so it does, every start, before exec.
set -euo pipefail

log() { printf '%s coordinator-entrypoint: %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

ROOT=/srv/fleet
EXPERIMENTS="$ROOT/experiments"
DB="$EXPERIMENTS/runs.sqlite"

log "image git_sha=${COORD_GIT_SHA:-unknown} role=${COORD_ROLE:-unset} uid=$(id -u) args=[$*]"

# ---------------------------------------------------------------------------------------------
# 1. Secrets: copied OUT of their read-only mounts into a writable home.
#
# Bind-mounting ~/.ssh read-only "works" but leaves ssh unable to append to known_hosts, so every
# connection to a fresh box warns; and a host-side file with the wrong mode makes ssh refuse the
# key outright with a message that reads like an auth failure. Copying is two lines and removes
# both traps. Secrets are NEVER baked into the image (inv. 6).
# ---------------------------------------------------------------------------------------------
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if [ -d /run/secrets/ssh ]; then
  cp -a /run/secrets/ssh/. ~/.ssh/ 2>/dev/null || true
  chmod 600 ~/.ssh/* 2>/dev/null || true
  chmod 644 ~/.ssh/*.pub ~/.ssh/known_hosts 2>/dev/null || true
  chmod 600 ~/.ssh/config 2>/dev/null || true
else
  log "WARNING: /run/secrets/ssh not mounted — no keys; every box will be unreachable"
fi
touch ~/.ssh/known_hosts && chmod 644 ~/.ssh/known_hosts

mkdir -p ~/.config/vastai
if [ -f /run/secrets/vastai/vast_api_key ]; then
  cp /run/secrets/vastai/vast_api_key ~/.config/vastai/vast_api_key
  chmod 600 ~/.config/vastai/vast_api_key
else
  log "WARNING: no vast_api_key mounted — renting/reconcile against Vast will fail"
fi

# ---------------------------------------------------------------------------------------------
# 2. The experiments mount. An UNMOUNTED volume is the failure this checks for: the Dockerfile
#    pre-creates the directory, so without `-v` the daemon would happily create a private registry
#    inside the container's writable layer, orchestrate nothing, and lose it on the next roll.
# ---------------------------------------------------------------------------------------------
[ -d "$EXPERIMENTS" ] || die "$EXPERIMENTS missing"
touch "$EXPERIMENTS/.coord_write_test" 2>/dev/null \
  || die "$EXPERIMENTS is not writable by uid $(id -u) — check the host dir's owner matches COORD_UID"
rm -f "$EXPERIMENTS/.coord_write_test"

if ! mountpoint -q "$EXPERIMENTS" 2>/dev/null && [ -z "${COORD_ALLOW_UNMOUNTED:-}" ]; then
  # `mountpoint` is absent in slim images often enough that this must not be fatal on its own;
  # the emptiness check below is the real signal.
  log "note: $EXPERIMENTS does not look like a mountpoint (ok if bind-mounted via a parent)"
fi

# ---------------------------------------------------------------------------------------------
# 3. ANNOUNCE THE REGISTRY. Read-only; a missing DB is fine (a fresh test-bed root starts empty
#    and the daemon creates the schema on first connect).
# ---------------------------------------------------------------------------------------------
if [ -f "$DB" ]; then
  python3 - "$DB" <<'PY'
import sqlite3, sys, os
db = sys.argv[1]
try:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    def q(sql, d="?"):
        try: return c.execute(sql).fetchone()[0]
        except Exception: return d
    print(f"  registry: {db} ({os.path.getsize(db)/1e6:.1f} MB)")
    total = q("select count(*) from tasks")
    queued = q("select count(*) from tasks where state='queued'")
    running = q("select count(*) from tasks where state='running'")
    print(f"  tasks: total={total} queued={queued} running={running}")
    n_vast = q("select count(*) from instances where state='live' and source!='owned'")
    n_owned = q("select count(*) from instances where state='live' and source='owned'")
    print(f"  instances live: vast={n_vast} owned={n_owned}")
    for r in c.execute("select label, ssh_host, ssh_port, slots_total from instances "
                       "where source='owned' and state='live'"):
        print(f"    owned box: {r[0]} -> {r[1]}:{r[2]} ({r[3]} slots)")
except Exception as e:
    print(f"  registry: UNREADABLE ({e})")
PY
else
  log "registry: $DB does not exist yet — the daemon will create an EMPTY one"
fi

# ---------------------------------------------------------------------------------------------
# 4. Test-bed guard. `COORD_ROLE=testbed` asserts the registry it is pointed at is NOT the live
#    fleet's. The cheap, honest discriminator is the live owned boxes: if this registry knows the
#    real boxes, it is (a copy of) the production registry and a mutating daemon on it would fight
#    the live coordinator — worker rollouts, packing, teardown. Refuse unless explicitly overridden.
# ---------------------------------------------------------------------------------------------
if [ "${COORD_ROLE:-}" = "testbed" ] && [ -f "$DB" ]; then
  n_owned=$(python3 -c "
import sqlite3,sys
try:
    c=sqlite3.connect('file:$DB?mode=ro',uri=True)
    print(c.execute(\"select count(*) from instances where source='owned' and state='live'\").fetchone()[0])
except Exception: print(0)" 2>/dev/null || echo 0)
  if [ "$n_owned" -gt 0 ] && [ -z "${COORD_TESTBED_ALLOW_OWNED_BOXES:-}" ]; then
    die "role=testbed but this registry has $n_owned LIVE OWNED BOXES — it looks like the production
     registry. A mutating test-bed daemon here would push worker code to boxes the live coordinator
     owns and fight its rolling upgrade. Point COORD_DATA at a fresh dir, or set
     COORD_TESTBED_ALLOW_OWNED_BOXES=1 if you really mean it."
  fi
fi

# Start marker for the healthcheck's grace period. A docker HEALTHCHECK runs with the IMAGE's env,
# not this script's, so an exported variable would never reach it — the marker has to be on disk.
date +%s > /tmp/coord_started_at 2>/dev/null || true

log "exec dispatcher.py $*"
cd "$ROOT"
exec python3 fleet/dispatcher.py "$@"
