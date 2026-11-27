#!/usr/bin/env python3
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

"""
Keep state that one test, or an earlier aborted run, leaves behind from
failing the tests that come after it.

zfs_hygiene preflight [-n]
    Run once before the suite starts.  Look for state that the suite
    left behind and a clean machine would not have: pools on $DISKS or
    on test files, ZFS labels on $DISKS, tunables that an aborted test
    saved and did not restore, zinject handlers, test users and groups,
    mounts, loop and lofi devices, swap on test devices, scsi_debug and
    files under $TEST_BASE_DIR.  Clean up what is clearly the suite's own
    (-n: only report it).  Exit 1 if anything is left that would make
    tests fail, or that the run would destroy without owning it.

zfs_hygiene check [-n] <statefile> <label> [<sid> <groupstatefile>]
    Run by the test runner before and after every test, setup and
    cleanup script.  Record the state that a test must leave as it
    found it and, if <statefile> exists, print one "<label>: ..." line
    for each difference.  Undo what is safe to undo without knowing the
    test (tunables, zinject handlers, and pools, datasets, devices,
    mounts, users and files that the suite owns), so that one leak does
    not fail the rest of the run.  Save the state in <statefile>.  With
    <sid>, also report and kill the processes the test left running in
    its session; <groupstatefile> is the statefile of the test's group.
    With -n, only report: undo nothing and kill nothing.

zfs_hygiene state
    Print the state that check records.

Runs as root.  Reads DISKS, KEEP, STF_SUITE, STF_PATH and TEST_BASE_DIR
(or FILEDIR) from the environment.

Text from commands and files is decoded as file names are (os.fsdecode),
so that names with any bytes in them survive a round trip through a
statefile and can be printed.
"""

import argparse
import collections
import functools
import glob
import io
import os
import pwd
import re
import stat
import subprocess
import sys
import time

UNAME = os.uname().sysname
BASEDIR = (os.environ.get('TEST_BASE_DIR') or os.environ.get('FILEDIR') or
           '/var/tmp')
if BASEDIR.endswith('/'):
    BASEDIR = BASEDIR[:-1]
DISKS = os.environ.get('DISKS', '')
KEEP = os.environ.get('KEEP', '')
STF_SUITE = os.environ.get('STF_SUITE', '')
SUDO_USER = os.environ.get('SUDO_USER', '')

HYGIENE_DIR = '/var/tmp/zfs_hygiene'
# Names and sizes of the kernel variables that are tunables (illumos),
# found once per run.
TUNABLE_LIST = HYGIENE_DIR + '/tunables.list'
# Where the tunables of a kernel module are (Linux).
PARAMETERS = '/sys/module/%s/parameters'

# KEEP is a list of pool names, or an egrep expression of them.
KEEP_POOLS = set(re.sub(r'[\]\[^$|()]', ' ', KEEP).split())

# Test users get their home directory in here (see add_user()).
TEST_HOMES = (BASEDIR, '/var/tmp')
# Users the tests create and delete (see add_user() callers and acl.cfg).
TEST_USERS = ('huser other1 other2 puser quser1 quser2 staff1 staff2 '
              'zfstest zxtr').split()
# Groups the suite creates (see add_group() callers).
TEST_GROUPS = ('everyone hgroup othergrp pamtestgroup pgroup qgroup qgroup1 '
               'qgroup2 zfsgrp').split()

# Files in the test directory that are not the tests': the scratch files of
# log_must(), and the runner's results, configuration and vdev files.
NOT_TEST_FILES = re.compile(
    r'err|out|test_results|constrained_path\..*|zts-.*|file-vdev[0-9]+|'
    r'systemd-private-.*|cloud-init.*|zfs_hygiene')
# zed raises zfs_zevent_len_max when it misses events.
UNSTABLE_TUNABLES = ('zfs/zfs_dbgmsg_enable', 'zfs/zfs_zevent_len_max')
# How the tests name the kernel variable they change (illumos).
TUNABLE_USE = re.compile(
    r'(?:set|get|save|restore)_tunable(?:32|64)? +"?([a-z_0-9]+)')
# illumos prints disks as cXtYdZsN, also with zpool list -P.
CTD_DISK = re.compile(r'c[0-9]+(t[0-9A-Fa-f]+)?d[0-9]+([sp][0-9]+)?$')
# Undo in an order that works: handlers, mounts, swap and dump before the
# datasets and pools they hold busy, pools before the devices and files they
# are on.
UNDO_ORDER = ('tunable zinject mount swap dump dataset pool loop lofi user '
              'group module file').split()


class Failed(Exception):
    """A cleanup was not done.  The message is what the command that failed
    said, and is empty if the script does not do this cleanup at all."""


# Text, files and commands.

def lines(text):
    """Split text at newlines only: a name may hold \\r, \\f and so on."""
    if text.endswith('\n'):
        text = text[:-1]
    return text.split('\n') if text else []


def words(line):
    """The words of a line, split at blanks and tabs as awk does."""
    return [word for word in re.split('[ \t]+', line) if word]


def rows(text, skip=0):
    """The words of each line of text after the first skip lines.  As in awk,
    a word that is not there is empty."""
    return [words(line) + [''] * 8 for line in lines(text)[skip:]]


def cut(text, size):
    """The first size bytes of text."""
    return os.fsdecode(os.fsencode(text)[:size])


def read(path):
    """The contents of a file, nothing if it can't be read."""
    try:
        with open(path, 'rb') as f:
            return os.fsdecode(f.read())
    except OSError:
        return ''


def write(path, content, mode='wb'):
    """Write lines to a file."""
    with open(path, mode) as f:
        f.write(os.fsencode(''.join(line + '\n' for line in content)))


def find(top, is_wanted):
    """The paths under top whose mode is wanted (e.g. stat.S_ISLNK)."""
    found = []
    for parent, dirs, files in os.walk(top):
        for path in [os.path.join(parent, name) for name in dirs + files]:
            try:
                if is_wanted(os.lstat(path).st_mode):
                    found.append(path)
            except OSError:
                pass    # It is gone already.
    return sorted(found)


Result = collections.namedtuple('Result', 'rc out err')


def run(*argv, stdin='', timeout=None):
    """Run a command and return its exit status and what it printed.  Kill it
    if it takes longer than timeout seconds."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except OSError as e:
        return Result(127, '', '%s: %s' % (argv[0], e.strerror))
    try:
        out, err = proc.communicate(os.fsencode(stdin), timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass    # A command stuck in the kernel does not die.
        return Result(124, '', '')
    return Result(proc.returncode, os.fsdecode(out), os.fsdecode(err))


def out(*argv, **kwargs):
    """What a command prints; nothing if it can't run."""
    return run(*argv, **kwargs).out


def ok(*argv):
    return run(*argv).rc == 0


def must(*argv, **kwargs):
    result = run(*argv, **kwargs)
    if result.rc != 0:
        raise Failed(result.err)


def msg(text):
    print('zfs_hygiene: ' + text)


# What the suite owns.

def default_keep():
    """zfs-tests.sh runs the pre-flight before it sets its default KEEP (every
    imported pool), so that a pool an earlier run left is not kept.  Keep
    every imported pool that the suite did not make."""
    if not KEEP:
        for pool in imported_pools():
            if not is_test_pool(pool):
                KEEP_POOLS.add(pool)


def imported_pools():
    return out('zpool', 'list', '-H', '-o', 'name').split()


def is_zvol(path):
    return path.startswith(('/dev/zvol/', '/dev/zd'))


def zvol_pool(path):
    """The pool of a zvol device (/dev/zvol/[r]dsk/pool/vol or /dev/zdN)."""
    if path.startswith('/dev/zd'):
        links = find('/dev/zvol', stat.S_ISLNK)
        path = next((ln for ln in links if os.path.realpath(ln) == path), '')
    if not path.startswith('/dev/zvol/'):
        return ''
    return re.sub(r'^(r?dsk/)?', '', path[len('/dev/zvol/'):]).split('/')[0]


def under_basedir(path):
    return path.startswith(BASEDIR + '/')


def unescape(field):
    """/proc/self/mounts and mount -p write a blank, tab, newline or
    backslash in a name as \\ooo."""
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), field)


def escape(field):
    """A name for a state line, which holds one mount: a newline and a
    backslash as unescape() reads them."""
    return field.replace('\\', '\\134').replace('\n', '\\012')


def mounts():
    """Mounted file systems: [special, mountpoint, fstype]."""
    if UNAME == 'SunOS':
        # The fields are separated by tabs, and names are not escaped.
        return [(line.split('\t') + ['', ''])[:3]
                for line in lines(read('/etc/mnttab'))]
    if UNAME == 'Linux':
        text = read('/proc/self/mounts')
    else:
        text = out('mount', '-p')
    return [[unescape(field) for field in row[:3]] for row in rows(text)]


def mounted_under(path):
    """Is a file system mounted on or under this path?"""
    return any(mnt == path or mnt.startswith(path + '/')
               for _, mnt, _ in mounts())


def del_account(kind, name):
    """Delete a user or a group."""
    if UNAME == 'FreeBSD':
        must('pw', kind + 'del', name)
    else:
        must(kind + 'del', name)


def disk_of(dev):
    """The disk of a disk or partition: the device name without /dev/,
    /dev/dsk/ or the partition or slice suffix."""
    name = dev.split('/')[-1]
    if UNAME == 'SunOS':
        return re.sub(r'[sp][0-9]+$', '', name)
    if UNAME != 'Linux':
        return re.sub(r'[ps][0-9]+[a-h]?$', '', name)
    path = os.path.realpath(dev if dev.startswith('/') else '/dev/' + dev)
    # The disk at the bottom of a partition, dm or md stack.
    disks = [row[0] for row in rows(out('lsblk', '-snlo', 'NAME,TYPE', path))
             if row[1] == 'disk']
    return disks[-1] if disks else path.split('/')[-1]


@functools.lru_cache(maxsize=None)
def test_disks():
    return [disk_of(disk) for disk in DISKS.split()]


def is_whole_disk(dev):
    """Is a DISKS entry a whole disk (or a file, loop or multipath device),
    not a partition or a device on top of one?"""
    if dev.startswith('/') and os.path.isfile(dev):
        return True
    if UNAME == 'Linux':
        path = os.path.realpath(dev if dev.startswith('/') else '/dev/' + dev)
        kind = out('lsblk', '-dno', 'TYPE', path).strip()
        return kind in ('', 'disk', 'loop', 'mpath')
    return disk_of(dev) == dev.split('/')[-1]


def is_test_disk(dev):
    disk = disk_of(dev)
    return bool(disk) and disk in test_disks()


def backing_file(dev):
    """The file behind a loop, lofi or md device, if any."""
    if UNAME == 'Linux':
        return out('losetup', '-nO', 'BACK-FILE', dev).rstrip('\n')
    if UNAME == 'SunOS':
        return out('lofiadm', dev).rstrip('\n')
    info = rows(out('mdconfig', '-lv', '-u', dev.split('/')[-1]))
    return info[0][3] if info else ''


def is_test_vdev(path):
    """Is this vdev path something the suite creates its pools on: one of
    $DISKS, a file or a loop device on a file under the test directory, a
    zvol, or a scsi_debug disk?"""
    if under_basedir(path):
        return True
    if is_zvol(path):
        pool = zvol_pool(path)
        return bool(pool) and pool not in KEEP_POOLS
    if path.startswith(('/dev/loop', '/dev/lofi/', '/dev/md')):
        return under_basedir(backing_file(path))
    disk = disk_of(path)
    if disk and disk in test_disks():
        return True
    if UNAME == 'Linux':
        model = read('/sys/block/%s/device/model' % disk)
        return model.startswith('scsi_debug')
    return False


def pool_vdevs(pool):
    """The leaf vdevs of a pool."""
    names = [row[0] for row in rows(out('zpool', 'list', '-vHP', pool))]
    return [name if name.startswith('/') else '/dev/dsk/' + name
            for name in names if name.startswith('/') or CTD_DISK.match(name)]


def is_test_pool(pool):
    """A pool the suite made and left behind: all its vdevs are test vdevs, and
    it is on $DISKS or scsi_debug, on the suite's vdev files, or has a test
    pool's name."""
    vdevs = pool_vdevs(pool)
    strong = pool.startswith('testpool')
    for vdev in vdevs:
        if not is_test_vdev(vdev):
            return False
        if vdev.startswith((BASEDIR + '/testdir', BASEDIR + '/file-vdev')):
            strong = True
        elif vdev.startswith('/dev/') and not is_zvol(vdev):
            strong = True
    return bool(vdevs) and strong


def is_test_home(user, home):
    """Is this the home directory that add_user() gives a test user?"""
    return home in [homes + '/' + user for homes in TEST_HOMES]


def uses_test_disk(pool):
    return any(is_test_disk(vdev) for vdev in pool_vdevs(pool))


def pool_suspended(pool):
    """Whether a pool is suspended.  Its health does not say so on illumos."""
    return bool(re.search(
        'faulted in response to IO failures|pool is suspended',
        out('zpool', 'status', pool)))


def pool_health(pool):
    return out('zpool', 'list', '-H', '-o', 'health', pool).strip()


def destroy_pool(pool):
    """Destroy a pool the suite left behind.  Destroying a suspended pool can
    hang, but zpool clear resumes it if what made its I/O fail is gone, e.g.
    a tunable that the check has undone.  Not if it is faulted: that needs a
    reboot."""
    if pool_suspended(pool):
        # zpool clear sleeps in the kernel if the pool suspends again.
        run('zpool', 'clear', pool, timeout=60)
        if pool_suspended(pool):
            raise Failed('pool %s is suspended' % pool)
    health = pool_health(pool)
    if health not in ('ONLINE', 'DEGRADED'):
        raise Failed('pool %s is %s' % (pool, health))
    must('zpool', 'destroy', '-f', pool, timeout=300)


# The state a test must leave as it found it, one "<type> <name> [value]"
# line per item.

def state_datasets(pools):
    """The datasets, snapshots and bookmarks in the suite's pools that are
    not suspended."""
    return ['dataset ' + name
            for pool in pools
            for name in lines(out('zfs', 'list', '-H', '-t', 'all', '-o',
                                  'name', '-r', pool))
            if re.search('[/@#]', name)]


def linux_tunable(name, text):
    """The state of a tunable (module/parameter) that reads as text.  For a
    choice like "cycle [fastest] original", keep the choice."""
    if name in UNSTABLE_TUNABLES:
        return []
    return ['tunable %s %s' % (name, re.sub(r'.*\[([^]]*)\].*', r'\1', value))
            for value in lines(text) if value]


def sunos_tunable_sizes(symbols, names):
    """The lines of the tunable list: the name and size (in hex) of each of
    the named kernel variables in the output of mdb's ::nm."""
    sizes = []
    for line in lines(symbols):
        field = line.split('|')
        if len(field) >= 7 and 'OBJT' in field[2]:
            name = field[6].replace(' ', '')
            if name in names:
                sizes.append(name + ' ' + re.sub('^0x0*', '', field[1]))
    return sizes


def sunos_tunable_list():
    """The size of each tunable, found once per run."""
    try:
        os.mkdir(HYGIENE_DIR, 0o755)
    except FileExistsError:
        pass
    try:
        st = os.lstat(HYGIENE_DIR)
        trusted = (stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and
                   not st.st_mode & 0o022)
    except OSError:
        trusted = False
    sizes = lines(read(TUNABLE_LIST)) if trusted else []
    if not sizes:
        # The kernel variables the tests change.  kmem_mtb_move is set with
        # mdb -kw by tests on some branches.
        names = {'kmem_mtb_move'}
        for top in (STF_SUITE + '/tests', STF_SUITE + '/include'):
            for path in find(top, stat.S_ISREG):
                names.update(TUNABLE_USE.findall(read(path)))
        symbols = out('mdb', '-k', stdin='::nm\n')
        sizes = sunos_tunable_sizes(symbols, names)
        if trusted:
            write(TUNABLE_LIST, sizes)
    # Only names go to mdb, where a line starting with ! runs a command.
    return {row[0]: row[1] for row in rows('\n'.join(sizes))
            if re.match('[a-z_0-9]+$', row[0])}


def sunos_tunables(mdb_output):
    """The state of the tunables that mdb printed with name/X or name/J."""
    values = [line.split(':') for line in lines(mdb_output)]
    return ['tunable %s 0x%s' % (v[0], re.sub('[ \t]', '', v[1]))
            for v in values if len(v) == 2 and v[1].strip(' ')]


def state_tunables():
    if UNAME == 'Linux':
        return [line
                for module in ('zfs', 'spl')
                for path in find(PARAMETERS % module, lambda mode:
                                 stat.S_ISREG(mode) and mode & stat.S_IWUSR)
                for line in linux_tunable(
                    module + '/' + os.path.basename(path), read(path))]
    if UNAME == 'FreeBSD':
        return ['tunable ' + line.replace('=', ' ', 1)
                for line in lines(out('sysctl', '-e', 'vfs.zfs'))]
    formats = {'4': 'X', '8': 'J'}
    prints = ['%s/%s\n' % (name, formats[size])
              for name, size in sunos_tunable_list().items()
              if size in formats]
    return sunos_tunables(out('mdb', '-k', stdin=''.join(prints)))


def set_tunable(name, value):
    if UNAME == 'Linux':
        module, _, parameter = name.partition('/')
        try:
            write(PARAMETERS % module + '/' + parameter, [value])
        except OSError as e:
            raise Failed(str(e))
    elif UNAME == 'FreeBSD':
        must('sysctl', '%s=%s' % (name, value))
    else:
        fmt = {'4': 'W', '8': 'Z'}.get(sunos_tunable_list().get(name))
        if not fmt:
            raise Failed()
        must('mdb', '-kw', stdin='%s/%s %s\n' % (name, fmt, value))


def zinject_handlers():
    """The words of each zinject handler: its id, its pool, ...; the last
    ones count the I/O it matched."""
    handlers = [words(line) for line in lines(out('zinject'))]
    return [h for h in handlers if h and re.match('[0-9]+$', h[0])]


def other_mounts():
    """Mounts that are not ZFS datasets: on disks, loop devices, zvols, or in
    the test directory."""
    return [(mnt, fstype, dev) for dev, mnt, fstype in mounts()
            if fstype != 'zfs' and
            (dev.startswith('/dev/') or under_basedir(mnt))]


def loop_devices():
    """"<device> <backing file>" for each loop device.  losetup pads the
    device names to the same width."""
    loops = []
    for line in lines(out('losetup', '-nO', 'NAME,BACK-FILE', '-l')):
        dev, _, backing = line.strip(' ').partition(' ')
        loops.append(('%s %s' % (dev, backing.strip(' '))).rstrip(' '))
    return loops


def swap_devices():
    if UNAME == 'Linux':
        text = read('/proc/swaps')
    elif UNAME == 'SunOS':
        text = out('swap', '-l')
    else:
        text = out('swapctl', '-l')
    return [row[0] for row in rows(text, skip=1)]


def dump_devices():
    """The dump device (illumos), as a list."""
    if UNAME != 'SunOS':
        return []
    return [row[2] for row in rows(out('dumpadm'))
            if row[:2] == ['Dump', 'device:']]


def lofi_devices():
    """[device, backing file] for each lofi device."""
    return [row[:2] for row in rows(out('lofiadm'), skip=1)]


def state_devices():
    state = ['swap ' + dev for dev in swap_devices()]
    state += ['dump ' + dev for dev in dump_devices()]
    if UNAME == 'Linux':
        state += ['loop ' + loop for loop in loop_devices()]
        state += ['dm ' + row[0] for row in rows(out('dmsetup', 'ls'))
                  if row[0] != 'No']
        if os.path.isdir('/sys/module/scsi_debug'):
            state.append('module scsi_debug')
        # A write to /dev/zvol/... before udev makes the link leaves a
        # regular file there that udev never replaces.
        state += ['zvolfile ' + f for f in find('/dev/zvol', stat.S_ISREG)]
    elif UNAME == 'SunOS':
        state += ['lofi %s %s' % tuple(lofi) for lofi in lofi_devices()]
    else:
        state += ['md %s %s' % (row[0], row[3])
                  for row in rows(out('mdconfig', '-lv'))]
    return state


def accounts():
    """The name and home directory of each user."""
    users = [line.split(':') for line in lines(read('/etc/passwd'))]
    return [(user[0], (user + [''] * 5)[5]) for user in users]


def groups():
    return [line.split(':')[0] for line in lines(read('/etc/group'))]


def test_files():
    """The files and directories the tests made in the test directory."""
    try:
        names = os.listdir(BASEDIR)
    except OSError:
        names = []
    return [name for name in lines('\n'.join(names))
            if not NOT_TEST_FILES.fullmatch(name)]


def state_misc():
    state = ['hostid ' + out('hostid').rstrip('\n')]
    state += ['user %s %s' % account for account in accounts()]
    state += ['group ' + group for group in groups()]
    if UNAME == 'SunOS':
        state += ['share ' + row[1] for row in rows(out('share'))]
    else:
        exports = (lines(read('/etc/exports.d/zfs.exports')) +
                   lines(read('/etc/zfs/exports')))
        state += ['share ' + words(line)[0] for line in exports
                  if not line.startswith('#') and words(line)]
    if ok('pgrep', '-x', 'zed'):
        state.append('process zed')
    return state + ['file ' + name for name in test_files()]


def state_arc():
    """The ARC's size limits.  Writing 0 to zfs_arc_max or zfs_arc_min does not
    undo an earlier non-zero value, so the limits can stay changed while the
    tunables read as before."""
    if UNAME == 'Linux':
        return ['arc %s %s' % (row[0], row[2])
                for row in rows(read('/proc/spl/kstat/zfs/arcstats'))
                if row[0] in ('c_max', 'c_min')]
    if UNAME == 'SunOS':
        stats = out('kstat', '-p', 'zfs:0:arcstats:c_max',
                    'zfs:0:arcstats:c_min')
    else:
        stats = out('sysctl', 'kstat.zfs.misc.arcstats.c_max',
                    'kstat.zfs.misc.arcstats.c_min')
    return ['arc %s %s' % (re.sub(r'.*[:.]', '', row[0].rstrip(':')), row[1])
            for row in rows(stats)]


def state():
    """The state, sorted as sort does in the C locale."""
    pools = imported_pools()
    test_pools = [pool for pool in pools if is_test_pool(pool)]
    suspended = [pool for pool in test_pools if pool_suspended(pool)]
    found = (['pool ' + pool for pool in pools] +
             ['suspended ' + pool for pool in suspended] +
             state_datasets([pool for pool in test_pools
                             if pool not in suspended]) +
             state_tunables() + state_arc() +
             ['zinject %s %s' % tuple((h + [''])[:2])
              for h in zinject_handlers()] +
             ['mount %s %s %s' % tuple(escape(field) for field in mount)
              for mount in other_mounts()] +
             state_devices() + state_misc())
    return sorted(set('\n'.join(found).split('\n')), key=os.fsencode)


# What changed between two states.

def key(line):
    """What identifies a state line; the rest of the line is its value."""
    field = words(line) + ['', '']
    if field[0] in ('hostid', 'dump'):
        return field[0]
    if field[0] in ('tunable', 'arc'):
        return '%s %s' % (field[0], field[1])
    return line


def value(line):
    return line[len(key(line)) + 1:]


class Change:
    """One difference between two states: a state line that is new ("leaked"),
    is gone ("removed"), or has another value than the line it was
    ("changed").  The text is what the check reports:
        leaked <line>
        removed <line>
        changed <type> <name> <old> -> <new>
    """
    def __init__(self, what, line, was=None):
        self.what, self.line, self.was = what, line, was
        self.kind, self.name = (words(line) + ['', ''])[:2]
        # The line without its type: "<name> [value]".
        self.rest = line[len(self.kind) + 1:]
        self.text = '%s %s' % (what, line)
        if what == 'changed':
            self.text = 'changed %s %s -> %s' % (
                key(line), value(was), value(line))


def state_diff(old, new):
    """The changes from the old state to the new one, sorted by text."""
    was = {key(line): line for line in old}
    now = {key(line) for line in new}
    changes = [Change('leaked', line) for line in new if key(line) not in was]
    changes += [Change('changed', line, was[key(line)]) for line in new
                if was.get(key(line), line) != line]
    changes += [Change('removed', line) for k, line in was.items()
                if k not in now]
    return sorted(changes, key=lambda change: os.fsencode(change.text))


def to_report(changes, base, group_base, reported, is_test):
    """Leave out the changes that are not the leak of the script that just
    ran.  base and group_base are the states before the first script of
    this statefile and of the group, reported is what was reported before,
    and is_test says that the script is a test, not a setup or cleanup."""
    gone = {change.name for change in changes if change.kind == 'pool'}

    def skip(change):
        what, line = change.what, change.line
        return (
            # The tests of a group may set the hostid, which its setup
            # saves and its cleanup restores (mmp): check it for the whole
            # group.
            (is_test and what == 'changed' and change.kind == 'hostid') or
            # What the group setup made is the group to manage (a test that
            # destroys it is left to the tests after it), and what an
            # earlier test made was a leak by that test.
            (what == 'removed' and line not in group_base) or
            # Something there before the first test is not a leak.
            (what == 'leaked' and line in base) or
            # Setup recreates the testdir* mount points.
            change.text.startswith('removed file testdir') or
            # Reported before and not undone: do not blame the group
            # cleanup.
            change.text.strip(' \t') in reported or
            # The datasets of a pool made or destroyed went with it.
            (change.kind == 'dataset' and
             re.split('[/@#]', change.name)[0] in gone) or
            # A leaked pool that is suspended is reported as a pool.
            (change.kind == 'suspended' and change.name in gone))

    return [change for change in changes if not skip(change)]


def still_true(text, now):
    """Does a reported change still describe the state now (a set of
    lines)?  A leak that is gone, e.g. removed by a cleanup, may come back
    from a later test, which is then to blame for it."""
    what, _, rest = text.partition(' ')
    if what == 'leaked':
        return rest in now
    if what == 'removed':
        return rest not in now
    if what == 'changed':
        return any(rest.startswith(key(line) + ' ') and
                   rest.endswith(' -> ' + value(line)) for line in now)
    return True


def file_owner(path):
    try:
        uid = os.lstat(path).st_uid
        return pwd.getpwuid(uid).pw_name
    except OSError as e:
        raise Failed(str(e))
    except KeyError:
        return str(uid)


def owned(is_ours):
    """Only undo a change to something that is the suite's."""
    if not is_ours:
        raise Failed()


def undo(change):
    """Undo one change, if it is safe without knowing the test: put tunables
    back, and remove what the test added and the suite owns.  Raise Failed
    if it was not undone."""
    op, name = '%s %s' % (change.what, change.kind), change.name
    if op == 'changed tunable':
        set_tunable(name, value(change.was))
    elif op == 'leaked zinject':     # zinject <id> <pool>
        owned((words(change.rest) + ['', ''])[1] not in KEEP_POOLS)
        must('zinject', '-c', name)
    elif op == 'leaked mount':      # mount <mountpoint> <fstype> <special>
        field = change.rest.rsplit(' ', 2)
        owned(len(field) == 3)
        mnt, _, special = [unescape(f) for f in field]
        owned(under_basedir(mnt) or is_test_vdev(special))
        must('umount', '-f', mnt)
    elif op == 'leaked dataset':
        # Names may have spaces.  Its descendants and clones are leaks
        # too, and may be gone already.
        dataset = change.rest
        if ok('zfs', 'list', '-t', 'all', dataset):
            recurse = [] if '#' in dataset else ['-Rf']
            must('zfs', 'destroy', *(recurse + [dataset]))
    elif op == 'leaked pool':
        # After the tunables, which may have suspended it.
        owned(is_test_pool(name))
        destroy_pool(name)
    elif op == 'changed dump':
        must('dumpadm', '-d', value(change.was).strip())
    elif op == 'leaked process':
        # A test zed (zed_start()) left running.
        owned(name == 'zed')
        must('pkill', '-f', test_zed_pattern())
    elif op in ('leaked loop', 'leaked lofi'):      # <device> <file>
        owned(under_basedir(change.rest[len(name) + 1:]))
        must('losetup' if change.kind == 'loop' else 'lofiadm', '-d', name)
    elif op == 'leaked swap':
        owned(is_test_vdev(name))
        must(*(['swap', '-d'] if UNAME == 'SunOS' else ['swapoff']), name)
    elif op == 'leaked user':       # user <name> <home>
        home = (words(change.line) + ['', ''])[2]
        owned(is_test_home(name, home))
        del_account('user', name)
        must('rm', '-rf', home)
    elif op == 'leaked group':
        owned(name in TEST_GROUPS)
        del_account('group', name)
    elif op == 'leaked zvolfile':
        owned(name.startswith('/dev/zvol/'))
        must('rm', '-f', name)
    elif op == 'leaked module':
        must('modprobe', '-r', name)
    elif op == 'leaked file':
        # Only what the tests (root or a test user) or the runner's user
        # made, and not if something is mounted in it.
        path = BASEDIR + '/' + change.rest
        owned(file_owner(path) in ['root', SUDO_USER] + TEST_USERS)
        owned(not mounted_under(path))
        must('rm', '-rf', path)
    else:
        raise Failed()


UNDONE = ' (undone)'


def undo_result(change):
    """Undo a change, and return what to add to its report."""
    try:
        undo(change)
        return UNDONE
    except Failed as e:
        error = ' '.join(str(e).split())
        return ' (undo failed: %s)' % cut(error, 160) if error else ''


def test_zed_pattern():
    """How pgrep and pkill find a zed that zed_start() started."""
    return 'zed .*-d %s/zed' % BASEDIR


def ps(pid, column):
    """A column of ps for one process; nothing if it is gone."""
    return out('ps', '-o', column + '=', '-p', pid).rstrip('\n')


def check_processes(statefile, label, sid, report_only):
    """Report and kill the processes a test left running, e.g. background
    jobs.  The runner starts each test in a session of its own."""
    zed_was_running = 'process zed' in lines(read(statefile))
    for pid in out('pgrep', '-s', sid).split():
        args = ps(pid, 'args')
        if not args:
            continue        # It exited since pgrep.
        # A test may restart the zed that was running before it (zed_stop,
        # then zed_start).
        command = ps(pid, 'comm')
        if command in ('zed', 'lt-zed') and zed_was_running:
            continue
        leak = '%s: leaked process %s %s' % (label, pid, cut(args, 100))
        if report_only:
            print(leak)
            continue
        run('kill', '-9', pid)
        # A process stuck in the kernel does not die.
        for _ in range(10):
            if not ok('kill', '-0', pid):
                break
            time.sleep(0.5)
        if ok('kill', '-0', pid):
            print('%s (undo failed: still running, state %s)' % (
                leak, ps(pid, 's')))
        else:
            print(leak + UNDONE)


def is_empty_testdir(change, remove):
    """Setup recreates the testdir* mount points, so an empty one left behind
    does no harm.  Remove it, without a report."""
    if not change.text.startswith('leaked file testdir'):
        return False
    path = BASEDIR + '/' + change.rest
    try:
        if remove:
            os.rmdir(path)
            return True
        return not (os.path.islink(path) or os.listdir(path))
    except OSError:
        return False


def check(statefile, label, sid='', group='', report_only=False):
    if sid:
        check_processes(statefile, label, sid, report_only)

    was = lines(read(statefile))

    def state_held():
        """The state, with the datasets of the pools that are suspended as
        they were: they can't be listed, and when the pool resumes, the
        ones that differ are the changes."""
        new = state()
        held = {line[len('suspended '):] for line in new
                if line.startswith('suspended ')}
        new += [line for line in was if line.startswith('dataset ') and
                re.split('[/@#]', line[len('dataset '):])[0] in held]
        return sorted(set(new), key=os.fsencode)

    now = state_held()
    base = statefile + '.base'
    if not os.path.isfile(statefile):
        write(base, now)
        write(statefile, now)
        return

    group_base = group + '.base'
    if not os.path.isfile(group_base):
        group_base = base
    reported = os.path.join(os.path.dirname(statefile), '.leakcheck-reported')
    was_reported = lines(read(reported))
    current = {line.strip(' \t') for line in now}
    still = [text for text in was_reported if still_true(text, current)]
    if not report_only and still != was_reported:
        write(reported, still)
    changes = to_report(
        state_diff(was, now), set(lines(read(base))),
        set(lines(read(group_base))), set(still),
        is_test=bool(sid))      # Only a test's check has a session id.

    def undo_order(change):
        if change.kind not in UNDO_ORDER:
            return (len(UNDO_ORDER), 0)
        depth = 0
        if change.kind == 'mount':      # Unmount the deepest first.
            depth = -change.rest.rsplit(' ', 2)[0].count('/')
        return (UNDO_ORDER.index(change.kind), depth)

    for change in sorted(changes, key=undo_order):
        if is_empty_testdir(change, remove=not report_only):
            continue
        text = change.text.strip(' \t')
        result = '' if report_only else undo_result(change)
        print('%s: %s%s' % (label, text, result))
        if result != UNDONE:
            write(reported, [text], mode='ab')
    # With no changes, nothing was undone or removed: the state is as found.
    write(statefile, state_held() if changes and not report_only else now)


class Preflight:
    """The pre-flight checks."""

    # What fix() can do that is not a command.
    actions = {'destroy_pool': destroy_pool, 'del_account': del_account,
               'set_tunable': set_tunable}

    def __init__(self, dryrun=False):
        self.dryrun = dryrun
        self.problems = 0
        self.cleaned = 0

    def problem(self, text):
        """Something is wrong that we leave alone: the run must not start."""
        msg('PROBLEM: ' + text)
        self.problems += 1

    def fix(self, what, *command, last=True):
        """Run a cleanup command, or only say what it would be with -n.
        Return whether it is done.  If it fails and is not the last try,
        it is not a problem yet."""
        shown = ' '.join(command)
        if self.dryrun:
            self.problem('%s (would run: %s)' % (what, shown))
            return True
        try:
            if command[0] in self.actions:
                self.actions[command[0]](*command[1:])
            else:
                must(*command)
        except Failed:
            if last:
                self.problem('%s (failed: %s)' % (what, shown))
            return False
        msg('cleaned: %s (%s)' % (what, shown))
        self.cleaned += 1
        return True

    def check_env(self):
        runner = (r'^[^ ]*python[0-9.]* [^ ]*'
                  r'(test-runner\.py|/bin/run)( |$)')
        if ok('pgrep', '-f', runner):
            self.problem('another test runner is running: ' + ''.join(
                lines(out('pgrep', '-fl', runner))[:1]))
        disks = test_disks()
        dups = sorted({disk for disk in disks if disks.count(disk) > 1})
        if dups:
            self.problem('DISKS lists %s more than once' % ' '.join(dups))
        # The suite takes the whole disk of each entry as its own.
        for dev in DISKS.split():
            if not is_whole_disk(dev):
                self.problem('DISKS includes %s, which is not a whole disk; '
                             'the suite would take all of %s' % (
                                 dev, disk_of(dev)))
        if SUDO_USER in TEST_USERS:
            self.problem('the tests create and delete user %s; run the '
                         'suite as another user' % SUDO_USER)
        # The trim and initialize tests measure file vdevs with du.
        comp = out('zfs', 'list', '-H', '-o', 'compression', BASEDIR).strip()
        if comp not in ('', 'off'):
            msg('note: %s is on a ZFS file system with compression=%s; '
                'tests that check the space their file vdevs use (e.g. '
                'zpool_trim_verify_trimmed) will fail' % (BASEDIR, comp))
        df = words((lines(out('df', '-k', BASEDIR)) or [''])[-1])
        avail = df[-3] if len(df) > 2 else ''
        if re.match('[0-9]+$', avail) and int(avail) < 2 * 1024 * 1024:
            self.problem('only %d MB free in %s' % (
                int(avail) // 1024, BASEDIR))

        # A disk in $DISKS that holds a file system mounted outside the test
        # directory, swap or dump is not the suite's: DISKS is wrong, e.g.
        # because device names changed at boot.
        for dev, mnt, fstype in mounts():
            if (dev.startswith('/dev/') and fstype != 'zfs' and
                    not under_basedir(mnt) and is_test_disk(dev)):
                self.problem('DISKS includes %s, which has %s mounted; '
                             'check DISKS' % (disk_of(dev), mnt))
        if UNAME != 'FreeBSD':
            for dev in swap_devices() + dump_devices():
                if not is_zvol(dev) and is_test_disk(dev):
                    self.problem('DISKS includes %s, which holds swap or '
                                 'dump; check DISKS' % disk_of(dev))

    def check_foreign_pools(self):
        """Imported pools that are not the suite's but are in its way."""
        for pool in imported_pools():
            if pool in KEEP_POOLS:
                if uses_test_disk(pool):
                    self.problem('kept pool %s uses DISKS' % pool)
            elif is_test_pool(pool):
                pass
            elif pool.startswith('testpool') or uses_test_disk(pool):
                self.problem(' '.join(
                    ['pool %s uses devices outside DISKS and the test '
                     'directory:' % pool] + pool_vdevs(pool)))
            elif UNAME == 'SunOS':
                # default_cleanup destroys every pool not in KEEP.
                self.problem('pool %s is not in KEEP; the suite would '
                             'destroy it' % pool)

    def clean_zinject(self):
        for handler in zinject_handlers():
            ident, pool = (handler + [''])[:2]
            if pool in KEEP_POOLS:
                msg('note: zinject handler %s on kept pool %s' % (ident, pool))
            else:
                self.fix('zinject handler %s on %s' % (ident, pool),
                         'zinject', '-c', ident)

    def clean_mounts(self):
        # The deepest first: a file system mounted in another keeps it busy.
        for mnt, fstype, dev in sorted(other_mounts(), reverse=True,
                                       key=lambda m: m[0].count('/')):
            if under_basedir(mnt) or is_test_vdev(dev):
                self.fix('%s mount of %s on %s' % (fstype, dev, mnt),
                         'umount', '-f', mnt)

    def clean_swap(self):
        """Swap and dump devices on test pools' zvols keep the pools busy."""
        if UNAME == 'FreeBSD':
            return
        swapoff = ['swap', '-d'] if UNAME == 'SunOS' else ['swapoff']
        for dev in swap_devices():
            if is_test_vdev(dev):
                self.fix('swap on ' + dev, *(swapoff + [dev]))
        for dev in dump_devices():
            if not is_test_vdev(dev):
                continue
            # Move it to the dump zvol of a pool that stays.
            kept = [path for path in sorted(glob.glob('/dev/zvol/dsk/*/dump'))
                    if zvol_pool(path) in KEEP_POOLS]
            if kept:
                self.fix('dump device on ' + dev, 'dumpadm', '-d', kept[0])
            else:
                self.problem('dump device is %s; move it with dumpadm -d' %
                             dev)

    def clean_pools(self):
        """Destroy the pools the suite created and left behind; more than one
        pass for pools on other test pools' zvols."""
        done = set()
        for last in (False, False, True):
            for pool in imported_pools():
                if (pool in KEEP_POOLS or pool in done or
                        not is_test_pool(pool)):
                    continue
                health = pool_health(pool)
                if pool_suspended(pool):
                    health = 'SUSPENDED'
                if health in ('ONLINE', 'DEGRADED'):
                    if self.fix('destroy leftover pool ' + pool,
                                'destroy_pool', pool, last=last):
                        done.add(pool)
                elif last:
                    self.problem('leftover pool %s is %s; clear it or '
                                 'reboot' % (pool, health))

    def clean_devices(self):
        """Loop and lofi devices on test files, and scsi_debug."""
        if UNAME == 'Linux':
            for loop in loop_devices():
                dev, _, backing = loop.partition(' ')
                if not under_basedir(backing):
                    continue
                for dm in [row[0] for row in rows(out('dmsetup', 'ls'))]:
                    deps = out('dmsetup', 'deps', '-o', 'devname', dm)
                    if '(%s)' % dev.split('/')[-1] in deps:
                        self.fix('dm device %s on %s' % (dm, dev),
                                 'dmsetup', 'remove', dm)
                self.fix('loop device %s on %s' % (dev, backing),
                         'losetup', '-d', dev)
            if os.path.isdir('/sys/module/scsi_debug'):
                self.fix('scsi_debug module loaded',
                         'modprobe', '-r', 'scsi_debug')
            for path in find('/dev/zvol', stat.S_ISREG):
                self.fix('regular file %s in /dev/zvol' % path,
                         'rm', '-f', path)
        elif UNAME == 'SunOS':
            for dev, backing in lofi_devices():
                if under_basedir(backing):
                    self.fix('lofi device %s on %s' % (dev, backing),
                             'lofiadm', '-d', dev)

    @staticmethod
    def disk_paths(disk):
        if UNAME == 'Linux':
            return out('lsblk', '-lnpo', 'NAME', '/dev/' + disk).split()
        if UNAME == 'SunOS':
            slices = ['/dev/rdsk/%ss%d' % (disk, s)
                      for s in (0, 1, 2, 3, 4, 5, 6, 8)]
            return (['/dev/rdsk/%sp0' % disk] +
                    [path for path in slices if os.path.exists(path)])
        return sorted(path for path in glob.glob('/dev/%s*' %
                                                 glob.escape(disk))
                      if disk_of(path) == disk)

    def clean_disks(self):
        """Labels of exported or destroyed pools make "zpool import" find more
        than the test expects."""
        for disk in test_disks():
            for path in self.disk_paths(disk):
                if ok('zdb', '-l', path):
                    self.fix('ZFS label on ' + path,
                             'zpool', 'labelclear', '-f', path)

    def check_cachefile(self):
        """Pools in the cache file that are not imported come back at the next
        boot."""
        cache = '/etc/zfs/zpool.cache'
        if not os.path.isfile(cache):
            return
        imported = imported_pools()
        for line in lines(out('zdb', '-C', '-U', cache)):
            pool = line[:-1].strip()
            if re.match('[^ \t].*:$', line) and pool not in imported:
                msg('note: pool %s is in %s but not imported; the next boot '
                    'will try to import it' % (pool, cache))

    def clean_tunables(self):
        # Find the tunables again: the kernel may have changed since the
        # last run.
        if not self.dryrun and os.path.exists(TUNABLE_LIST):
            os.remove(TUNABLE_LIST)

        # Tunables that save_tunable() saved and an aborted test did not
        # restore.
        column = {'FreeBSD': 1, 'Linux': 2}.get(UNAME)
        names = {row[0]: row[column] for row in rows(
            read(STF_SUITE + '/include/tunables.cfg'))} if column else {}
        for path in sorted(glob.glob(glob.escape(BASEDIR) + '/tunable-*')):
            if not os.path.isfile(path):
                continue
            name = names.get(path.split('/tunable-')[-1], '')
            if name in ('', 'UNSUPPORTED'):
                self.problem('%s saves a tunable that this script cannot '
                             'restore; restore it and remove the file' % path)
                continue
            if UNAME == 'Linux':
                tunable = 'zfs/' + name
                new = read(PARAMETERS % 'zfs' + '/' + name)
            else:
                tunable = 'vfs.zfs.' + name
                new = out('sysctl', '-n', tunable)
            old, new = read(path).rstrip('\n'), new.rstrip('\n')
            if new != old and not self.fix(
                    'tunable %s is %s, saved %s' % (tunable, new, old),
                    'set_tunable', tunable, old):
                continue        # Keep what it should be.
            self.fix('saved tunable file ' + path, 'rm', '-f', path)

    def clean_users(self):
        for user, home in accounts():
            if user != SUDO_USER and is_test_home(user, home):
                self.fix('test user ' + user, 'del_account', 'user', user)
                if os.path.isdir(home):
                    self.fix('home of test user ' + user, 'rm', '-rf', home)
        existing = groups()
        for group in TEST_GROUPS:
            if group in existing:
                self.fix('test group ' + group, 'del_account', 'group', group)

    def clean_files(self):
        base = glob.escape(BASEDIR)
        # A test zed (zed_start()) that an aborted run left running.
        if ok('pgrep', '-f', test_zed_pattern()):
            self.fix('test zed', 'pkill', '-f', test_zed_pattern())
        for pattern in ('testdir*', 'tempfile*', 'zed', 'zts-results.*'):
            for path in sorted(glob.glob(base + '/' + pattern)):
                if not os.path.exists(path):
                    continue
                if mounted_under(path):
                    self.problem('leftover %s has a file system mounted in '
                                 'it' % path)
                else:
                    self.fix('leftover ' + path, 'rm', '-rf', path)
        # zfs-tests.sh refuses to start if its vdev files exist; remove them
        # unless a pool or a loop device still uses them.
        used = {row[0] for row in rows(out('zpool', 'list', '-vHP'))}
        for device in state_devices():
            used.update(words(device))
        for path in sorted(glob.glob(base + '/file-vdev*')):
            if os.path.isfile(path) and path not in used:
                self.fix('leftover vdev file ' + path, 'rm', '-f', path)

    def run(self):
        # See every pool, also the ones the run will exclude.
        os.environ.pop('__ZFS_POOL_EXCLUDE', None)
        os.environ.pop('__ZFS_POOL_RESTRICT', None)

        # Clean up nothing if DISKS looks wrong, a pool that is not ours is
        # in the way, or another run is going on.
        self.check_env()
        self.check_foreign_pools()
        if self.problems:
            msg('%d problem(s); fix them before running the ZFS test suite' %
                self.problems)
            return 1
        for step in (self.clean_zinject, self.clean_mounts, self.clean_swap,
                     self.clean_pools, self.clean_devices, self.clean_disks,
                     self.check_cachefile, self.clean_tunables,
                     self.clean_users, self.clean_files):
            step()

        if self.problems:
            msg('%d problem(s) left; fix them before running the ZFS test '
                'suite' % self.problems)
            return 1
        if self.cleaned:
            msg('cleaned up %d item(s) left by an earlier run' % self.cleaned)
        return 0


def parse_args(argv):
    """The command and its arguments.  A usage error exits with status 2."""
    parser = argparse.ArgumentParser(
        prog='zfs_hygiene', description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', metavar='command')
    preflight = commands.add_parser(
        'preflight', help='clean up before the suite starts')
    preflight.add_argument('-n', action='store_true', help='only report')
    check = commands.add_parser(
        'check', help='report and undo what a test left behind')
    check.add_argument('-n', action='store_true', help='only report')
    check.add_argument('statefile', help='the state that the last check saved')
    check.add_argument('label', help='what to start each report line with')
    check.add_argument('sid', nargs='?', default='',
                       help='the session the test ran in')
    check.add_argument('groupstatefile', nargs='?', default='',
                       help="the statefile of the test's group")
    commands.add_parser('state', help='print the state that check records')
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error('a command is required')
    return args


def main(argv):
    try:
        args = parse_args(argv)
    except SystemExit as e:
        return e.code
    os.environ['LC_ALL'] = 'C'
    stf_path = os.environ.get('STF_PATH')
    os.environ['PATH'] = ((stf_path + ':' if stf_path else '') +
                          '/usr/sbin:/usr/bin:/sbin:/bin')
    # Print names as they are, whatever bytes they hold.
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding=sys.getfilesystemencoding(),
        errors=sys.getfilesystemencodeerrors(), newline='\n',
        line_buffering=True)
    if os.geteuid() != 0:
        sys.stderr.write('zfs_hygiene: must run as root\n')
        return 2

    default_keep()

    status = 0
    if args.command == 'preflight':
        status = Preflight(dryrun=args.n).run()
    elif args.command == 'check':
        check(args.statefile, args.label, args.sid, args.groupstatefile,
              report_only=args.n)
    else:
        print('\n'.join(state()))
    sys.stdout.flush()
    return status


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
