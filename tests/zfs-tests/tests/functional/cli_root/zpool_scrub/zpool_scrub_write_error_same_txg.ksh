#!/bin/ksh -p
# SPDX-License-Identifier: CDDL-1.0
#
# CDDL HEADER START
#
# The contents of this file are subject to the terms of the
# Common Development and Distribution License (the "License").
# You may not use this file except in compliance with the License.
#
# You can obtain a copy of the license at usr/src/OPENSOLARIS.LICENSE
# or https://opensource.org/licenses/CDDL-1.0.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# When distributing Covered Code, include this CDDL HEADER in each
# file and include the License file at usr/src/OPENSOLARIS.LICENSE.
# If applicable, add the following below this CDDL HEADER, with the
# fields enclosed by brackets "[]" replaced with your own identifying
# information: Portions Copyright [yyyy] [name of copyright owner]
#
# CDDL HEADER END
#

. $STF_SUITE/include/libtest.shlib

#
# DESCRIPTION:
# A write that fails in the txg of a scrub's setup gives the device a DTL
# that ends after that txg.  The scrub must still run to completion, not be
# cancelled and replaced by a resilver.
#
# STRATEGY:
# 1. Create a mirror pool on two file vdevs.
# 2. Keep writing to a file so that every txg has dirty data.
# 3. Fail writes to one child while "zpool scrub" sets up the scrub.
# 4. Wait for the scans to finish.
# 5. Check that the scrub ran to completion.  A resilver of the writes
#    that failed after its setup may follow it.
#

verify_runnable "global"

function cleanup
{
	[[ -n "$writer_pid" ]] && kill $writer_pid 2>/dev/null
	wait
	zinject -c all >/dev/null 2>&1
	poolexists $TESTPOOL1 && destroy_pool $TESTPOOL1
	rm -f $VDEV1 $VDEV2
}

log_assert "A write error in the txg of a scrub's setup does not cancel" \
	"the scrub"
log_onexit cleanup

VDEV1=$TEST_BASE_DIR/scrub_write_error_vdev1
VDEV2=$TEST_BASE_DIR/scrub_write_error_vdev2

log_must truncate -s $MINVDEVSIZE $VDEV1 $VDEV2
log_must zpool create -f $TESTPOOL1 mirror $VDEV1 $VDEV2
log_must zfs set compression=off $TESTPOOL1
MNT=$(get_prop mountpoint $TESTPOOL1)

while true; do
	dd if=/dev/urandom of=$MNT/file bs=1M count=16 conv=notrunc \
	    status=none
done &
writer_pid=$!
sleep 4

log_must zinject -d $VDEV2 -e io -T write $TESTPOOL1
log_must zpool scrub $TESTPOOL1
log_must zinject -c all
kill $writer_pid
wait $writer_pid
writer_pid=""

log_must zpool wait -t scrub,resilver $TESTPOOL1
log_must zpool status -v $TESTPOOL1
history=$(zpool history -i $TESTPOOL1 | grep -E "scan (setup|aborted|done)")
log_note "$history"
next=$(echo "$history" | awk '/scan setup func=1 /{ found = 1; next }
	found { print; exit }')
[[ -n "$next" ]] || log_fail "scrub setup or its end not found in history"
[[ "$next" == *"scan done"* ]] || log_fail "the scrub did not complete: $next"

log_pass "A write error in the txg of a scrub's setup does not cancel" \
	"the scrub"
