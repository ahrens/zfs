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
# Encrypted datasets keep the form of their local MAC, with or without
# project accounting, whichever form new datasets get.
#
# STRATEGY:
# 1. With zfs_crypt_local_mac_has_project unset, create an encrypted
#    filesystem
# 2. With it set, create another
# 3. Export and import the pool, then write to and sync both filesystems
# 4. Check that each kept its form of local MAC
# 5. Export and import again, and check that both still mount
#

verify_runnable "global"

POOLNAME=testpool$$
FILEDEV=$TEST_BASE_DIR/userspace_encrypted_local_mac_project
NO_PROJECT=$POOLNAME/noproject
PROJECT=$POOLNAME/project

function cleanup
{
	destroy_pool $POOLNAME
	rm -f $FILEDEV
	set_tunable32 CRYPT_LOCAL_MAC_HAS_PROJECT $saved
}

function create_encrypted
{
	log_must eval "echo password | zfs create -o encryption=on" \
	    "-o keyformat=passphrase -o keylocation=prompt $1"
	log_must eval "echo data > $(get_prop mountpoint $1)/file"
}

function import_and_mount
{
	log_must zpool import -d $TEST_BASE_DIR $POOLNAME
	for fs in $NO_PROJECT $PROJECT; do
		log_must eval "echo password | zfs load-key $fs"
		log_must zfs mount $fs
	done
}

function mac_messages # <with|without> <filesystem>
{
	typeset id=$(get_prop objsetid $2)

	kstat dbgmsg | grep -c \
	    "objset $id has a local MAC $1 project accounting"
}

typeset saved=$(get_tunable CRYPT_LOCAL_MAC_HAS_PROJECT)
log_onexit cleanup

log_assert "Encrypted datasets keep the form of their local MAC"

truncate -s $SPA_MINDEVSIZE $FILEDEV
log_must zpool create $POOLNAME $FILEDEV

# 1. and 2.
log_must set_tunable32 CRYPT_LOCAL_MAC_HAS_PROJECT 0
create_encrypted $NO_PROJECT
sync_pool $POOLNAME
log_must set_tunable32 CRYPT_LOCAL_MAC_HAS_PROJECT 1
create_encrypted $PROJECT
sync_pool $POOLNAME

# 3.
log_must zpool export $POOLNAME
import_and_mount
typeset -i without=$(mac_messages without $NO_PROJECT)
typeset -i with=$(mac_messages with $PROJECT)
for fs in $NO_PROJECT $PROJECT; do
	log_must eval "echo more >> $(get_prop mountpoint $fs)/file"
done
sync_pool $POOLNAME

# 4.
(( $(mac_messages without $NO_PROJECT) > without )) ||
    log_fail "$NO_PROJECT lost its local MAC without project accounting"
(( $(mac_messages with $PROJECT) > with )) ||
    log_fail "$PROJECT lost its local MAC with project accounting"

# 5.
log_must zpool export $POOLNAME
import_and_mount
log_must zfs userspace $NO_PROJECT
log_must zfs projectspace $NO_PROJECT

log_pass "Encrypted datasets keep the form of their local MAC"
