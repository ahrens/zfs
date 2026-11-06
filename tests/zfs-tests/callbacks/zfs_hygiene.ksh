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
# Copyright 2026 Oxide Computer Company
#

#
# Keep state that one test, or an earlier aborted run, leaves behind from
# failing the tests that come after it.
#
# zfs_hygiene preflight [-n]
#	Run once before the suite starts.  Look for state that the suite
#	left behind and a clean machine would not have: pools on $DISKS or
#	on test files, ZFS labels on $DISKS, tunables that an aborted test
#	saved and did not restore, zinject handlers, test users and groups,
#	mounts, loop and lofi devices, swap on test devices, scsi_debug and
#	files under $TEST_BASE_DIR.  Clean up what is clearly the suite's own
#	(-n: only report it).  Exit 1 if anything is left that would make
#	tests fail, or that the run would destroy without owning it.
#
# zfs_hygiene check <statefile> <label> [<sid> <groupstatefile>]
#	Run by the test runner before and after every test, setup and
#	cleanup script.  Record the state that a test must leave as it
#	found it and, if <statefile> exists, print one "<label>: ..." line
#	for each difference.  Undo what is safe to undo without knowing the
#	test (tunables, zinject handlers, and pools, datasets, devices,
#	mounts, users and files that the suite owns), so that one leak does
#	not fail the rest of the run.  Save the state in <statefile>.  With
#	<sid>, also report and kill the processes the test left running in
#	its session; <groupstatefile> is the statefile of the test's group.
#
# Runs as root.  Reads DISKS, KEEP, STF_SUITE, STF_PATH and TEST_BASE_DIR
# (or FILEDIR) from the environment.
#

export LC_ALL=C
export PATH="${STF_PATH:+$STF_PATH:}/usr/sbin:/usr/bin:/sbin:/bin"
UNAME=$(uname)
BASEDIR=${TEST_BASE_DIR:-${FILEDIR:-/var/tmp}}
BASEDIR=${BASEDIR%%/}
HYGIENE_DIR=/var/tmp/zfs_hygiene
# Names and sizes of the kernel variables that are tunables (illumos),
# found once per run.
TUNABLE_LIST=$HYGIENE_DIR/tunables.list

# KEEP is a list of pool names, or an egrep expression of them.
KEEP_POOLS=" $(echo "$KEEP" | sed 's/[]^$|()[]/ /g') "

# Test users get their home directory in here (see add_user()).
TEST_HOMES="$BASEDIR /var/tmp"
# Users the tests create and delete (see add_user() callers and acl.cfg).
TEST_USERS="huser other1 other2 puser quser1 quser2 staff1 staff2 zfstest
    zxtr"
# Groups the suite creates (see add_group() callers).
TEST_GROUPS="everyone hgroup othergrp pamtestgroup pgroup qgroup qgroup1
    qgroup2 zfsgrp"

typeset -i problems=0
typeset -i cleaned=0
dryrun=false

function msg
{
	echo "zfs_hygiene: $*"
}

# Something is wrong that we leave alone: the run must not start.
function problem
{
	msg "PROBLEM: $*"
	((problems += 1))
}

# Run a cleanup command, or only say what it would be with -n.
function fix
{
	typeset what="$1"
	shift

	if $dryrun; then
		problem "$what (would run: $*)"
		return 0
	fi
	if "$@" >/dev/null 2>&1; then
		msg "cleaned: $what ($*)"
		((cleaned += 1))
		return 0
	fi
	problem "$what (failed: $*)"
	return 1
}

function is_kept
{
	[[ -n $1 && "$KEEP_POOLS" == *" $1 "* ]]
}

#
# zfs-tests.sh runs the pre-flight before it sets its default KEEP (every
# imported pool), so that a pool an earlier run left is not kept.  Keep
# every imported pool that the suite did not make.
#
function default_keep
{
	typeset pool

	[[ -n $KEEP ]] && return
	for pool in $(zpool list -H -o name 2>/dev/null); do
		is_test_pool $pool || KEEP_POOLS+="$pool "
	done
}

# The pool of a zvol device (/dev/zvol/[r]dsk/pool/vol or /dev/zdN).
function zvol_pool
{
	typeset path=$1 link

	if [[ $path == /dev/zd* ]]; then
		for link in $(find /dev/zvol -type l 2>/dev/null); do
			if [[ $(readlink -f $link) == "$path" ]]; then
				path=$link
				break
			fi
		done
		[[ $path == /dev/zvol/* ]] || return
	fi
	echo ${path#/dev/zvol/} | sed -E 's,^(r?dsk/)?,,; s,/.*,,'
}

function under_basedir
{
	[[ "$1" == "$BASEDIR"/* ]]
}

# Is a file system mounted on or under this path?
function mounted_under
{
	mounts | awk -v d="$1" '
	    $2 == d || index($2, d "/") == 1 { found = 1 }
	    END { exit !found }'
}

# del_account user|group <name>
function del_account
{
	if [[ $UNAME == FreeBSD ]]; then
		pw ${1}del $2
	else
		${1}del $2
	fi
}

#
# Disks and partitions: device name without /dev/, /dev/dsk/ or the
# partition or slice suffix.
#
function disk_of
{
	typeset dev=${1##*/}

	case $UNAME in
	SunOS)
		echo $dev | sed -E 's/(s|p)[0-9]+$//'
		;;
	Linux)
		typeset path=$1
		[[ $path == /* ]] || path=/dev/$path
		path=$(readlink -f $path)
		# The disk at the bottom of a partition, dm or md stack.
		typeset disk=$(lsblk -snlo NAME,TYPE $path 2>/dev/null |
		    awk '$2 == "disk" {d = $1} END {print d}')
		dev=${path##*/}
		echo ${disk:-$dev}
		;;
	*)
		echo $dev | sed -E 's/(p|s)[0-9]+[a-h]?$//'
		;;
	esac
}

function test_disks
{
	typeset d

	for d in $DISKS; do
		disk_of $d
	done
}

function is_test_disk
{
	typeset d=$(disk_of $1)

	[[ -n $d && " $(echo $(test_disks)) " == *" $d "* ]]
}

# The file behind a loop or lofi device, if any.
function backing_file
{
	case $UNAME in
	Linux)
		losetup -nO BACK-FILE "$1" 2>/dev/null
		;;
	SunOS)
		lofiadm "$1" 2>/dev/null
		;;
	FreeBSD)
		mdconfig -lv -u "${1##*/}" 2>/dev/null | awk '{print $4}'
		;;
	esac
}

#
# Is this vdev path something the suite creates its pools on: one of
# $DISKS, a file or a loop device on a file under the test directory, a
# zvol, or a scsi_debug disk?
#
function is_test_vdev
{
	typeset path=$1

	under_basedir "$path" && return 0
	case $path in
	/dev/zvol/*|/dev/zd*)
		typeset pool=$(zvol_pool $path)
		[[ -n $pool ]] && ! is_kept $pool
		return
		;;
	/dev/loop*|/dev/lofi/*|/dev/md*)
		under_basedir "$(backing_file $path)"
		return
		;;
	esac
	is_test_disk $path && return 0
	if [[ $UNAME == Linux ]]; then
		typeset d=$(disk_of $path)
		[[ "$(cat /sys/block/$d/device/model 2>/dev/null)" == \
		    scsi_debug* ]] && return 0
	fi
	return 1
}

#
# A pool the suite made and left behind: all its vdevs are test vdevs, and
# it is on $DISKS or scsi_debug, on the suite's vdev files, or has a test
# pool's name.
#
function is_test_pool
{
	typeset pool=$1 vdev
	typeset any=false strong=false

	[[ $pool == testpool* ]] && strong=true
	for vdev in $(pool_vdevs $pool); do
		any=true
		is_test_vdev $vdev || return 1
		case $vdev in
		$BASEDIR/testdir*|$BASEDIR/file-vdev*) strong=true ;;
		/dev/*) [[ $vdev == /dev/zvol/* || $vdev == /dev/zd* ]] ||
		    strong=true ;;
		esac
	done
	$any && $strong
}

function uses_test_disk
{
	typeset vdev

	for vdev in $(pool_vdevs $1); do
		is_test_disk $vdev && return 0
	done
	return 1
}

#
# Whether a pool is suspended.  Its health does not say so on illumos.
#
function pool_suspended
{
	zpool status $1 2>/dev/null |
	    grep -Eq 'faulted in response to IO failures|pool is suspended'
}

#
# Destroy a pool the suite left behind.  Destroying a suspended pool can
# hang, but zpool clear resumes it if what made its I/O fail is gone, e.g.
# a tunable that the check has undone.  Not if it is faulted: that needs a
# reboot.
#
function destroy_pool
{
	typeset health
	typeset -i i=0

	if pool_suspended $1; then
		# zpool clear sleeps in the kernel if the pool suspends again.
		zpool clear $1 >/dev/null 2>&1 &
		while ((i++ < 60)) && kill -0 $! 2>/dev/null; do
			sleep 1
		done
		if pool_suspended $1; then
			echo "pool $1 is suspended" >&2
			return 1
		fi
	fi
	health=$(zpool list -H -o health $1)
	case $health in
	ONLINE|DEGRADED)
		;;
	*)
		echo "pool $1 is $health" >&2
		return 1
		;;
	esac
	if command -v timeout >/dev/null 2>&1; then
		timeout 300 zpool destroy -f $1
	else
		zpool destroy -f $1
	fi
}

# The leaf vdevs of a pool.  illumos prints disks as cXtYdZsN even with -P.
function pool_vdevs
{
	zpool list -vHP $1 2>/dev/null | awk '
	    $1 ~ /^\// { print $1 }
	    $1 ~ /^c[0-9]+(t[0-9A-Fa-f]+)?d[0-9]+([sp][0-9]+)?$/ {
		print "/dev/dsk/" $1 }'
}

#
# The state a test must leave as it found it, one "<type> <key> [value]"
# line per item.
#
function state_pools
{
	zpool list -H -o name 2>/dev/null | sed 's/^/pool /'
}

# The datasets, snapshots and bookmarks in the suite's pools.
function state_datasets
{
	typeset pool

	for pool in $(zpool list -H -o name 2>/dev/null); do
		is_test_pool $pool && ! pool_suspended $pool || continue
		zfs list -H -t all -o name -r $pool 2>/dev/null
	done | awk '/[\/@#]/ { print "dataset " $0 }'
}

function tunable_list_sunos
{
	typeset list=$1

	[[ -s $list ]] && return
	# The kernel variables the tests change, with their sizes.
	{
		grep -rhoE \
		    '(set|get|save|restore)_tunable(32|64)? +"?[a-z_0-9]+' \
		    $STF_SUITE/tests $STF_SUITE/include 2>/dev/null |
		    awk '{gsub(/"/, "", $2); print $2}'
		# Set with mdb -kw by tests on some branches.
		echo kmem_mtb_move
	} | sort -u >$list.names
	echo "::nm" | mdb -k 2>/dev/null | awk -F'|' -v names=$list.names '
	    BEGIN { while ((getline n <names) > 0) want[n] = 1 }
	    {
		gsub(/ /, "", $7)
		size = $2
		sub(/^0x0*/, "", size)
		if (($7 in want) && $3 ~ /OBJT/)
			print $7, size
	    }' >$list
	rm -f $list.names
}

function state_tunables
{
	typeset list=$TUNABLE_LIST

	case $UNAME in
	Linux)
		# zed raises zfs_zevent_len_max when it misses events.  For
		# a choice like "cycle [fastest] original", keep the choice.
		find /sys/module/zfs/parameters /sys/module/spl/parameters \
		    -type f -perm -u+w -exec grep -H . {} + 2>/dev/null |
		    sed -E 's,^/sys/module/([^/]*)/parameters/,\1/,; s,:, ,' |
		    grep -Ev '^zfs/(zfs_dbgmsg_enable|zfs_zevent_len_max) ' |
		    sed -E 's/^([^ ]+ ).*\[([^]]*)\].*$/\1\2/; s/^/tunable /'
		;;
	FreeBSD)
		sysctl -e vfs.zfs 2>/dev/null | sed 's/=/ /; s/^/tunable /'
		;;
	SunOS)
		tunable_list_sunos $list
		awk '$2 == 4 {print $1 "/X"} $2 == 8 {print $1 "/J"}' $list |
		    mdb -k 2>/dev/null |
		    awk -F: 'NF == 2 && $2 !~ /^ *$/ {
			gsub(/[ \t]/, "", $2); print "tunable", $1, "0x" $2 }'
		;;
	esac
}

function set_tunable
{
	typeset name=$1 value=$2
	typeset size

	case $UNAME in
	Linux)
		echo "$value" >/sys/module/${name%%/*}/parameters/${name#*/}
		;;
	FreeBSD)
		sysctl "$name=$value"
		;;
	SunOS)
		size=$(awk -v n=$name '$1 == n {print $2}' $TUNABLE_LIST)
		case $size in
		4)	echo "$name/W $value" | mdb -kw ;;
		8)	echo "$name/Z $value" | mdb -kw ;;
		*)	return 1 ;;
		esac
		;;
	esac
}

function state_zinject
{
	zinject 2>/dev/null | awk '$1 ~ /^[0-9]+$/ {
	    $1 = $1; print "zinject", $0 }'
}

# Mounted file systems: "special mountpoint fstype".
function mounts
{
	case $UNAME in
	Linux)	awk '{print $1, $2, $3}' /proc/self/mounts ;;
	SunOS)	awk '{print $1, $2, $3}' /etc/mnttab ;;
	FreeBSD) mount -p | awk '{print $1, $2, $3}' ;;
	esac
}

function state_mounts
{
	typeset base=$BASEDIR

	# Mounts that are not ZFS datasets: on disks, loop devices, zvols,
	# or in the test directory.
	mounts | awk -v base=$base '$3 != "zfs" &&
	    ($1 ~ /^\/dev\// || index($2, base "/") == 1) {
		print "mount", $2, $3, $1 }'
}

function state_devices
{
	case $UNAME in
	Linux)
		losetup -nO NAME,BACK-FILE -l 2>/dev/null | sed 's/^/loop /'
		dmsetup ls 2>/dev/null | awk '$1 != "No" {print "dm", $1}'
		[[ -d /sys/module/scsi_debug ]] && echo "module scsi_debug"
		awk 'NR > 1 {print "swap", $1}' /proc/swaps
		# A write to /dev/zvol/... before udev makes the link
		# leaves a regular file there that udev never replaces.
		find /dev/zvol -type f 2>/dev/null | sed 's/^/zvolfile /'
		;;
	SunOS)
		lofiadm 2>/dev/null | awk 'NR > 1 {print "lofi", $1, $2}'
		swap -l 2>/dev/null | awk 'NR > 1 {print "swap", $1}'
		dumpadm 2>/dev/null | awk '/Dump device:/ {print "dump", $3}'
		;;
	FreeBSD)
		mdconfig -lv 2>/dev/null | awk '{print "md", $1, $4}'
		swapctl -l 2>/dev/null | awk 'NR > 1 {print "swap", $1}'
		;;
	esac
}

function state_misc
{
	echo "hostid $(hostid 2>/dev/null)"
	cut -d: -f1,6 /etc/passwd | awk -F: '{print "user", $1, $2}'
	cut -d: -f1 /etc/group | sed 's/^/group /'
	case $UNAME in
	SunOS)
		share 2>/dev/null | awk '{print "share", $2}'
		;;
	*)
		grep -hv '^#' /etc/exports.d/zfs.exports /etc/zfs/exports \
		    2>/dev/null | awk 'NF {print "share", $1}'
		;;
	esac
	pgrep -x zed >/dev/null 2>&1 && echo "process zed"
	# Files and directories the tests make in the test directory, but
	# not the scratch files of log_must(), nor the runner's results,
	# configuration and vdev files.
	typeset skip='^(err|out|test_results|constrained_path\..*|zts-.*|'
	skip+='file-vdev[0-9]+|systemd-private-.*|cloud-init.*|zfs_hygiene)$'
	ls -A $BASEDIR 2>/dev/null | grep -Ev "$skip" | sed 's/^/file /'
}

#
# The ARC's size limits.  Writing 0 to zfs_arc_max or zfs_arc_min does not
# undo an earlier non-zero value, so the limits can stay changed while the
# tunables read as before.
#
function state_arc
{
	case $UNAME in
	Linux)
		awk '$1 == "c_max" || $1 == "c_min" {print "arc", $1, $3}' \
		    /proc/spl/kstat/zfs/arcstats 2>/dev/null
		;;
	SunOS)
		kstat -p zfs:0:arcstats:c_max zfs:0:arcstats:c_min \
		    2>/dev/null | awk '{sub(/.*:/, "", $1); print "arc", $1, $2}'
		;;
	FreeBSD)
		sysctl kstat.zfs.misc.arcstats.c_max \
		    kstat.zfs.misc.arcstats.c_min 2>/dev/null |
		    awk '{sub(/.*\./, "", $1); sub(/:$/, "", $1);
		    print "arc", $1, $2}'
		;;
	esac
}

function state
{
	state_pools
	state_datasets
	state_tunables
	state_arc
	state_zinject
	state_mounts
	state_devices
	state_misc
}

#
# Print what changed between two state files, as
#	leaked <line>		new in the second
#	removed <line>		gone from the second
#	changed <type> <key> <old> -> <new>
#
function state_diff
{
	awk '
	    function key(line) {
		split(line, f, " ")
		if (f[1] == "hostid" || f[1] == "dump")
			return f[1]
		return (f[1] == "tunable" || f[1] == "arc") ? \
		    f[1] " " f[2] : line
	    }
	    function val(line,	k) {
		k = key(line)
		if (length(line) <= length(k))
			return ""
		return substr(line, length(k) + 2)
	    }
	    NR == FNR { old[key($0)] = $0; next }
	    {
		k = key($0)
		if (!(k in old))
			print "leaked " $0
		else if (old[k] != $0)
			print "changed " k " " val(old[k]) " -> " val($0)
		new[k] = 1
	    }
	    END { for (k in old) if (!(k in new)) print "removed " old[k] }
	' "$1" "$2" | sort
}

#
# Undo one difference that state_diff reported, if it is safe without
# knowing the test: put tunables back, and remove what the test added and
# the suite owns.  Succeed if it was undone.
#
function undo
{
	typeset what=$1 type=$2 key=$3
	typeset home dir

	case "$what $type" in
	"changed tunable")
		set_tunable $key $4
		;;
	"leaked zinject")
		zinject -c $key
		;;
	"leaked mount")
		under_basedir $key || is_test_vdev $5 || return 1
		umount -f $key
		;;
	"leaked dataset")
		# Names may have spaces.  Its descendants and clones are leaks
		# too, and may be gone already.
		typeset ds="$*"
		ds=${ds#leaked dataset }
		zfs list -t all "$ds" >/dev/null 2>&1 || return 0
		if [[ $ds == *#* ]]; then
			zfs destroy "$ds"
		else
			zfs destroy -Rf "$ds"
		fi
		;;
	"leaked pool")
		# After the tunables, which may have suspended it.
		is_test_pool $key && destroy_pool $key
		;;
	"changed dump")
		dumpadm -d $key
		;;
	"leaked process")
		# A test zed (zed_start()) left running.
		[[ $key == zed ]] || return 1
		pkill -f "zed .*-d $BASEDIR/zed"
		;;
	"leaked loop")
		under_basedir "$4" && losetup -d $key
		;;
	"leaked lofi")
		under_basedir "$4" && lofiadm -d $key
		;;
	"leaked swap")
		is_test_vdev $key || return 1
		if [[ $UNAME == SunOS ]]; then
			swap -d $key
		else
			swapoff $key
		fi
		;;
	"leaked user")
		home=$4
		for dir in $TEST_HOMES; do
			[[ $home == $dir/$key ]] || continue
			del_account user $key && rm -rf $home
			return
		done
		return 1
		;;
	"leaked group")
		[[ " $(echo $TEST_GROUPS) " == *" $key "* ]] &&
		    del_account group $key
		;;
	"leaked zvolfile")
		[[ $key == /dev/zvol/* ]] && rm -f $key
		;;
	"leaked module")
		modprobe -r $key
		;;
	"leaked file")
		# Only what the tests (root or a test user) or the runner's
		# user made, and not if something is mounted in it.
		typeset f="$*" owner
		f="$BASEDIR/${f#leaked file }"
		owner=$(ls -ld "$f" | awk '{print $3}')
		[[ $owner == root || $owner == "$SUDO_USER" ||
		    " $(echo $TEST_USERS) " == *" $owner "* ]] || return 1
		mounted_under "$f" && return 1
		rm -rf "$f"
		;;
	*)
		return 1
		;;
	esac
}

function check
{
	typeset statefile=$1 label=$2 sid=$3 group=$4
	typeset now=$statefile.now diffs=$statefile.diff
	typeset reported=$(dirname $statefile)/.leakcheck-reported
	typeset line type pid

	# Processes the test left running, e.g. background jobs.  The runner
	# starts each test in a session of its own.
	if [[ -n $sid ]]; then
		for pid in $(pgrep -s $sid 2>/dev/null); do
			# A test may restart the zed that was running before
			# it (zed_stop, then zed_start).
			[[ $(ps -o comm= -p $pid) == @(zed|lt-zed) ]] &&
			    grep -qx 'process zed' $statefile 2>/dev/null &&
			    continue
			line="leaked process $pid $(ps -o args= -p $pid |
			    cut -c1-100)"
			kill -9 $pid 2>/dev/null
			# A process stuck in the kernel does not die.
			typeset -i i=0
			while ((i++ < 10)) && kill -0 $pid 2>/dev/null; do
				sleep 0.5
			done
			if kill -0 $pid 2>/dev/null; then
				echo "$label: $line (undo failed: still" \
				    "running, state $(ps -o s= -p $pid))"
			else
				echo "$label: $line (undone)"
			fi
		done
	fi

	mkdir -p $HYGIENE_DIR
	state | sort -u >$now
	if [[ ! -f $statefile ]]; then
		cp $now $statefile.base
		mv $now $statefile
		return
	fi

	# Leave out what is not this script's leak: see the comment on each
	# rule.  A test's check is the one with a session id.
	touch $reported
	typeset gbase=$statefile.base
	[[ -f $group.base ]] && gbase=$group.base
	state_diff $statefile $now | awk -v base=$statefile.base \
	    -v gbase=$gbase -v reported=$reported -v test=$sid '
	    FILENAME == base { inbase[$0] = 1 }
	    FILENAME == gbase { ingroup[$0] = 1 }
	    FILENAME == base || FILENAME == gbase { next }
	    FILENAME == reported { seen[$0] = 1; next }
	    /^(leaked|removed) pool / { gone[$3] = 1 }
	    # The tests of a group may set the hostid, which its setup saves
	    # and its cleanup restores (mmp): check it for the whole group.
	    test != "" && /^changed hostid / { next }
	    # What the group setup made is the group to manage (a test that
	    # destroys it is left to the tests after it), and what an earlier
	    # test made was a leak by that test.
	    /^removed / && !(substr($0, 9) in ingroup) { next }
	    # Something there before the first test is not a leak.
	    /^leaked / && (substr($0, 8) in inbase) { next }
	    # Setup recreates the testdir* mount points.
	    /^removed file testdir/ { next }
	    # Reported before and not undone: do not blame the group cleanup.
	    !($0 in seen) { out[++n] = $0 }
	    # The datasets of a pool made or destroyed went with it.
	    END {
		for (i = 1; i <= n; i++) {
			split(out[i], f, " ")
			pool = f[3]
			sub(/[\/@#].*/, "", pool)
			if (f[2] != "dataset" || !(pool in gone))
				print out[i]
		}
	    }
	' $statefile.base $gbase $reported - >$diffs

	# Undo in an order that works: handlers and mounts before pools,
	# pools before the devices and files they are on.
	for type in tunable zinject mount dataset pool swap loop lofi user \
	    group module file '[a-z]*'; do
		grep -E "^[a-z]+ $type " $diffs | while read -r line; do
			typeset err
			# Setup recreates the testdir* mount points, so an
			# empty one left behind does no harm.
			if [[ $line == "leaked file testdir"* ]] &&
			    rmdir "$BASEDIR/${line#leaked file }" 2>/dev/null
			then
				continue
			fi
			if err=$(undo $line 2>&1 >/dev/null); then
				echo "$label: $line (undone)"
				continue
			elif [[ -n $err ]]; then
				echo "$label: $line (undo failed: $(echo $err |
				    cut -c1-160))"
			else
				echo "$label: $line"
			fi
			echo "$line" >>$reported
		done
		grep -Ev "^[a-z]+ $type " $diffs >$diffs.tmp
		mv $diffs.tmp $diffs
	done
	rm -f $diffs
	state | sort -u >$statefile
	rm -f $now
}

#
# Pre-flight checks.
#
# Imported pools that are not the suite's but are in its way.
function preflight_foreign_pools
{
	typeset pool

	for pool in $(zpool list -H -o name 2>/dev/null); do
		if is_kept $pool; then
			uses_test_disk $pool &&
			    problem "kept pool $pool uses DISKS"
			continue
		fi
		is_test_pool $pool && continue
		if [[ $pool == testpool* ]] || uses_test_disk $pool; then
			problem "pool $pool uses devices outside DISKS and" \
			    "the test directory:" $(pool_vdevs $pool)
		elif [[ $UNAME == SunOS ]]; then
			# default_cleanup destroys every pool not in KEEP.
			problem "pool $pool is not in KEEP; the suite would" \
			    "destroy it"
		fi
	done
}

function preflight_pools
{
	typeset pool vdev health done
	typeset -i pass

	# Pools the suite created and left behind.  Destroy them; more than
	# one pass for pools on other test pools' zvols.
	for pass in 1 2 3; do
		for pool in $(zpool list -H -o name 2>/dev/null); do
			is_kept $pool && continue
			is_test_pool $pool || continue
			[[ " $done " == *" $pool "* ]] && continue
			health=$(zpool list -H -o health $pool)
			pool_suspended $pool && health=SUSPENDED
			if [[ $health != ONLINE && $health != DEGRADED ]]; then
				((pass == 3)) && problem "leftover pool $pool" \
				    "is $health; clear it or reboot"
				continue
			fi
			done="$done $pool"
			fix "destroy leftover pool $pool" destroy_pool $pool
		done
	done
}

function disk_paths
{
	typeset d=$1 s

	case $UNAME in
	Linux)
		lsblk -lnpo NAME /dev/$d 2>/dev/null
		;;
	SunOS)
		echo /dev/rdsk/${d}p0
		for s in 0 1 2 3 4 5 6 8; do
			[[ -e /dev/rdsk/${d}s$s ]] && echo /dev/rdsk/${d}s$s
		done
		;;
	*)
		ls /dev/${d}* 2>/dev/null
		;;
	esac
}

# Pools in the cache file that are not imported come back at the next boot.
function preflight_cachefile
{
	typeset pool imported=" $(echo $(zpool list -H -o name 2>/dev/null)) "

	[[ -f /etc/zfs/zpool.cache ]] || return
	zdb -C -U /etc/zfs/zpool.cache 2>/dev/null |
	    awk '/^[^ \t].*:$/ {sub(/:$/, ""); print}' | while read pool; do
		[[ $imported == *" $pool "* ]] && continue
		msg "note: pool $pool is in /etc/zfs/zpool.cache but not" \
		    "imported; the next boot will try to import it"
	done
}

function preflight_disks
{
	typeset d path

	for d in $(test_disks); do
		for path in $(disk_paths $d); do
			# Labels of exported or destroyed pools make
			# "zpool import" find more than the test expects.
			zdb -l $path >/dev/null 2>&1 || continue
			fix "ZFS label on $path" zpool labelclear -f $path
		done
	done
}

function preflight_tunables
{
	typeset name old new f

	# Find the tunables again: the kernel may have changed since the last
	# run.
	$dryrun || rm -f $TUNABLE_LIST

	# Tunables that save_tunable() saved and an aborted test did not
	# restore.
	for f in $BASEDIR/tunable-*; do
		[[ -f $f ]] || continue
		name=$(echo ${f##*/tunable-})
		if [[ $UNAME == Linux ]]; then
			typeset lname=$(awk -v n=$name '$1 == n {print $3}' \
			    $STF_SUITE/include/tunables.cfg 2>/dev/null)
			if [[ -n $lname ]]; then
				old=$(cat $f)
				new=$(cat /sys/module/zfs/parameters/$lname)
				[[ $new == "$old" ]] ||
				    fix "tunable $lname is $new, saved $old" \
				    set_tunable zfs/$lname "$old"
			fi
		fi
		fix "saved tunable file $f" rm -f $f
	done
}

function preflight_zinject
{
	typeset id pool rest

	zinject 2>/dev/null | awk '$1 ~ /^[0-9]+$/ {print $1, $2}' |
	    while read id pool; do
		if is_kept $pool; then
			msg "note: zinject handler $id on kept pool $pool"
			continue
		fi
		fix "zinject handler $id on $pool" zinject -c $id
	done
}

function preflight_mounts
{
	typeset mnt fstype dev

	state_mounts | while read line mnt fstype dev; do
		if under_basedir $mnt || is_test_vdev $dev; then
			fix "$fstype mount of $dev on $mnt" umount -f $mnt
		fi
	done
}

# Swap and dump devices on test pools' zvols keep the pools busy.
function preflight_swap
{
	typeset dev to

	case $UNAME in
	Linux)
		swapon --show=NAME --noheadings 2>/dev/null | while read dev; do
			is_test_vdev $dev && fix "swap on $dev" swapoff $dev
		done
		;;
	SunOS)
		swap -l 2>/dev/null | awk 'NR > 1 {print $1}' |
		    while read dev; do
			is_test_vdev $dev && fix "swap on $dev" swap -d $dev
		done
		dev=$(dumpadm 2>/dev/null | awk '/Dump device:/ {print $3}')
		if [[ -n $dev ]] && is_test_vdev $dev; then
			for to in /dev/zvol/dsk/*/dump; do
				[[ -e $to ]] &&
				    is_kept $(echo $to | cut -d/ -f5) && break
				to=
			done
			if [[ -n $to ]]; then
				fix "dump device on $dev" dumpadm -d $to
			else
				problem "dump device is $dev; move it with" \
				    "dumpadm -d"
			fi
		fi
		;;
	esac
}

# Loop and lofi devices on test files, and scsi_debug.
function preflight_devices
{
	typeset dev file dm

	case $UNAME in
	Linux)
		losetup -nO NAME,BACK-FILE -l 2>/dev/null |
		    while read dev file; do
			under_basedir "$file" || continue
			for dm in $(dmsetup ls 2>/dev/null | awk '{print $1}')
			do
				dmsetup deps -o devname $dm 2>/dev/null |
				    grep -q "(${dev##*/})" || continue
				fix "dm device $dm on $dev" dmsetup remove $dm
			done
			fix "loop device $dev on $file" losetup -d $dev
		done
		if [[ -d /sys/module/scsi_debug ]]; then
			fix "scsi_debug module loaded" modprobe -r scsi_debug
		fi
		find /dev/zvol -type f 2>/dev/null | while read file; do
			fix "regular file $file in /dev/zvol" rm -f $file
		done
		;;
	SunOS)
		lofiadm 2>/dev/null | awk 'NR > 1 {print $1, $2}' |
		    while read dev file; do
			under_basedir "$file" &&
			    fix "lofi device $dev on $file" lofiadm -d $dev
		done
		;;
	esac
}

function preflight_users
{
	typeset user home group

	cut -d: -f1,6 /etc/passwd | tr : ' ' | while read user home; do
		[[ $user == "$SUDO_USER" ]] && continue
		for dir in $TEST_HOMES; do
			[[ $home == $dir/$user ]] || continue
			fix "test user $user" del_account user $user
			[[ -d $home ]] && fix "home of test user $user" \
			    rm -rf $home
			break
		done
	done
	for group in $TEST_GROUPS; do
		grep -q "^$group:" /etc/group || continue
		fix "test group $group" del_account group $group
	done
}

function preflight_files
{
	typeset f

	# Test-run zed instances keep their pid file under $BASEDIR/zed.
	typeset pid=$(cat $BASEDIR/zed/zed.pid 2>/dev/null)
	if [[ -n $pid ]] && ps -o args= -p $pid 2>/dev/null | grep -q zed
	then
		fix "test zed" kill $pid
	fi
	for f in $BASEDIR/testdir* $BASEDIR/tempfile* $BASEDIR/zed \
	    $BASEDIR/zts-results.*; do
		[[ -e $f ]] || continue
		if mounted_under $f; then
			problem "leftover $f has a file system mounted in it"
			continue
		fi
		fix "leftover $f" rm -rf $f
	done
	# zfs-tests.sh refuses to start if its vdev files exist; remove them
	# unless a pool or a loop device still uses them.
	typeset used="$(zpool list -vHP 2>/dev/null | awk '{print $1}')
	    $(state_devices)"
	for f in $BASEDIR/file-vdev*; do
		[[ -f $f ]] || continue
		[[ "$used" == *"$f"* ]] && continue
		fix "leftover vdev file $f" rm -f $f
	done
}

function preflight_env
{
	typeset avail comp dev dups fstype mnt

	typeset runner='^[^ ]*python[0-9.]* [^ ]*'
	runner+='(test-runner\.py|/bin/run)( |$)'

	if pgrep -f "$runner" >/dev/null 2>&1; then
		problem "another test runner is running:" \
		    "$(pgrep -fl "$runner" | head -1)"
	fi
	dups=$(echo $(test_disks | sort | uniq -d))
	[[ -n $dups ]] && problem "DISKS lists $dups more than once"
	if [[ -n $SUDO_USER &&
	    " $(echo $TEST_USERS) " == *" $SUDO_USER "* ]]; then
		problem "the tests create and delete user $SUDO_USER; run" \
		    "the suite as another user"
	fi
	# The trim and initialize tests measure file vdevs with du.
	comp=$(zfs list -H -o compression $BASEDIR 2>/dev/null)
	if [[ -n $comp && $comp != off ]]; then
		msg "note: $BASEDIR is on a ZFS file system with" \
		    "compression=$comp; tests that check the space their" \
		    "file vdevs use (e.g. zpool_trim_verify_trimmed) will fail"
	fi
	avail=$(df -k $BASEDIR 2>/dev/null | awk 'END {print $(NF - 2)}')
	if [[ $avail == +([0-9]) ]] && ((avail < 2 * 1024 * 1024)); then
		problem "only $((avail / 1024)) MB free in $BASEDIR"
	fi

	# A disk in $DISKS that holds a file system mounted outside the test
	# directory, swap or dump is not the suite's: DISKS is wrong, e.g.
	# because device names changed at boot.
	mounts | while read dev mnt fstype; do
		[[ $dev == /dev/* && $fstype != zfs ]] || continue
		under_basedir $mnt && continue
		is_test_disk $dev && problem "DISKS includes" \
		    "$(disk_of $dev), which has $mnt mounted; check DISKS"
	done
	case $UNAME in
	Linux)	awk 'NR > 1 {print $1}' /proc/swaps ;;
	SunOS)	swap -l 2>/dev/null | awk 'NR > 1 {print $1}'
		dumpadm 2>/dev/null | awk '/Dump device:/ {print $3}' ;;
	esac | while read dev; do
		[[ $dev == /dev/zvol/* || $dev == /dev/zd* ]] && continue
		is_test_disk $dev && problem "DISKS includes" \
		    "$(disk_of $dev), which holds swap or dump; check DISKS"
	done
}

function preflight
{
	# See every pool, also the ones the run will exclude.
	unset __ZFS_POOL_EXCLUDE __ZFS_POOL_RESTRICT

	# Clean up nothing if DISKS looks wrong, a pool that is not ours is
	# in the way, or another run is going on.
	preflight_env
	preflight_foreign_pools
	if ((problems > 0)); then
		msg "$problems problem(s); fix them before running the ZFS" \
		    "test suite"
		return 1
	fi
	preflight_zinject
	preflight_mounts
	preflight_swap
	preflight_pools
	preflight_devices
	preflight_disks
	preflight_cachefile
	preflight_tunables
	preflight_users
	preflight_files

	if ((problems > 0)); then
		msg "$problems problem(s) left; fix them before running the" \
		    "ZFS test suite"
		return 1
	fi
	((cleaned > 0)) && msg "cleaned up $cleaned item(s) left by an" \
	    "earlier run"
	return 0
}

if [[ $(id -u) != 0 ]]; then
	echo "zfs_hygiene: must run as root" >&2
	exit 2
fi

default_keep

case $1 in
preflight)
	[[ $2 == -n ]] && dryrun=true
	preflight
	;;
check)
	if (($# < 3 || $# > 5)); then
		echo "usage: zfs_hygiene check <file> <label>" \
		    "[<sid> <groupfile>]" >&2
		exit 2
	fi
	check "$2" "$3" "$4" "$5"
	;;
state)
	mkdir -p $HYGIENE_DIR
	state | sort -u
	;;
*)
	echo "usage: zfs_hygiene preflight [-n] | check <file> <label>" >&2
	exit 2
	;;
esac
