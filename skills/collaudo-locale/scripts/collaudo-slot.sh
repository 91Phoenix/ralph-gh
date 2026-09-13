#!/usr/bin/env bash
# Slot allocator for collaudi sharing one machine.
#
# A slot is one collaudo's private lane: an app port, a tunnel port and — when
# COLLAUDO_ACCOUNTS is set — a test account. Claims are atomic (mkdir), so
# several sessions can acquire at the same moment without agreeing on
# anything and without a human picking numbers.
#
# Usage:
#   eval "$(bash collaudo-slot.sh acquire [label])"
#   bash collaudo-slot.sh list                  # who holds what
#   bash collaudo-slot.sh release <slot>        # give it back
#   bash collaudo-slot.sh exclusive             # exit 0 only if nobody holds one
#
# Environment:
#   COLLAUDO_LEASES     lease root                 (default ~/.ralph-gh/collaudo-leases)
#   COLLAUDO_SLOTS      how many slots exist       (default 4)
#   COLLAUDO_ACCOUNTS   space-separated accounts, one per slot (optional)
#   COLLAUDO_PORT_BASE  app port of slot 1         (default 8110; slot n = base+n-1)
#   COLLAUDO_FWD_BASE   tunnel port of slot 1      (default 18110)
#   COLLAUDO_GRACE_MIN  minutes before a lease with no listener is reaped (default 30)

set -euo pipefail

LEASES="${COLLAUDO_LEASES:-$HOME/.ralph-gh/collaudo-leases}"
SLOTS="${COLLAUDO_SLOTS:-4}"
PORT_BASE="${COLLAUDO_PORT_BASE:-8110}"
FWD_BASE="${COLLAUDO_FWD_BASE:-18110}"
GRACE_MIN="${COLLAUDO_GRACE_MIN:-30}"
read -r -a ACCOUNTS <<< "${COLLAUDO_ACCOUNTS:-}"

mkdir -p "$LEASES"

listening() { [ -n "$(lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true)" ]; }

port_of() { echo $((PORT_BASE + $1 - 1)); }
fwd_of() { echo $((FWD_BASE + $1 - 1)); }
account_of() { local i=$(( $1 - 1 )); [ "$i" -lt "${#ACCOUNTS[@]}" ] && echo "${ACCOUNTS[$i]}" || true; }

# A live collaudo has its app port or its tunnel listening. A lease with
# neither, older than the grace period, belonged to a session that is gone.
reap() {
  local d n age
  for d in "$LEASES"/slot-*; do
    [ -d "$d" ] || continue
    n="${d##*/slot-}"
    if listening "$(port_of "$n")" || listening "$(fwd_of "$n")"; then continue; fi
    age=$(( ( $(date +%s) - $(stat -f %m "$d" 2>/dev/null || stat -c %Y "$d") ) / 60 ))
    [ "$age" -ge "$GRACE_MIN" ] && rm -rf "$d"
  done
  return 0   # never let the last short-circuited test above trip `set -e`
}

acquire() {
  local label="${1:-}" n d
  reap
  for n in $(seq 1 "$SLOTS"); do
    d="$LEASES/slot-$n"
    if mkdir "$d" 2>/dev/null; then
      printf '%s\n%s\n%s\n' "$label" "$(date +%s)" "$$" > "$d/lease"
      cat <<EOT
export COLLAUDO_SLOT=$n
export COLLAUDO_PORT=$(port_of "$n")
export COLLAUDO_FWD_PORT=$(fwd_of "$n")
export COLLAUDO_ACCOUNT='$(account_of "$n")'
EOT
      return 0
    fi
  done
  echo "echo 'collaudo-slot: no free slot (of $SLOTS)' >&2; false"
  return 1
}

list() {
  reap
  local d n label
  for d in "$LEASES"/slot-*; do
    [ -d "$d" ] || continue
    n="${d##*/slot-}"
    label="$(head -n1 "$d/lease" 2>/dev/null || true)"
    printf 'slot %s  port %s  fwd %s  account %s  label %s\n' \
      "$n" "$(port_of "$n")" "$(fwd_of "$n")" "$(account_of "$n")" "${label:--}"
  done
}

release() {
  local n="${1:?slot number}"
  rm -rf "$LEASES/slot-$n"
}

exclusive() {
  reap
  local d
  for d in "$LEASES"/slot-*; do
    [ -d "$d" ] && return 1
  done
  return 0
}

case "${1:-}" in
  acquire) shift; acquire "$@" ;;
  list) list ;;
  release) shift; release "$@" ;;
  exclusive) exclusive ;;
  *) echo "usage: collaudo-slot.sh acquire [label] | list | release <slot> | exclusive" >&2; exit 2 ;;
esac
