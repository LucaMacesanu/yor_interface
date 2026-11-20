#!/bin/bash
# Bring up all YOR CAN interfaces, retrying until the adapters exist and
# ~/YOR/extra/setup.sh succeeds (arms answering). Replaces "rerun setup if the
# CAN bus wasn't connected yet". Does not enable motors.
#
#   CAN_PREFLIGHT_TIMEOUT  seconds before giving up (default 600)
#   CAN_IFACES             interfaces to wait for (default "can0 can_right can_left")
#   REQUIRE_ARM_FEEDBACK   1 = also require "Nero arm detected" on every arm bus (default 1)

YOR_DIR=${YOR_DIR:-$HOME/YOR}
TIMEOUT=${CAN_PREFLIGHT_TIMEOUT:-600}
IFACES=${CAN_IFACES:-"can0 can_right can_left"}
REQUIRE_ARM_FEEDBACK=${REQUIRE_ARM_FEEDBACK:-1}

start=$(date +%s)
attempt=0
while :; do
    attempt=$((attempt + 1))
    missing=""
    for i in $IFACES; do
        ip link show "$i" &>/dev/null || missing="$missing $i"
    done
    if [ -n "$missing" ]; then
        echo "[can_preflight] attempt $attempt: waiting for adapters:$missing"
    else
        echo "[can_preflight] attempt $attempt: running extra/setup.sh"
        out=$(cd "$YOR_DIR" && bash ./extra/setup.sh 2>&1)
        rc=$?
        echo "$out"
        if [ $rc -eq 0 ]; then
            warn=$(echo "$out" | grep -c "WARNING: .*no Nero arm feedback")
            if [ "$REQUIRE_ARM_FEEDBACK" != 1 ] || [ "$warn" -eq 0 ]; then
                echo "[can_preflight] OK after $attempt attempt(s)."
                exit 0
            fi
            echo "[can_preflight] CAN up but $warn arm(s) silent (powered? E-stop?)"
        fi
    fi
    now=$(date +%s)
    if [ $((now - start)) -ge "$TIMEOUT" ]; then
        echo "[can_preflight] giving up after ${TIMEOUT}s. Fix the hardware, then press Start again."
        exit 1
    fi
    sleep 3
done
