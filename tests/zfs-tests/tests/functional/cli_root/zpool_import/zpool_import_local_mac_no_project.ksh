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

. $STF_SUITE/include/libtest.shlib

#
# DESCRIPTION:
# An encrypted dataset written by illumos ZFS, whose local MAC doesn't cover
# the project accounting, can be mounted, and keeps that form of local MAC.
#
# STRATEGY:
# 1. Import a pool made by illumos ZFS with project_quota active, holding an
#    encrypted filesystem and a snapshot of it
# 2. Load the key, mount the filesystem and read the snapshot
# 3. Write to the filesystem and check that its local MAC was found to be
#    without project accounting
# 4. Export, import and mount again, and check that the MAC kept its form
#

verify_runnable "global"

POOL_NAME=noprojmac
POOL_FILE=illumos_local_mac_no_project.dat
FS=$POOL_NAME/testfs

function cleanup
{
	poolexists $POOL_NAME && log_must zpool destroy $POOL_NAME
	log_must rm -f /$TESTPOOL/$POOL_FILE
}
log_onexit cleanup

function import_and_mount
{
	log_must zpool import -d /$TESTPOOL $POOL_NAME
	log_must eval "echo password | zfs load-key $FS"
	log_must zfs mount $FS
}

function no_project_messages
{
	kstat dbgmsg | grep -c \
	    "objset $objsetid has a local MAC without project accounting"
}

log_assert "Datasets with illumos ZFS's local MAC can be mounted and keep it"

BLOCKFILES=$STF_SUITE/tests/functional/cli_root/zpool_import/blockfiles
log_must eval "bzcat $BLOCKFILES/$POOL_FILE.bz2 > /$TESTPOOL/$POOL_FILE"

# 1. and 2.
import_and_mount
mntpnt=$(get_prop mountpoint $FS)
log_must grep -q "illumos local MAC test file" $mntpnt/testfile
log_must eval "zfs send $FS@snap1 > /dev/null"
objsetid=$(get_prop objsetid $FS)

# 3.
typeset -i before=$(no_project_messages)
log_must eval "echo more >> $mntpnt/testfile"
sync_pool $POOL_NAME
typeset -i after=$(no_project_messages)
(( after > before )) ||
    log_fail "local MAC of $FS not found without project accounting"

# 4.
log_must zpool export $POOL_NAME
import_and_mount
log_must eval "echo more >> $mntpnt/testfile"
sync_pool $POOL_NAME
(( $(no_project_messages) > after )) || log_fail "local MAC of $FS changed form"
log_must eval "zfs send $FS@snap1 > /dev/null"

log_pass "Datasets with illumos ZFS's local MAC can be mounted and keep it"
