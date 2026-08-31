# lib-dispatcher.sh — stage the networkd-dispatcher hooks into a tarball.
#
# Sourced by every builder, install and tools alike. It exists because the two
# generated hooks used to be heredocs duplicated across the three install
# builders: when the `auto_update` gate turned out to be testing for a value
# nothing ever wrote, the same one-line fix had to be applied in three places,
# and the rpi5 copy had already drifted from the other two. The hooks are now
# ordinary files under MANET/networkd-dispatcher/ with one copy each.
#
# The tools tarball stages exactly the same set. A dispatcher hook is a script,
# not a board-specific artifact, so there is no reason an over-the-air update
# should not carry a fix to one.
#
# Usage: stage_dispatcher_hooks <STAGE> <REPO_ROOT>

stage_dispatcher_hooks() {
    local stage="$1" repo="$2"
    local src="$repo/MANET/networkd-dispatcher"

    mkdir -p \
        "$stage/etc/networkd-dispatcher/carrier.d" \
        "$stage/etc/networkd-dispatcher/routable.d" \
        "$stage/etc/networkd-dispatcher/off.d" \
        "$stage/etc/networkd-dispatcher/no-carrier.d" \
        "$stage/etc/networkd-dispatcher/degraded.d"

    install -m 0755 "$src/carrier.d/50-ethernet-detect" \
        "$stage/etc/networkd-dispatcher/carrier.d/50-ethernet-detect"
    install -m 0755 "$src/routable.d/50-manet-uplink" \
        "$stage/etc/networkd-dispatcher/routable.d/50-manet-uplink"

    # One script serves all three teardown states.
    local state
    for state in off no-carrier degraded; do
        install -m 0755 "$src/off" \
            "$stage/etc/networkd-dispatcher/${state}.d/50-gateway-disable"
    done

    # Reference copies. Not active -- networkd-dispatcher only runs what is in
    # /etc/networkd-dispatcher/<state>.d -- but kept on the node so the
    # originals are readable next to the ones actually in force.
    mkdir -p "$stage/root/networkd-dispatcher"
    install -m 0755 "$src/carrier" "$stage/root/networkd-dispatcher/carrier"
    install -m 0755 "$src/off"     "$stage/root/networkd-dispatcher/off"
}
