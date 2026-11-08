#!/bin/ksh -p
# SPDX-License-Identifier: CDDL-1.0
#
# This file and its contents are supplied under the terms of the
# Common Development and Distribution License ("CDDL"), version 1.0.
# You may only use this file in accordance with the terms of version
# 1.0 of the CDDL.
#
# A full copy of the text of the CDDL should have accompanied this
# source.  A copy of the CDDL is also available via the Internet at
# https://opensource.org/license/CDDL-1.0.
#

#
# Copyright (c) 2026 by Oxide Computer Company. All rights reserved.
#

. $STF_SUITE/include/libtest.shlib
. $STF_SUITE/tests/functional/removal/removal.kshlib

#
# DESCRIPTION:
# A scrub that runs instead of the resilver scheduled by a "zpool replace"
# must write the segments of split blocks of a removed vdev to the new
# device.  When the scrub completes the new device is considered complete,
# so after the old device is detached it holds the only copy.
#
# STRATEGY:
# 1. Create a pool, write a file, add a second top-level vdev and remove
#    the first one with 8K removal segments, so every 128K block of the
#    file is split into 16 segments on the second vdev.
# 2. Delay the I/O of the pool's vdevs and start the sync of a txg.  While
#    it syncs, start a scrub and then replace a leaf of the second vdev
#    with a new vdev or a hot spare, so that the scrub's setup clears the
#    resilver that the replace scheduled and the resilver never starts.
# 3. Wait for the scrub, check that a scrub (not a resilver) ran, and
#    detach the old leaf.
# 4. Re-import the pool with the new vdev holding the only copy of the
#    file's segments, check the file, scrub and check for errors.
# 5. Do this for a single-disk vdev replaced by a new vdev, the same with a
#    hot spare, and the second child of a mirror replaced by a new vdev.
#
verify_runnable "global"

typeset VDEV_DIR=$TEST_BASE_DIR/removal_scrub_after_replace
typeset VDEV_A=$VDEV_DIR/a
typeset VDEV_B1=$VDEV_DIR/b1
typeset VDEV_B2=$VDEV_DIR/b2
typeset VDEV_NEW=$VDEV_DIR/new
typeset file digest

function cleanup
{
	zinject -c all
	log_must set_tunable32 REMOVE_MAX_SEGMENT $ORIG_REMOVE_MAX_SEGMENT
	log_must set_tunable32 SCAN_SUSPEND_PROGRESS $ORIG_SCAN_SUSPEND_PROGRESS
	poolexists $TESTPOOL && destroy_pool $TESTPOOL
	rm -rf $VDEV_DIR
}

#
# Create the pool, write $file, and move it onto the vdev given by the
# arguments, with every block split.
#
function setup_split_pool
{
	log_must mkdir -p $VDEV_DIR
	log_must truncate -s $MINVDEVSIZE $VDEV_A $VDEV_B1 $VDEV_B2 $VDEV_NEW
	log_must zpool create -f -O compression=off -O recordsize=128k \
	    $TESTPOOL $VDEV_A
	file=$(get_prop mountpoint $TESTPOOL)/file
	log_must dd if=/dev/urandom of=$file bs=1M count=16
	digest=$(xxh128digest $file)

	log_must zpool add -f $TESTPOOL "$@"
	log_must set_tunable32 REMOVE_MAX_SEGMENT 8192
	log_must zpool remove -w $TESTPOOL $VDEV_A
	log_must set_tunable32 REMOVE_MAX_SEGMENT $ORIG_REMOVE_MAX_SEGMENT
	log_must zpool sync $TESTPOOL
}

#
# Replace $1 with $2 so that a scrub takes over the resilver.
#
function scrub_during_replace
{
	typeset old=$1
	typeset new=$2

	#
	# Keep the scrub from issuing I/O until the replace is done, so that
	# it doesn't slow down the txgs that the replace waits for.
	#
	log_must set_tunable32 SCAN_SUSPEND_PROGRESS 1
	for vdev in $(get_disklist $TESTPOOL); do
		log_must zinject -d $vdev -D 100:1 $TESTPOOL
	done
	log_must dd if=/dev/urandom of=$(dirname $file)/file2 bs=1M count=1
	zpool sync $TESTPOOL &
	typeset sync_pid=$!
	sleep 0.5

	#
	# The scrub's setup is assigned to a txg that can't sync until the
	# replace has been applied, and the replace schedules its resilver a
	# few txgs later.
	#
	zpool scrub $TESTPOOL &
	typeset scrub_pid=$!
	sleep 0.5
	log_must zpool replace $TESTPOOL $old $new
	wait $scrub_pid || log_fail "zpool scrub $TESTPOOL failed"
	wait $sync_pid || log_fail "zpool sync $TESTPOOL failed"
	log_must zinject -c all
	log_must set_tunable32 SCAN_SUSPEND_PROGRESS 0

	log_must zpool wait -t scrub,resilver,replace $TESTPOOL
	typeset scan=$(zpool status $TESTPOOL | grep "scan:")
	log_note "$scan"
	echo "$scan" | grep -q "scrub repaired" || \
	    log_fail "A scrub didn't run after the replace: $scan"

	# A spare stays attached until the old leaf is detached.
	if vdevs_in_pool $TESTPOOL $old; then
		log_must zpool detach $TESTPOOL $old
	fi
	log_mustnot vdevs_in_pool $TESTPOOL $old
}

#
# Re-import the pool with $@ offline, so that the new vdev's copy of the
# file is the only one, and check the file and the pool.
#
function check_new_copy
{
	log_must zpool export $TESTPOOL
	log_must zpool import -d $VDEV_DIR $TESTPOOL
	for vdev in "$@"; do
		log_must zpool offline $TESTPOOL $vdev
	done

	typeset after=$(xxh128digest $file)
	if [[ "$after" != "$digest" ]]; then
		zpool status -v $TESTPOOL
		log_fail "$file changed: $digest became $after"
	fi
	log_must zpool scrub -w $TESTPOOL
	if ! check_pool_status $TESTPOOL "errors" "No known data errors"; then
		zpool status -v $TESTPOOL
		log_fail "$TESTPOOL has data errors"
	fi

	destroy_pool $TESTPOOL
	rm -rf $VDEV_DIR
}

log_assert "A scrub instead of a resilver writes split blocks of a removed" \
    "vdev to the new device"

ORIG_REMOVE_MAX_SEGMENT=$(get_tunable REMOVE_MAX_SEGMENT)
ORIG_SCAN_SUSPEND_PROGRESS=$(get_tunable SCAN_SUSPEND_PROGRESS)
log_onexit cleanup

log_note "Replace a single-disk vdev with a new vdev"
setup_split_pool $VDEV_B1
scrub_during_replace $VDEV_B1 $VDEV_NEW
check_new_copy

log_note "Replace a single-disk vdev with a hot spare"
setup_split_pool $VDEV_B1 spare $VDEV_NEW
scrub_during_replace $VDEV_B1 $VDEV_NEW
check_new_copy

log_note "Replace the second child of a mirror with a new vdev"
setup_split_pool mirror $VDEV_B1 $VDEV_B2
scrub_during_replace $VDEV_B2 $VDEV_NEW
check_new_copy $VDEV_B1

log_pass "A scrub instead of a resilver writes split blocks of a removed" \
    "vdev to the new device"
