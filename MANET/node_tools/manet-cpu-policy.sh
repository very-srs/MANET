#!/bin/bash
# Keep all CPUs online and scale with demand. Limit only Compute Module 4
# boards, which must manage heat in a sealed enclosure.

SYS_CPU="${MANET_SYS_CPU:-/sys/devices/system/cpu}"
MODEL_FILE="${MANET_CPU_MODEL_FILE:-/proc/device-tree/model}"
# CM4 ceiling in kHz. Adjust this constant to change the enclosure policy.
CM4_MAX_FREQ_KHZ=800000

# Print accepted arguments to stderr.
usage() {
    printf '%s\n' 'usage: manet-cpu-policy.sh' >&2
}

[ $# -eq 0 ] || { usage; exit 1; }

# Report failures while allowing the remaining CPUs and policies to proceed.
cpu_policy_fail() {
    printf 'ERROR: CPU policy: %s\n' "$1" >&2
    failed=1
}

# Read one positive frequency in kHz, rejecting missing or malformed values.
read_frequency() {
    local value
    value=$(cat "$1" 2>/dev/null) || return 1
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || return 1
    printf '%s\n' "$value"
}

# Apply one policy and report the governor and ceiling read back from sysfs.
apply_policy() {
    local policy="$1" maximum minimum cap frequency governor current
    local -a frequencies governors

    maximum=$(read_frequency "$policy/cpuinfo_max_freq") || {
        cpu_policy_fail "${policy##*/}: cannot read cpuinfo_max_freq"
        return
    }
    cap=$maximum
    if [[ "$board" == *'Compute Module 4'* ]]; then
        minimum=$(read_frequency "$policy/cpuinfo_min_freq") || {
            cpu_policy_fail "${policy##*/}: cannot read cpuinfo_min_freq"
            return
        }
        (( cap > CM4_MAX_FREQ_KHZ )) && cap=$CM4_MAX_FREQ_KHZ
        (( cap < minimum )) && cap=$minimum
        if [ -f "$policy/scaling_available_frequencies" ]; then
            read -r -a frequencies < "$policy/scaling_available_frequencies"
            maximum=$cap
            cap=$minimum
            for frequency in "${frequencies[@]}"; do
                [[ "$frequency" =~ ^[1-9][0-9]*$ ]] || continue
                if (( frequency <= maximum && frequency > cap )); then
                    cap=$frequency
                fi
            done
        fi
    fi
    if ! printf '%s\n' "$cap" > "$policy/scaling_max_freq"; then
        cpu_policy_fail "${policy##*/}: cannot set frequency ceiling to $cap kHz"
    fi

    # If neither demand governor is offered, preserve the kernel's choice.
    governors=()
    if [ -f "$policy/scaling_available_governors" ]; then
        read -r -a governors < "$policy/scaling_available_governors"
    fi
    for governor in schedutil ondemand; do
        if [[ " ${governors[*]} " == *" $governor "* ]]; then
            if ! printf '%s\n' "$governor" > "$policy/scaling_governor"; then
                cpu_policy_fail "${policy##*/}: cannot set governor to $governor"
            fi
            break
        fi
    done
    current=$(cat "$policy/scaling_governor" 2>/dev/null) || {
        cpu_policy_fail "${policy##*/}: cannot read scaling_governor"
        current=unknown
    }
    cap=$(read_frequency "$policy/scaling_max_freq") || {
        cpu_policy_fail "${policy##*/}: cannot read scaling_max_freq"
        cap=unknown
    }
    printf 'CPU policy: board="%s" %s governor=%s cap=%s kHz\n' \
        "$board" "${policy##*/}" "$current" "$cap"
}

failed=0
# Device-tree strings end in NUL, which shell variables cannot hold.
board=$(tr -d '\000\n' 2>/dev/null < "$MODEL_FILE") || board=unknown
[ -n "$board" ] || board=unknown

# Online CPUs before enumerating policies: hotplug can expose another policy.
hotplug=0
for online in "$SYS_CPU"/cpu[0-9]*/online; do
    [ -f "$online" ] || continue
    hotplug=1
    if ! printf '1\n' > "$online"; then
        cpu_policy_fail "cannot bring ${online%/online} online"
    fi
done
if [ "$hotplug" -eq 0 ]; then
    printf '%s\n' 'CPU policy: no CPU hotplug controls; skipping online writes'
fi

policies=0
for policy in "$SYS_CPU"/cpufreq/policy*; do
    [ -d "$policy" ] || continue
    policies=1
    apply_policy "$policy"
done
if [ "$policies" -eq 0 ]; then
    printf 'CPU policy: board="%s" no cpufreq policies; skipping scaling\n' \
        "$board"
fi
exit "$failed"
