#!/usr/bin/env bash
# api_pki.sh — the coordinator API's private CA (remote-submit spec, M1).
#
# WHY A CA AND NOT TOKENS. A bearer token is replayable by whoever sees it, and comparing one means
# writing credential-handling code. Here OpenSSL does the authentication and nginx drives it: a
# client proves possession of a key it generated and never sent anywhere, and revocation is a CRL
# the proxy reloads.
#
# ⛔ THE CA KEY NEVER ENTERS A CONTAINER. Only `ca.pem` and `crl.pem` are mounted. Nothing in this
#    script reads or writes a CLIENT's private key either — a client generates its own and sends a
#    CSR, so the only secret that ever moves is none.
#
#   api_pki.sh init-ca                    # once per host; host_setup.sh calls it
#   api_pki.sh issue-server <name> [san…] # the coordinator's own certificate
#   api_pki.sh csr <name>                 # ON A CLIENT: make its key + CSR (key stays there)
#   api_pki.sh sign-client <name> <csr>   # on the host: sign a CSR for an ALREADY-LISTED client
#   api_pki.sh revoke <name>              # revoke every cert for that name, regenerate the CRL
#   api_pki.sh list                       # what exists, and how many days each has left
#   api_pki.sh crl                        # regenerate the CRL (a monthly timer calls this)
set -euo pipefail

COORD_ROOT=${COORD_ROOT:-/srv/coord}
CA_DIR=${COORD_API_CA_DIR:-$COORD_ROOT/secrets/ca}
API_DIR=${COORD_API_SECRETS:-$COORD_ROOT/secrets/api}
CLIENTS=${COORD_API_CLIENTS:-$API_DIR/clients.json}
CA_DAYS=3650
SERVER_DAYS=365
CLIENT_DAYS=365          # owner, 2026-09-17: 365 days, revocable
CRL_DAYS=365             # ⚠ nginx REJECTS EVERY CLIENT once a CRL expires — long, plus a timer

log() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
die() { printf 'api_pki: %s\n' "$*" >&2; exit 1; }

cmd_init_ca() {
  mkdir -p "$CA_DIR" "$API_DIR"
  chmod 0700 "$CA_DIR"
  if [ -f "$CA_DIR/ca.pem" ]; then
    log "CA already present at $CA_DIR/ca.pem — left untouched"
  else
    log "creating the API CA in $CA_DIR"
    openssl ecparam -name prime256v1 -genkey -noout -out "$CA_DIR/ca.key"
    chmod 0400 "$CA_DIR/ca.key"
    openssl req -x509 -new -key "$CA_DIR/ca.key" -sha256 -days "$CA_DAYS" \
      -subj "/CN=fleet coordinator API CA" \
      -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
      -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -out "$CA_DIR/ca.pem"
  fi
  touch "$CA_DIR/index.txt"
  [ -f "$CA_DIR/serial" ] || echo 1000 > "$CA_DIR/serial"
  [ -f "$CA_DIR/crlnumber" ] || echo 1000 > "$CA_DIR/crlnumber"
  [ -f "$CLIENTS" ] || echo '{}' > "$CLIENTS"
  _write_openssl_cnf
  cmd_crl
  install -m 0644 "$CA_DIR/ca.pem" "$API_DIR/ca.pem"
  log "CA ready. Public bits for clients: $API_DIR/ca.pem"
}

# openssl's `ca` needs a config; generated rather than shipped so the paths always match this host.
_write_openssl_cnf() {
  cat > "$CA_DIR/openssl.cnf" <<EOF
[ ca ]
default_ca = CA_default
[ CA_default ]
dir               = $CA_DIR
database          = \$dir/index.txt
serial            = \$dir/serial
crlnumber         = \$dir/crlnumber
certificate       = \$dir/ca.pem
private_key       = \$dir/ca.key
new_certs_dir     = \$dir
default_md        = sha256
policy            = policy_any
email_in_dn       = no
rand_serial       = no
unique_subject    = no
[ policy_any ]
commonName        = supplied
[ client_ext ]
basicConstraints  = critical,CA:FALSE
keyUsage          = critical,digitalSignature
extendedKeyUsage  = clientAuth
[ server_ext ]
basicConstraints  = critical,CA:FALSE
keyUsage          = critical,digitalSignature,keyEncipherment
extendedKeyUsage  = serverAuth
EOF
}

_need_ca() { [ -f "$CA_DIR/ca.key" ] || die "no CA yet — run: $0 init-ca"; }

cmd_issue_server() {
  _need_ca
  local name=${1:-tower}; shift || true
  local sans="DNS:$name"
  for extra in "$@"; do sans="$sans,DNS:$extra"; done
  log "issuing the server certificate for $sans"
  openssl ecparam -name prime256v1 -genkey -noout -out "$API_DIR/server.key"
  chmod 0600 "$API_DIR/server.key"
  openssl req -new -key "$API_DIR/server.key" -subj "/CN=$name" -out "$CA_DIR/server.csr"
  # The SANs live in their own ext file WITH the server extensions: `-extensions NAME` names a
  # SECTION, so a bare `subjectAltName=` line makes openssl fail with "Error checking certificate
  # extensions from extfile section server_ext".
  cat > "$CA_DIR/server_ext.cnf" <<EOF
[ server_ext ]
basicConstraints = critical,CA:FALSE
keyUsage         = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName   = $sans
EOF
  openssl ca -batch -config "$CA_DIR/openssl.cnf" -extensions server_ext \
    -extfile "$CA_DIR/server_ext.cnf" \
    -days "$SERVER_DAYS" -in "$CA_DIR/server.csr" -out "$API_DIR/server.pem"
  chmod 0644 "$API_DIR/server.pem"
  log "server certificate: $API_DIR/server.pem (SANs $sans)"
}

cmd_csr() {
  # RUNS ON THE CLIENT. The key is written here and never leaves; only the CSR is sent anywhere.
  local name=${1:?usage: $0 csr <client-name>}
  local dir=${COORD_API_CLIENT_DIR:-$HOME/.config/fleet/coord-api}
  mkdir -p "$dir"; chmod 0700 "$dir"
  openssl ecparam -name prime256v1 -genkey -noout -out "$dir/client.key"
  chmod 0600 "$dir/client.key"
  openssl req -new -key "$dir/client.key" -subj "/CN=$name" -out "$dir/client.csr"
  log "key stays at $dir/client.key — send ONLY $dir/client.csr to the coordinator host"
}

cmd_sign_client() {
  _need_ca
  local name=${1:?usage: $0 sign-client <name> <csr>} csr=${2:?usage: $0 sign-client <name> <csr>}
  [ -f "$csr" ] || die "no such CSR: $csr"
  openssl req -in "$csr" -noout -verify >/dev/null 2>&1 || die "$csr is not a valid CSR"
  # The name must ALREADY be in clients.json: a certificate this CA signed but the authorization
  # table does not know is a credential with no meaning, and issuing one invites the assumption
  # that holding a cert is itself a permission. It is not — inv. 4 is deny-by-default.
  python3 - "$CLIENTS" "$name" <<'PY' || die "add \"$name\" to $CLIENTS first (deny-by-default)"
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(data.get(sys.argv[2]), list) else 1)
PY
  log "signing a ${CLIENT_DAYS}-day client certificate for $name"
  openssl ca -batch -config "$CA_DIR/openssl.cnf" -extensions client_ext \
    -days "$CLIENT_DAYS" -in "$csr" -out "$CA_DIR/issued-$name.pem"
  cmd_crl
  log "signed: $CA_DIR/issued-$name.pem — send it back with $API_DIR/ca.pem (neither is secret)"
}

cmd_revoke() {
  _need_ca
  local name=${1:?usage: $0 revoke <name>} found=0
  for cert in "$CA_DIR"/issued-"$name".pem "$CA_DIR"/issued-"$name"-*.pem; do
    [ -f "$cert" ] || continue
    openssl ca -batch -config "$CA_DIR/openssl.cnf" -revoke "$cert" || true
    found=1
  done
  [ "$found" = 1 ] || log "no issued certificate found for $name (nothing to revoke)"
  cmd_crl
  log "revoked $name — the proxy refuses it after its next CRL reload"
}

cmd_crl() {
  _need_ca
  openssl ca -batch -config "$CA_DIR/openssl.cnf" -gencrl -crldays "$CRL_DAYS" \
    -out "$API_DIR/crl.pem"
  chmod 0644 "$API_DIR/crl.pem"
}

cmd_list() {
  _need_ca
  printf '%-28s %-10s %s\n' NAME STATUS EXPIRES
  awk -F'\t' '{split($6, cn, "CN="); printf "%-28s %-10s %s\n", cn[2], ($1=="V"?"valid":"REVOKED"), $2}' \
    "$CA_DIR/index.txt" 2>/dev/null || true
  [ -f "$API_DIR/server.pem" ] && \
    printf '%-28s %-10s %s\n' "(server)" valid "$(openssl x509 -enddate -noout -in "$API_DIR/server.pem" | cut -d= -f2)"
}

case "${1:-}" in
  init-ca)      shift; cmd_init_ca "$@" ;;
  issue-server) shift; cmd_issue_server "$@" ;;
  csr)          shift; cmd_csr "$@" ;;
  sign-client)  shift; cmd_sign_client "$@" ;;
  revoke)       shift; cmd_revoke "$@" ;;
  crl)          shift; cmd_crl "$@" ;;
  list)         shift; cmd_list "$@" ;;
  *) die "usage: $0 {init-ca|issue-server|csr|sign-client|revoke|crl|list}" ;;
esac
