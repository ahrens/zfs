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
# A raw receive zeroes the local MAC, which leaves no sign of its form, so
# the received dataset takes the form set by zfs_crypt_local_mac_has_project,
# both when it's new and when it's sent back to its origin.
#
# STRATEGY:
# For each setting of zfs_crypt_local_mac_has_project:
# 1. With the opposite setting, create an encrypted source and snapshot it
# 2. Raw send the snapshot to a new target
# 3. With the setting under test, mount the target, write to it and
#    snapshot it
# 4. Raw send the new snapshot back to the source, then mount and write to
#    the source
# Then:
# 5. Export and import the pool, then write to and sync every filesystem
# 6. Check that each received filesystem has the form set when it was
#    first written after its receive, and that its accounting works
#

verify_runnable "global"

POOLNAME=testpool$$
FILEDEV=$TEST_BASE_DIR/userspace_send_encrypted_local_mac_project

function cleanup
{
	destroy_pool $POOLNAME
	rm -f $FILEDEV
	set_tunable32 CRYPT_LOCAL_MAC_HAS_PROJECT $saved
}

function load_and_mount # <filesystem>
{
	log_must eval "echo password | zfs load-key $1"
	log_must zfs mount $1
}

function write_and_sync # <filesystem>
{
	log_must eval "echo more >> $(get_prop mountpoint $1)/file"
	sync_pool $POOLNAME
}

function mac_messages # <with|without> <filesystem>
{
	typeset id=$(get_prop objsetid $2)

	kstat dbgmsg | grep -c \
	    "objset $id has a local MAC $1 project accounting"
}

typeset saved=$(get_tunable CRYPT_LOCAL_MAC_HAS_PROJECT)
log_onexit cleanup

log_assert "Raw-received datasets take the default form of local MAC"

truncate -s $SPA_MINDEVSIZE $FILEDEV
log_must zpool create $POOLNAME $FILEDEV

for value in 0 1; do
	source=$POOLNAME/source$value
	target=$POOLNAME/target$value

	# 1.
	log_must set_tunable32 CRYPT_LOCAL_MAC_HAS_PROJECT $(( 1 - value ))
	log_must eval "echo password | zfs create -o encryption=on" \
	    "-o keyformat=passphrase -o keylocation=prompt $source"
	write_and_sync $source
	log_must zfs snapshot $source@base

	# 2.
	log_must eval "zfs send -w $source@base | zfs recv $target"

	# 3.
	log_must set_tunable32 CRYPT_LOCAL_MAC_HAS_PROJECT $value
	load_and_mount $target
	write_and_sync $target
	log_must zfs snapshot $target@s1

	# 4.
	log_must zfs umount $source
	log_must zfs unload-key $source
	log_must eval "zfs send -w -i @base $target@s1 | zfs recv -F $source"
	load_and_mount $source
	write_and_sync $source
done

# 5.
log_must zpool export $POOLNAME
log_must zpool import -d $TEST_BASE_DIR $POOLNAME
for value in 0 1; do
	for fs in $POOLNAME/source$value $POOLNAME/target$value; do
		load_and_mount $fs
		write_and_sync $fs
	done
done

# 6.
for value in 0 1; do
	form=$( (( value )) && echo with || echo without)
	for fs in $POOLNAME/source$value $POOLNAME/target$value; do
		(( $(mac_messages $form $fs) > 0 )) ||
		    log_fail "$fs has no local MAC $form project accounting"
		log_must zfs userspace $fs
		log_must zfs projectspace $fs
	done
done

log_pass "Raw-received datasets take the default form of local MAC"
