#!/bin/bash
# Count distinct BATMAN originators across interfaces and routes.
# Usage: mesh-peer-count.sh [--batctl PATH] [--list]
# --list prints lowercase sorted MACs, one per line. Otherwise print a count.
# Failed, timed-out or malformed queries exit 1 without output so an
# unavailable table cannot imply isolation.

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
    printf '%s\n' "Cannot read BATMAN peers: $1" >&2
    exit 1
}

# Kill after the grace period if a child ignores TERM, bounding the wait.
table=$(timeout --kill-after=2 5 "$batctl" meshif bat0 originators_json 2>/dev/null) ||
    fail "originator query failed"
[ -n "$table" ] || fail "empty originator response"

# Slurp to reject multiple JSON documents in a response.
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
