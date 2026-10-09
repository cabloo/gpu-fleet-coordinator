#!/usr/bin/env bash
# owned_box_setup.sh — turn a FRESH Ubuntu 26.04 install into an owned fleet worker.
#
# Spec: docs/specs/box-pause.spec.md (capacity schedule, inv. 17–20d); the manual recipe it
# automates is docs/operations.md "Add a machine you own". Idempotent: re-run it freely.
#
#   1. On a machine with the repo (the devcontainer), build a SELF-CONTAINED copy and send it over:
#        fleet/owned_box_setup.sh --bundle > /tmp/setup-owned-box.sh
#        scp /tmp/setup-owned-box.sh <user>@<newbox>:
#   2. On the new box:
#        sudo bash setup-owned-box.sh --label <label>
#      A fresh NVIDIA driver needs ONE reboot: the script says so and stops; reboot, run it again.
#   3. It ends by printing the two coordinator-side steps (register the box, write its schedule).
#
# What it builds, and why each piece is the way it is:
#   * NVIDIA driver (ubuntu-drivers) + Docker + nvidia-container-toolkit, GPU passed by CDI
#     (`--device nvidia.com/gpu=all`), NEVER `--gpus all`: the legacy hook loses the GPU on the next
#     `systemctl daemon-reload` (7/7 desktop GPU drops, 2026-08-12..09-23).
#   * The fleet's worker image (Dockerfile.owned_worker: py3.12 = the fleet's compile ABI; the host's
#     py3.14 cannot run the fleet's .so files) with sshd on --port, reached by the fleet key only.
#   * The host capacity enforcer (inv. 20c): a 1-minute systemd timer running box_capacity_apply.py
#     against the schedule the COORDINATOR pushes into the bind-mounted control dir (inv. 20b), so
#     the day/night CPU cap and GPU power cap are edited in configs/capacity/<label>.json on the
#     coordinator — never on this box.
#   * Suspend/hibernate masked: a sleeping box looks like a dead one to the fleet.
#
# Flags:  --label L (required)   --fleet-key KEY|FILE (required on first run)   --port 2222
#         --no-gpu (CPU-only box)   --base-image IMG (override the auto-picked torch image)
#         --recreate (replace a running worker — DRAIN IT FIRST:
#         `python fleet/box_pause.py drain --label L`)   --bundle (print self-contained copy)
#         --name N (default fleet: what this fleet is called ON THIS HOST. It prefixes the worker
#         image, its container, its directories and its timer, so a host set up under one name
#         must always be re-run with that name, or it gets a second, parallel installation)
set -euo pipefail
# The WHOLE body is one `{ ... }` block, so bash parses all of it before running any of it. Without
# this, bash reads the file as it goes, and replacing the file mid-run (re-copying the bundle while it
# builds the image) makes it resume at a stale byte offset in the NEW file — which is exactly what
# happened on gpudesktop 2026-09-25 (`line 180: ------: command not found`).
{

PAYLOAD_FILES=(Dockerfile.owned_worker capacity.py box_capacity_apply.py)
NAME=fleet

ORIG_ARGS="$*"
# The coordinator's ssh PUBLIC key: the only key let into the worker. Pass it with --fleet-key.
FLEET_PUBKEY=""
LABEL="" FLEET_KEY="$FLEET_PUBKEY" PORT=2222 NO_GPU=0 RECREATE=0 BUNDLE=0 BASE_IMAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --label) LABEL="$2"; shift 2 ;;
    --fleet-key) FLEET_KEY="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-gpu) NO_GPU=1; shift ;;
    --recreate) RECREATE=1; shift ;;
    --base-image) BASE_IMAGE="$2"; shift 2 ;;
    --bundle) BUNDLE=1; shift ;;
    --name) NAME="$2"; shift 2 ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Everything this script creates on the host is named after the fleet (--name).
[[ "$NAME" =~ ^[a-z][a-z0-9-]*$ ]] || die "--name must be lowercase letters, digits, dashes"
IMAGE=$NAME-owned-worker
ETC=/etc/$NAME-worker
CONTROL=/var/lib/$NAME-worker/control
OPT=/opt/$NAME-worker
UNIT=$NAME-capacity

# --- --bundle: this script + its three payload files as ONE runnable file (base64 tar trailer) -----
MARK=__FLEET_OWNED_BOX_PAYLOAD__
if [ "$BUNDLE" = 1 ]; then
  src=$(cd "$(dirname "$0")" && pwd)
  for f in "${PAYLOAD_FILES[@]}"; do [ -f "$src/$f" ] || die "--bundle: $src/$f missing"; done
  sed "/^${MARK}\$/,\$d" "$0"
  echo "$MARK"
  tar -C "$src" -czf - "${PAYLOAD_FILES[@]}" | base64
  exit 0
fi

# --- preflight --------------------------------------------------------------------------------
[ "$(id -u)" = 0 ] || die "run as root: sudo bash $0 $ORIG_ARGS"
[ -n "$LABEL" ] || die "--label is required (it must match configs/capacity/<label>.json and the registry)"
[[ "$LABEL" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "--label must be lowercase letters, digits, dashes"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "--port must be a number"
. /etc/os-release
[ "${ID:-}" = ubuntu ] || die "this script targets Ubuntu (found ${ID:-unknown})"
[ "${VERSION_ID:-}" = 26.04 ] || warn "written for Ubuntu 26.04, found ${VERSION_ID:-?} — continuing"
WORKER="$NAME-${LABEL}-worker"
export DEBIAN_FRONTEND=noninteractive

# The payload: the bundle's trailer, else the files beside this script (running from a checkout).
PAYLOAD=$(mktemp -d); trap 'rm -rf "$PAYLOAD"' EXIT
if line=$(grep -an "^${MARK}\$" "$0" | cut -d: -f1) && [ -n "$line" ]; then
  tail -n +"$((line + 1))" "$0" | base64 -d | tar -xzf - -C "$PAYLOAD"
else
  src=$(cd "$(dirname "$0")" && pwd)
  for f in "${PAYLOAD_FILES[@]}"; do cp "$src/$f" "$PAYLOAD/" 2>/dev/null || die \
    "$f not found beside $0 — run from a repo checkout, or build a self-contained copy with --bundle"; done
fi

# --- fleet key --------------------------------------------------------------------------------
mkdir -p "$ETC" "$CONTROL" "$OPT"
if [ -n "$FLEET_KEY" ]; then
  [ -f "$FLEET_KEY" ] && FLEET_KEY=$(cat "$FLEET_KEY")
  [[ "$FLEET_KEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-[a-z0-9-]+)\ [A-Za-z0-9+/=]+ ]] \
    || die "--fleet-key is not an ssh PUBLIC key (expected 'ssh-ed25519 AAAA...')"
  touch "$ETC/authorized_keys"
  grep -qxF "$FLEET_KEY" "$ETC/authorized_keys" || printf '%s\n' "$FLEET_KEY" >> "$ETC/authorized_keys"
fi
[ -s "$ETC/authorized_keys" ] || die "no key in $ETC/authorized_keys"
chmod 644 "$ETC/authorized_keys"

log "base packages"
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg openssh-client pciutils python3 ubuntu-drivers-common >/dev/null

# --- NVIDIA driver ----------------------------------------------------------------------------
if [ "$NO_GPU" = 0 ] && ! lspci | grep -qi 'nvidia'; then
  warn "no NVIDIA GPU on the PCI bus — setting up a CPU-only worker (same as --no-gpu)"
  NO_GPU=1
fi
if [ "$NO_GPU" = 0 ]; then
  if nvidia-smi -L >/dev/null 2>&1; then
    log "NVIDIA driver loaded: $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | head -1)"
  elif dpkg -l 'nvidia-driver-*' 2>/dev/null | grep -q '^ii'; then
    die "an NVIDIA driver is installed but not loaded — REBOOT, then re-run this same command.
  (Secure Boot on? The first boot asks you to enroll the MOK key at the console — pick 'Enroll MOK'.)"
  else
    log "installing the recommended NVIDIA driver (ubuntu-drivers)"
    ubuntu-drivers install
    if mokutil --sb-state 2>/dev/null | grep -qi 'enabled'; then
      warn "Secure Boot is ON: on the next boot a blue MOK screen asks to enroll the driver's key — choose 'Enroll MOK' and enter the password you just set, or the driver will not load."
    fi
    log "driver installed. REBOOT NOW, then re-run this same command to finish."
    exit 0
  fi
fi

# --- Docker (+ NVIDIA container toolkit, CDI) --------------------------------------------------
if ! command -v docker >/dev/null; then
  log "installing Docker (docker.io)"
  apt-get install -y -qq docker.io >/dev/null
fi
systemctl enable --now docker >/dev/null
dmaj=$(docker version --format '{{.Server.Version}}' | cut -d. -f1)
[ "${dmaj:-0}" -ge 25 ] || die "Docker $dmaj.x is too old for CDI device requests (need >= 25)"

if [ "$NO_GPU" = 0 ]; then
  if ! command -v nvidia-ctk >/dev/null; then
    if apt-cache policy nvidia-container-toolkit 2>/dev/null | grep -q 'Candidate: [0-9]'; then
      log "installing nvidia-container-toolkit (Ubuntu archive)"
    else
      log "installing nvidia-container-toolkit (NVIDIA apt repo)"
      curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
      curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        > /etc/apt/sources.list.d/nvidia-container-toolkit.list
      apt-get update -qq
    fi
    apt-get install -y -qq nvidia-container-toolkit >/dev/null
  fi
  log "generating the CDI spec (nvidia.com/gpu=all)"
  mkdir -p /etc/cdi
  nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml >/dev/null 2>&1
  # toolkit >= 1.18 regenerates the spec on every boot / driver change; older ones need a re-run of this script
  systemctl enable --now nvidia-cdi-refresh.path >/dev/null 2>&1 || true
  nvidia-ctk cdi list 2>/dev/null | grep -q 'nvidia.com/gpu=all' || die "CDI spec has no nvidia.com/gpu=all"
  # Docker 25-27 gate CDI behind a feature flag (28+ default it on); merge it in without clobbering.
  changed=$(python3 - <<'PY'
import json, os
p = "/etc/docker/daemon.json"
d = json.load(open(p)) if os.path.exists(p) and os.path.getsize(p) else {}
if d.get("features", {}).get("cdi") is True:
    print("no")
else:
    d.setdefault("features", {})["cdi"] = True
    json.dump(d, open(p, "w"), indent=2)
    print("yes")
PY
)
  [ "$changed" = yes ] && { log "enabled CDI in /etc/docker/daemon.json — restarting docker"; systemctl restart docker; }
  # Keep the driver resident so the power limit (inv. 20a) is not reset while the GPU idles.
  systemctl enable --now nvidia-persistenced >/dev/null 2>&1 || nvidia-smi -pm 1 >/dev/null 2>&1 || true
fi

# --- worker image + container ---------------------------------------------------------------
# The fleet's cu126 torch has no kernels for Blackwell (compute cap >= 10, e.g. RTX 50xx = 12.0):
# cuBLAS runs, every torch elementwise kernel dies "no kernel image" (gpudesktop, 2026-09-25).
# Same torch + py3.12, built for CUDA 13.0 — needs driver >= 580.
if [ -z "$BASE_IMAGE" ]; then
  BASE_IMAGE=pytorch/pytorch:2.12.1-cuda12.6-cudnn9-runtime
  if [ "$NO_GPU" = 0 ]; then
    cap=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | cut -d. -f1)
    [ "${cap:-0}" -ge 10 ] && BASE_IMAGE=pytorch/pytorch:2.12.1-cuda13.0-cudnn9-runtime
  fi
fi
log "building $IMAGE on $BASE_IMAGE (host network: the default bridge can fail to reach apt mirrors)"
docker build -q --network=host --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  -f "$PAYLOAD/Dockerfile.owned_worker" -t "$IMAGE" "$PAYLOAD" >/dev/null

# The worker's sshd HOST KEYS live on the host, not in the image. openssh-server generates them at
# image BUILD, so every rebuild / --recreate presented a new identity, and the coordinator (ssh
# accept-new: trust once, reject any change) quarantined the box as unreachable — gpudesktop,
# 2026-09-25, "Host key for [gpudesktop]:2222 has changed". Seeded from the RUNNING worker when
# one exists, so the identity the coordinator already trusts is the one that is kept.
KEYS="$ETC/ssh_host_keys"
KEY_TYPES=(rsa ecdsa ed25519)
mkdir -p "$KEYS" && chmod 700 "$KEYS"
if [ ! -f "$KEYS/ssh_host_ed25519_key" ]; then
  if docker inspect "$WORKER" >/dev/null 2>&1; then
    log "keeping $WORKER's current ssh host keys (the identity the coordinator already trusts)"
    for t in "${KEY_TYPES[@]}"; do
      docker cp "$WORKER:/etc/ssh/ssh_host_${t}_key" "$KEYS/" >/dev/null
      docker cp "$WORKER:/etc/ssh/ssh_host_${t}_key.pub" "$KEYS/" >/dev/null
    done
  else
    log "generating persistent ssh host keys for $WORKER"
    for t in "${KEY_TYPES[@]}"; do
      ssh-keygen -q -N '' -t "$t" -C "$WORKER" -f "$KEYS/ssh_host_${t}_key"
    done
  fi
  chown root:root "$KEYS"/* && chmod 600 "$KEYS"/*_key && chmod 644 "$KEYS"/*.pub
fi
KEY_MOUNTS=()
for t in "${KEY_TYPES[@]}"; do
  KEY_MOUNTS+=(-v "$KEYS/ssh_host_${t}_key:/etc/ssh/ssh_host_${t}_key:ro"
               -v "$KEYS/ssh_host_${t}_key.pub:/etc/ssh/ssh_host_${t}_key.pub:ro")
done

GPU_ARGS=()
[ "$NO_GPU" = 0 ] && GPU_ARGS=(--device nvidia.com/gpu=all)
if docker inspect "$WORKER" >/dev/null 2>&1; then
  if [ "$RECREATE" = 1 ]; then
    log "--recreate: replacing $WORKER (any task still on it is lost — drain first)"
    docker rm -f "$WORKER" >/dev/null
  else
    log "$WORKER already exists — left running (pass --recreate to rebuild it on the new image)"
    [ "$(docker inspect -f '{{.Image}}' "$WORKER")" = "$(docker image inspect -f '{{.Id}}' "$IMAGE")" ] \
      || warn "$WORKER runs an OLDER image than the one just built — drain it, then re-run with --recreate"
    docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' "$WORKER" | grep -q /root/fleet_host \
      || warn "$WORKER predates the capacity control mount — caps will not apply until --recreate"
  fi
fi
if ! docker inspect "$WORKER" >/dev/null 2>&1; then
  log "starting $WORKER on port $PORT"
  # --restart always, not unless-stopped: the latter does not revive a container a host shutdown stopped.
  docker run -d --restart always "${GPU_ARGS[@]}" --name "$WORKER" -p "$PORT:22" \
    -v "$ETC/authorized_keys:/root/.ssh/authorized_keys:ro" \
    -v "$CONTROL:/root/fleet_host" \
    "${KEY_MOUNTS[@]}" \
    "$IMAGE" >/dev/null
fi
docker start "$WORKER" >/dev/null

log "verifying the worker"
sleep 2
abi=$(docker exec "$WORKER" python3 -c "import sysconfig;print(sysconfig.get_config_var('EXT_SUFFIX'))")
[[ "$abi" == .cpython-312* ]] || die "worker python ABI is $abi — the fleet compiles for cpython-312"
if [ "$NO_GPU" = 0 ]; then
  docker exec "$WORKER" nvidia-smi -L >/dev/null 2>&1 || die "the worker container cannot see the GPU"
  # nvidia-smi seeing the card does not mean torch has kernels for it: run a torch-OWN kernel
  # (elementwise), not a matmul — cuBLAS JITs for unknown archs and passes on a broken image.
  docker exec "$WORKER" python3 -c "import torch; assert (torch.ones(8, device='cuda') * 2 + 1).sum().item() == 24" \
    >/dev/null 2>&1 || die "the worker's torch cannot run a kernel on this GPU (no kernel image for its arch?)
  — the base image's CUDA build is too old for this card; re-run with --recreate (and --base-image if needed)"
fi

# --- host capacity enforcer (inv. 20c) --------------------------------------------------------
log "installing the capacity enforcer (systemd timer, every minute)"
install -m 0644 "$PAYLOAD/capacity.py" "$PAYLOAD/box_capacity_apply.py" "$OPT/"
NOGPU_FLAG=""; [ "$NO_GPU" = 1 ] && NOGPU_FLAG=" --no-gpu"
cat > /etc/systemd/system/$UNIT.service <<EOF
[Unit]
Description=GPU fleet: apply the coordinator's time-of-day CPU/GPU caps to $WORKER
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $OPT/box_capacity_apply.py --uncapped-if-missing --config $CONTROL/capacity.json --container $WORKER$NOGPU_FLAG
EOF
cat > /etc/systemd/system/$UNIT.timer <<EOF
[Unit]
Description=GPU fleet: re-apply capacity caps every minute

[Timer]
OnBootSec=30s
OnUnitActiveSec=1min
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now "$UNIT.timer" >/dev/null
systemctl start "$UNIT.service" || warn "first enforcer run failed: journalctl -u $UNIT"

# --- host hygiene -----------------------------------------------------------------------------
log "masking suspend/hibernate (a sleeping box reads as a dead one)"
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true
if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then
  log "ufw is active — allowing $PORT/tcp"; ufw allow "$PORT/tcp" >/dev/null
fi

# --- next steps (coordinator side) ------------------------------------------------------------
CORES=$(nproc)
TZ_NAME=$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)
GPU_NAME="" VRAM_GB=0
if [ "$NO_GPU" = 0 ]; then
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
  VRAM_GB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | awk '{printf "%d", $1/1024+0.5}')
fi
ADDR=$(hostname -I | awk '{print $1}')
GPU_OPT=""; [ -n "$GPU_NAME" ] && GPU_OPT=" --gpu-name \"$GPU_NAME\""

cat <<EOF

$(printf '\033[1;32m')✔ $(hostname) is ready as owned box '$LABEL'$(printf '\033[0m')
  worker $WORKER on :$PORT · ${CORES} cores · ${GPU_NAME:-no GPU}${GPU_NAME:+ ${VRAM_GB}GB}

Next, from the devcontainer (coordinator side):

  1. Register it by a name the router's DNS resolves (DHCP can move this box off $ADDR):
       python fleet/register_owned_box.py --label $LABEL --host $(hostname) --port $PORT \\
         --slots $CORES$GPU_OPT
       python fleet/runq.py box probe $LABEL --wait
EOF
if [ "$NO_GPU" = 0 ]; then cat <<EOF

  2. Set its day/night caps in configs/capacity/$LABEL.json
     and restart the dispatcher. The coordinator pushes it here within a poll;
     this box's timer applies it within a minute. cpu/vram = share the fleet may pack;
     gpu_power = HARD cap, fraction of the card's default power limit (omit = stock):
       {
         "tz": "$TZ_NAME",
         "cores": $CORES, "vram_gb": $VRAM_GB,
         "windows": [
           {"from": "23:00", "to": "07:00", "cpu": 0.9, "vram": 1.0},
           {"from": "07:00", "to": "23:00", "cpu": 0.5, "vram": 0.5, "gpu_power": 0.6}
         ]
       }
     Check what is applied here any time:  journalctl -u $UNIT -n 20
EOF
else cat <<EOF

  2. No GPU: do NOT write configs/capacity/$LABEL.json — with vram_gb 0 it infers 0 slots and
     refuses every task (a box without a card needs no schedule).
EOF
fi
exit 0
}
