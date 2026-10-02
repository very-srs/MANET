#!/bin/bash
# Count distinct BATMAN originators; an unavailable table is never zero peers.
#
#   mesh-peer-count.sh [--batctl PATH] [--list]
#
# Prints how many distinct originator MACs batman-adv knows, or with --list
# the MACs themselves (lowercase, sorted, one per line). Multiple routes and
# interfaces to one originator count once. A failed, timed-out or malformed
# query exits 1 with nothing on stdout, so a caller can never mistake "could
# not ask" for "alone".

batctl=/usr/sbin/batctl
list=false
while [ $# -gt 0 ]; do
    case "$1" in
        --batctl)
            [ $# -ge 2 ] || { echo "usage: mesh-peer-count.sh [--batctl PATH] [--list]" >&2; exit 2; }
            batctl="$2"; shift 2 ;;
        --list) list=true; shift ;;
        *) echo "usage: mesh-peer-count.sh [--batctl PATH] [--list]" >&2; exit 2 ;;
    esac
done

fail() {
    echo "Cannot read BATMAN peers: $1" >&2
    exit 1
}

# KILL after a grace period: a child ignoring TERM must not hang the caller.
table=$(timeout --kill-after=2 5 "$batctl" meshif bat0 originators_json 2>/dev/null) ||
    fail "originator query failed"
[ -n "$table" ] || fail "empty originator response"

# Slurp, so a response holding more than one JSON document is refused rather
# than counted document by document.
peers=$(jq -rs '
    if length != 1 then error("expected exactly one originator table") else .[0] end
    | if type != "array" then error("originators response is not a list") else . end
    | map((if type == "object" then .orig_address else null end)
          | if type == "string" and length == 17
               and test("^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
            then ascii_downcase else error("invalid originator address") end)
    | unique | .[]' <<< "$table" 2>&1) || fail "${peers:-malformed originator response}"

if [ "$list" = true ]; then
    [ -z "$peers" ] || printf '%s\n' "$peers"
elif [ -z "$peers" ]; then
    echo 0
else
    printf '%s\n' "$peers" | wc -l
fi
