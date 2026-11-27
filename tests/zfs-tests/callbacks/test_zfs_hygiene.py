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
Unit tests for zfs_hygiene.py.  They need no root, pools or disks: the
commands and the files of the system are faked.  Run them with

    python3 -m unittest test_zfs_hygiene
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import zfs_hygiene as hy

ZPOOL_LIST = ('zpool', 'list', '-H', '-o', 'name')
# What the tests fake.
REAL = {'read': hy.read, 'disk_of': hy.disk_of, 'find': hy.find}


class HygieneTest(unittest.TestCase):
    """
    A Linux machine with the test directory in a temporary directory, the
    disk sdb in DISKS, and only the commands and files a test gives it:
    self.commands maps a command line to its output, or to its (exit
    status, output, errors), and self.files maps a path to its contents.
    Every command that runs is recorded in self.ran.
    """
    uname = 'Linux'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.base = os.path.join(self.tmp, 'base')
        os.mkdir(self.base)
        self.commands = {}
        self.files = {}
        self.ran = []
        self.patch(UNAME=self.uname, BASEDIR=self.base, DISKS='sdb',
                   KEEP='rpool', KEEP_POOLS={'rpool'}, SUDO_USER='tester',
                   TEST_HOMES=(self.base, '/var/tmp'),
                   HYGIENE_DIR=self.tmp + '/hygiene',
                   TUNABLE_LIST=self.tmp + '/hygiene/tunables.list',
                   PARAMETERS=self.tmp + '/sys/%s', run=self.run_command,
                   read=self.read, disk_of=self.disk_of,
                   find=lambda top, is_wanted: (
                       [] if top == '/dev/zvol' else
                       REAL['find'](top, is_wanted)))
        real_isdir = os.path.isdir
        self.patch_os('isdir', lambda path: (
            False if path == '/sys/module/scsi_debug' else real_isdir(path)))
        hy.test_disks.cache_clear()
        self.addCleanup(hy.test_disks.cache_clear)

    def patch_os(self, name, value):
        patcher = mock.patch.object(hy.os.path, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def patch(self, **attributes):
        for name, value in attributes.items():
            patcher = mock.patch.object(hy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_command(self, *argv, **kwargs):
        self.ran.append(' '.join(argv))
        result = self.commands.get(' '.join(argv), (127, '', ''))
        if isinstance(result, str):
            result = (0, result, '')
        return hy.Result(*result)

    def read(self, path):
        if path in self.files:
            return self.files[path]
        return REAL['read'](path) if path.startswith(self.tmp) else ''

    @staticmethod
    def disk_of(dev):
        return dev.split('/')[-1].rstrip('0123456789')

    def pool(self, name, *vdevs):
        """Fake an imported pool on these vdevs."""
        pools = self.commands.get(' '.join(ZPOOL_LIST), '')
        self.commands[' '.join(ZPOOL_LIST)] = pools + name + '\n'
        self.commands['zpool list -vHP ' + name] = '%s\t1G\n%s' % (
            name, ''.join('\t%s\t1G\n' % vdev for vdev in vdevs))
        self.commands['zpool list -H -o health ' + name] = 'ONLINE\n'
        self.commands['zpool destroy -f ' + name] = ''

    def change(self, what, line, was=None):
        return hy.Change(what, line, was)

    def undone(self, what, line, was=None):
        """Whether the check undoes a change, and the commands it ran."""
        self.ran = []
        try:
            hy.undo(self.change(what, line, was))
            return True
        except hy.Failed:
            return False


class TextTest(HygieneTest):
    def test_lines_split_at_newlines_only(self):
        self.assertEqual(hy.lines('a\rb\x0cc\nd \n\n'),
                         ['a\rb\x0cc', 'd ', ''])
        self.assertEqual(hy.lines(''), [])
        self.assertEqual(hy.lines('no newline'), ['no newline'])

    def test_words_and_rows_are_awk_fields(self):
        self.assertEqual(hy.words('  a\t b\xa0c  '), ['a', 'b\xa0c'])
        row = hy.rows('header\n/dev/lofi/1\n', skip=1)[0]
        self.assertEqual((row[0], row[1]), ('/dev/lofi/1', ''))

    def test_odd_names_survive_a_statefile(self):
        odd = os.fsdecode(b'file caf\xe9 \xff\xfe \xc3\xa9t\xc3\xa9 *')
        state = ['file a\rb', odd, 'pool tank']
        path = os.path.join(self.tmp, 'statefile')
        hy.write(path, state)
        self.assertEqual(hy.lines(hy.read(path)), state)
        self.assertEqual(hy.state_diff(state, hy.lines(hy.read(path))), [])
        with open(path, 'rb') as f:
            self.assertIn(b'file caf\xe9 \xff\xfe ', f.read())

    def test_odd_names_can_be_printed(self):
        raw = io.BytesIO()
        stdout = io.TextIOWrapper(raw, encoding='ascii',
                                  errors='surrogateescape')
        with contextlib.redirect_stdout(stdout):
            print(hy.Change('leaked', os.fsdecode(b'file \xff\xc3')).text)
            stdout.flush()
        self.assertEqual(raw.getvalue(), b'leaked file \xff\xc3\n')

    def test_cut_counts_bytes(self):
        text = os.fsdecode(b'\xc3\xa9\xc3\xa9\xc3\xa9')
        self.assertEqual(os.fsencode(hy.cut(text, 3)), b'\xc3\xa9\xc3')

    def test_state_is_sorted_by_bytes_and_unique(self):
        names = ['b\nc', os.fsdecode(b'\xc3\xa9'), os.fsdecode(b'\xff'),
                 'B', 'b']
        with mock.patch.object(os, 'listdir', return_value=names):
            files = [line for line in hy.state() if line.startswith('file ')]
        self.assertEqual(files, sorted(set(files), key=os.fsencode))
        self.assertEqual(len(files), 5)
        self.assertIn('file c', files)


class DiffTest(HygieneTest):
    def diff(self, old, new):
        return [change.text for change in hy.state_diff(old, new)]

    def test_leaked_removed_and_changed(self):
        old = ['pool rpool', 'tunable zfs/a 1', 'user zfstest /home', 'file f']
        new = ['pool rpool', 'pool testpool', 'tunable zfs/a 2', 'file g']
        self.assertEqual(self.diff(old, new), [
            'changed tunable zfs/a 1 -> 2', 'leaked file g',
            'leaked pool testpool', 'removed file f',
            'removed user zfstest /home'])
        self.assertEqual(self.diff(old, old), [])

    def test_keys(self):
        self.assertEqual(hy.key('hostid 00bab10c'), 'hostid')
        self.assertEqual(hy.key('dump /dev/zvol/dsk/rpool/dump'), 'dump')
        self.assertEqual(hy.key('tunable zfs/x a b'), 'tunable zfs/x')
        self.assertEqual(hy.key('arc c_max 5'), 'arc c_max')
        self.assertEqual(hy.key('user a /home/a'), 'user a /home/a')
        self.assertEqual(hy.value('tunable zfs/x a b'), 'a b')
        self.assertEqual(hy.value('pool tank'), '')

    def test_only_keyed_types_change(self):
        self.assertEqual(
            self.diff(['hostid 1', 'dump /a', 'arc c_max 1', 'user u /a'],
                      ['hostid 2', 'dump /b', 'arc c_max 2', 'user u /b']),
            ['changed arc c_max 1 -> 2', 'changed dump /a -> /b',
             'changed hostid 1 -> 2', 'leaked user u /b', 'removed user u /a'])

    def test_values_with_spaces_and_no_value(self):
        self.assertEqual(
            self.diff(['tunable zfs/s a b', 'tunable vfs.zfs.e '],
                      ['tunable zfs/s c', 'tunable vfs.zfs.e x']),
            ['changed tunable vfs.zfs.e  -> x',
             'changed tunable zfs/s a b -> c'])

    def test_change_fields(self):
        change = hy.state_diff(['tunable zfs/s a b'], ['tunable zfs/s c'])[0]
        self.assertEqual((change.what, change.kind, change.name),
                         ('changed', 'tunable', 'zfs/s'))
        self.assertEqual(hy.value(change.was), 'a b')


class FilterTest(HygieneTest):
    def report(self, changes, base=(), group=(), reported=(), is_test=True):
        changes = [self.change(*change) for change in changes]
        return [change.text for change in hy.to_report(
            changes, set(base), set(group), set(reported), is_test)]

    def test_hostid_is_checked_for_the_group(self):
        change = [('changed', 'hostid 2', 'hostid 1')]
        self.assertEqual(self.report(change, is_test=True), [])
        self.assertEqual(self.report(change, is_test=False),
                         ['changed hostid 1 -> 2'])

    def test_removed_only_if_the_group_setup_made_it(self):
        changes = [('removed', 'dataset testpool/fs'),
                   ('removed', 'dataset testpool/earlier')]
        self.assertEqual(self.report(changes, group=['dataset testpool/fs']),
                         ['removed dataset testpool/fs'])

    def test_leak_from_before_the_first_test_is_not_a_leak(self):
        changes = [('leaked', 'file old'), ('leaked', 'file new')]
        self.assertEqual(self.report(changes, base=['file old']),
                         ['leaked file new'])

    def test_removed_testdir_is_not_reported(self):
        changes = [('removed', 'file testdir1'), ('removed', 'file other')]
        group = ['file testdir1', 'file other']
        self.assertEqual(self.report(changes, group=group),
                         ['removed file other'])

    def test_reported_before_is_not_reported_again(self):
        changes = [('leaked', 'loop /dev/loop0 /f  '), ('leaked', 'file f'),
                   ('changed', 'tunable zfs/a 2', 'tunable zfs/a 1')]
        reported = ['leaked loop /dev/loop0 /f',
                    'changed tunable zfs/a 1 -> 2']
        self.assertEqual(self.report(changes, reported=reported),
                         ['leaked file f'])

    def test_datasets_go_with_their_pool(self):
        changes = [('leaked', 'pool testpool'),
                   ('leaked', 'dataset testpool/fs@snap'),
                   ('leaked', 'dataset testpool2/fs'),
                   ('removed', 'pool gone'),
                   ('removed', 'dataset gone/fs#mark'),
                   ('removed', 'dataset gone'),
                   ('removed', 'dataset kept/fs')]
        group = [line for _, line in changes]
        self.assertEqual(self.report(changes, group=group), [
            'leaked pool testpool', 'leaked dataset testpool2/fs',
            'removed pool gone', 'removed dataset kept/fs'])

    def test_leaked_pool_that_is_suspended_is_a_leaked_pool(self):
        changes = [('leaked', 'pool testpool2'),
                   ('leaked', 'suspended testpool2')]
        self.assertEqual(self.report(changes), ['leaked pool testpool2'])

    def test_pool_filtered_out_still_takes_its_datasets(self):
        changes = [('removed', 'pool early'), ('removed', 'dataset early/fs')]
        self.assertEqual(self.report(changes, group=['dataset early/fs']), [])


class OwnershipTest(HygieneTest):
    def test_keep_is_a_list_or_an_expression(self):
        for keep in ('rpool data', '^(rpool|data)$', 'rpool\ndata'):
            pools = set(hy.re.sub(r'[\]\[^$|()]', ' ', keep).split())
            self.assertEqual(pools, {'rpool', 'data'})

    def test_default_keep_is_every_pool_the_suite_did_not_make(self):
        self.patch(KEEP='', KEEP_POOLS=set())
        self.pool('rpool', '/dev/sda1')
        self.pool('testpool', '/dev/sdb1')
        self.pool('tank', '/dev/sdc')
        hy.default_keep()
        self.assertEqual(hy.KEEP_POOLS, {'rpool', 'tank'})

    def test_test_vdevs(self):
        self.commands['losetup -nO BACK-FILE /dev/loop0'] = (
            self.base + '/file-vdev0\n')
        self.commands['losetup -nO BACK-FILE /dev/loop1'] = '/home/img\n'
        self.files['/sys/block/sdd/device/model'] = 'scsi_debug      \n'
        self.files['/sys/block/sdc/device/model'] = 'Samsung SSD\n'
        for path, expected in ((self.base + '/anything', True),
                               (self.base + 'x/file', False),
                               ('/dev/sdb1', True), ('/dev/sdc1', False),
                               ('/dev/loop0', True), ('/dev/loop1', False),
                               ('/dev/sdd', True), ('/var/tmp/other', False),
                               ('/dev/zvol/testpool/vol', True),
                               ('/dev/zvol/rpool/swap', False)):
            self.assertEqual(hy.is_test_vdev(path), expected, path)

    def test_zvol_pool(self):
        self.assertEqual(hy.zvol_pool('/dev/zvol/dsk/tank/a/b'), 'tank')
        self.assertEqual(hy.zvol_pool('/dev/zvol/rdsk/tank/vol'), 'tank')
        self.assertEqual(hy.zvol_pool('/dev/zvol/tank/vol-part1'), 'tank')
        self.assertEqual(hy.zvol_pool('/dev/zd16'), '')
        with mock.patch.object(hy, 'find', return_value=['/dev/zvol/t/v']), \
                mock.patch.object(os.path, 'realpath',
                                  return_value='/dev/zd16'):
            self.assertEqual(hy.zvol_pool('/dev/zd16'), 't')

    def test_test_pools(self):
        self.pool('testpool', '/dev/sdb1')
        self.pool('onfile', self.base + '/file-vdev0')
        self.pool('ontestdir', self.base + '/testdir/vdev')
        self.pool('weakfile', self.base + '/somefile')
        self.pool('testpool2', self.base + '/somefile')
        self.pool('onzvol', '/dev/zvol/testpool/vol')
        self.pool('testpool3', '/dev/zvol/testpool/vol')
        self.pool('mixed', '/dev/sdb2', '/dev/sdc1')
        self.pool('testpool4', '/dev/sdc1')
        self.pool('testpool5')
        self.pool('rpool', '/dev/sda1')
        made = [pool for pool in hy.imported_pools() if hy.is_test_pool(pool)]
        self.assertEqual(made, ['testpool', 'onfile', 'ontestdir',
                                'testpool2', 'testpool3'])
        self.assertTrue(hy.uses_test_disk('mixed'))
        self.assertFalse(hy.uses_test_disk('rpool'))

    def test_illumos_vdev_names(self):
        self.commands['zpool list -vHP tank'] = (
            'tank\t1G\n\tmirror-0\t1G\n\t/dev/dsk/c1t0d0s0\t1G\n'
            '\tc2t5000C500A1B2C3D4d0\t1G\n\tc3d1p0\t1G\nlogs\t-\n')
        self.assertEqual(hy.pool_vdevs('tank'), [
            '/dev/dsk/c1t0d0s0', '/dev/dsk/c2t5000C500A1B2C3D4d0',
            '/dev/dsk/c3d1p0'])

    def test_disk_names(self):
        self.patch(disk_of=REAL['disk_of'], UNAME='SunOS')
        self.assertEqual(hy.disk_of('/dev/dsk/c1t0d0s0'), 'c1t0d0')
        self.assertEqual(hy.disk_of('c1t0d0p0'), 'c1t0d0')
        self.assertEqual(hy.disk_of('c1t0d0'), 'c1t0d0')
        self.patch(UNAME='FreeBSD')
        self.assertEqual(hy.disk_of('/dev/da0p2'), 'da0')
        self.assertEqual(hy.disk_of('ada1s1a'), 'ada1')
        self.patch(UNAME='Linux')
        self.commands['lsblk -snlo NAME,TYPE /dev/sdx1'] = (
            'sdx1 part\nsdx  disk\n')
        with mock.patch.object(os.path, 'realpath', lambda path: path):
            self.assertEqual(hy.disk_of('sdx1'), 'sdx')
            self.assertEqual(hy.disk_of('/dev/nolsblk7'), 'nolsblk7')


class UndoTest(HygieneTest):
    def test_tunable_is_put_back(self):
        os.makedirs(self.tmp + '/sys/zfs')
        self.assertTrue(self.undone('changed', 'tunable zfs/zfs_x 2 3',
                                    'tunable zfs/zfs_x 1 2'))
        self.assertEqual(hy.read(self.tmp + '/sys/zfs/zfs_x'), '1 2\n')
        self.assertFalse(self.undone('changed', 'tunable nomod/x 2',
                                     'tunable nomod/x 1'))
        self.assertFalse(self.undone('leaked', 'tunable zfs/zfs_new 1'))

    def test_tunable_is_put_back_on_illumos_and_freebsd(self):
        self.patch(UNAME='SunOS')
        self.files[hy.TUNABLE_LIST] = 'zfs_a 4\nzfs_b 8\nzfs_c 1\n'
        self.commands['mdb -kw'] = ''
        self.assertTrue(self.undone('changed', 'tunable zfs_a 0x2',
                                    'tunable zfs_a 0x1'))
        self.assertFalse(self.undone('changed', 'tunable zfs_c 0x2',
                                     'tunable zfs_c 0x1'))
        self.patch(UNAME='FreeBSD')
        self.commands['sysctl vfs.zfs.a=1'] = ''
        self.assertTrue(self.undone('changed', 'tunable vfs.zfs.a 2',
                                    'tunable vfs.zfs.a 1'))

    def test_zinject_handler_is_cleared(self):
        self.commands['zinject -c 7'] = ''
        self.assertTrue(self.undone('leaked', 'zinject 7 testpool 0 -'))
        self.assertEqual(self.ran, ['zinject -c 7'])

    def test_mount_in_test_directory_or_on_test_device(self):
        mnt = self.base + '/mnt'
        self.commands['umount -f ' + mnt] = ''
        self.commands['umount -f /mnt'] = ''
        self.assertTrue(self.undone('leaked', 'mount %s ext4 /dev/sdc1' % mnt))
        self.assertTrue(self.undone('leaked', 'mount /mnt ext4 /dev/sdb1'))
        self.assertFalse(self.undone('leaked', 'mount /mnt ext4 /dev/sdc1'))
        self.assertNotIn('umount -f /mnt', self.ran)

    def test_dataset(self):
        self.commands['zfs list -t all testpool/a b'] = ''
        self.commands['zfs destroy -Rf testpool/a b'] = ''
        self.assertTrue(self.undone('leaked', 'dataset testpool/a b'))
        self.assertIn('zfs destroy -Rf testpool/a b', self.ran)
        self.commands['zfs list -t all testpool/fs#mark'] = ''
        self.commands['zfs destroy testpool/fs#mark'] = ''
        self.assertTrue(self.undone('leaked', 'dataset testpool/fs#mark'))
        self.assertIn('zfs destroy testpool/fs#mark', self.ran)
        # Gone with its parent.
        self.assertTrue(self.undone('leaked', 'dataset testpool/gone'))
        self.assertEqual(self.ran, ['zfs list -t all testpool/gone'])
        self.commands['zfs list -t all testpool/busy'] = ''
        self.commands['zfs destroy -Rf testpool/busy'] = (1, '', 'busy\n')
        self.assertFalse(self.undone('leaked', 'dataset testpool/busy'))

    def test_only_test_pools_are_destroyed(self):
        self.pool('testpool', '/dev/sdb1')
        self.pool('tank', '/dev/sdc1')
        self.assertTrue(self.undone('leaked', 'pool testpool'))
        self.assertIn('zpool destroy -f testpool', self.ran)
        self.assertFalse(self.undone('leaked', 'pool tank'))
        self.assertNotIn('zpool destroy -f tank', self.ran)

    def test_suspended_or_faulted_pool_is_not_destroyed(self):
        self.pool('testpool', '/dev/sdb1')
        self.commands['zpool status testpool'] = 'state: pool is suspended\n'
        with self.assertRaisesRegex(hy.Failed, 'testpool is suspended'):
            hy.undo(self.change('leaked', 'pool testpool'))
        self.assertIn('zpool clear testpool', self.ran)
        self.commands['zpool status testpool'] = 'state: FAULTED\n'
        self.commands['zpool list -H -o health testpool'] = 'FAULTED\n'
        with self.assertRaisesRegex(hy.Failed, 'testpool is FAULTED'):
            hy.undo(self.change('leaked', 'pool testpool'))
        self.assertNotIn('zpool destroy -f testpool', self.ran)

    def test_loop_and_lofi_devices_on_test_files(self):
        self.commands['losetup -d /dev/loop3'] = ''
        self.commands['lofiadm -d /dev/lofi/1'] = ''
        vdev = self.base + '/file-vdev1'
        self.assertTrue(self.undone('leaked', 'loop /dev/loop3 ' + vdev))
        self.assertFalse(self.undone('leaked', 'loop /dev/loop4 /home/img'))
        self.assertFalse(self.undone('leaked', 'loop /dev/loop5'))
        self.assertTrue(self.undone('leaked', 'lofi /dev/lofi/1 ' + vdev))
        self.assertFalse(self.undone('leaked', 'lofi /dev/lofi/2 /export/f'))

    def test_mount_with_a_blank_in_its_name(self):
        mnt = self.base + '/my dir'
        self.commands['umount -f ' + mnt] = ''
        self.files['/proc/self/mounts'] = (
            'tmpfs %s tmpfs rw 0 0\n' % mnt.replace(' ', '\\040'))
        self.assertEqual(hy.other_mounts(), [(mnt, 'tmpfs', 'tmpfs')])
        self.assertTrue(self.undone('leaked', 'mount %s tmpfs tmpfs' % mnt))
        self.assertEqual(self.ran, ['umount -f ' + mnt])
        self.assertFalse(self.undone('leaked', 'mount /my dir tmpfs tmpfs'))

    def test_swap_and_dump_before_datasets_and_pools(self):
        order = hy.UNDO_ORDER
        for kind in ('swap', 'dump'):
            self.assertLess(order.index(kind), order.index('dataset'))

    def test_zinject_on_kept_pool_is_left(self):
        self.commands['zinject -c 1'] = ''
        self.commands['zinject -c 2'] = ''
        self.assertFalse(self.undone('leaked', 'zinject 1 rpool'))
        self.assertTrue(self.undone('leaked', 'zinject 2 testpool'))
        self.assertEqual(self.ran, ['zinject -c 2'])

    def test_mount_with_a_newline_in_its_name(self):
        self.files['/proc/self/mounts'] = (
            'tmpfs %s/a\\012b\\134 tmpfs rw 0 0\n' % self.base)
        mnt = self.base + '/a\nb\\'
        self.assertEqual(hy.other_mounts(), [(mnt, 'tmpfs', 'tmpfs')])
        self.patch(state_tunables=lambda: [], state_arc=lambda: [],
                   imported_pools=lambda: [], zinject_handlers=lambda: [],
                   state_devices=lambda: [], state_misc=lambda: [])
        line = 'mount %s/a\\012b\\134 tmpfs tmpfs' % self.base
        self.assertEqual(hy.state(), [line])
        self.commands['umount -f ' + mnt] = ''
        self.assertTrue(self.undone('leaked', line))
        self.assertEqual(self.ran, ['umount -f ' + mnt])
        self.assertFalse(self.undone('leaked', 'mount ' + self.base + '/a'))

    def test_swap_on_test_devices(self):
        self.commands['swapoff /dev/zvol/testpool/swap'] = ''
        self.assertTrue(self.undone('leaked', 'swap /dev/zvol/testpool/swap'))
        self.assertFalse(self.undone('leaked', 'swap /dev/zvol/rpool/swap'))
        self.assertFalse(self.undone('leaked', 'swap /dev/sda2'))
        self.patch(UNAME='SunOS')
        self.commands['swap -d /dev/zvol/dsk/testpool/swap'] = ''
        self.assertTrue(
            self.undone('leaked', 'swap /dev/zvol/dsk/testpool/swap'))

    def test_dump_device_is_put_back(self):
        self.commands['dumpadm -d /dev/zvol/dsk/rpool/dump'] = ''
        self.assertTrue(self.undone('changed', 'dump /dev/zvol/dsk/t/vol',
                                    'dump /dev/zvol/dsk/rpool/dump'))

    def test_users_with_a_test_home(self):
        home = self.base + '/staff1'
        self.commands['userdel staff1'] = ''
        self.commands['rm -rf ' + home] = ''
        self.commands['userdel quser1'] = ''
        self.commands['rm -rf /var/tmp/quser1'] = ''
        self.assertTrue(self.undone('leaked', 'user staff1 ' + home))
        self.assertEqual(self.ran, ['userdel staff1', 'rm -rf ' + home])
        self.assertTrue(self.undone('leaked', 'user quser1 /var/tmp/quser1'))
        self.assertFalse(self.undone('leaked', 'user alice /home/alice'))
        self.assertFalse(self.undone('leaked', 'user staff1 /home/staff1'))
        self.assertFalse(self.undone('leaked', 'user a %s/b' % self.base))
        self.assertFalse(self.undone('removed', 'user staff1 ' + home))
        self.patch(UNAME='FreeBSD')
        self.commands['pw userdel staff1'] = ''
        self.assertTrue(self.undone('leaked', 'user staff1 ' + home))

    def test_groups_the_suite_creates(self):
        self.commands['groupdel zfsgrp'] = ''
        self.commands['groupdel wheel2'] = ''
        self.assertTrue(self.undone('leaked', 'group zfsgrp'))
        self.assertFalse(self.undone('leaked', 'group wheel2'))
        self.assertEqual(self.ran, [])

    def test_files_made_by_the_tests(self):
        path = self.base + '/a  b *'
        self.commands['rm -rf ' + path] = ''
        self.files['/proc/self/mounts'] = '/dev/sda1 / ext4 rw 0 0\n'
        for owner, expected in (('root', True), ('tester', True),
                                ('zfstest', True), ('alice', False)):
            self.patch(file_owner=lambda path, owner=owner: owner)
            self.assertEqual(self.undone('leaked', 'file a  b *'), expected)
            self.assertEqual(self.ran,
                             ['rm -rf ' + path] * expected)

    def test_file_with_a_mount_in_it_is_left(self):
        self.patch(file_owner=lambda path: 'root')
        self.commands['rm -rf %s/dir' % self.base] = ''
        self.files['/proc/self/mounts'] = (
            'tank/fs %s/dir/mnt zfs rw 0 0\n' % self.base)
        self.assertFalse(self.undone('leaked', 'file dir'))
        self.files['/proc/self/mounts'] = (
            'tank/fs %s/my\\040dir/mnt zfs rw 0 0\n' % self.base)
        self.assertFalse(self.undone('leaked', 'file my dir'))
        self.patch(UNAME='SunOS')
        self.files['/etc/mnttab'] = (
            'tank/fs\t%s/my dir/mnt\tzfs\trw\t0\n' % self.base)
        self.assertFalse(self.undone('leaked', 'file my dir'))
        self.patch(UNAME='Linux')
        self.files['/proc/self/mounts'] = (
            'tank/fs %s/directory zfs rw 0 0\n' % self.base)
        self.assertTrue(self.undone('leaked', 'file dir'))

    def test_file_owner(self):
        path = os.path.join(self.base, 'mine')
        open(path, 'w').close()
        self.assertEqual(hy.file_owner(path),
                         hy.pwd.getpwuid(os.getuid()).pw_name)
        with self.assertRaises(hy.Failed):
            hy.file_owner(path + '.gone')

    def test_others(self):
        self.commands['pkill -f zed .*-d %s/zed' % self.base] = ''
        self.commands['modprobe -r scsi_debug'] = ''
        self.commands['rm -f /dev/zvol/t/vol'] = ''
        self.assertTrue(self.undone('leaked', 'process zed'))
        self.assertFalse(self.undone('leaked', 'process other'))
        self.assertTrue(self.undone('leaked', 'module scsi_debug'))
        self.assertTrue(self.undone('leaked', 'zvolfile /dev/zvol/t/vol'))
        self.assertFalse(self.undone('leaked', 'zvolfile /dev/zd0'))
        for what, line in (('leaked', 'share /export'), ('leaked', 'dm x'),
                           ('removed', 'pool testpool'),
                           ('removed', 'file testfile'),
                           ('changed', 'hostid 2'), ('changed', 'arc c 2')):
            self.assertFalse(self.undone(what, line, line))
            self.assertEqual(self.ran, [])

    def test_undo_result(self):
        self.commands['zinject -c 1'] = (1, '', 'no  such\nhandler ' +
                                         'x' * 200)
        self.commands['zinject -c 2'] = ''
        result = hy.undo_result(self.change('leaked', 'zinject 1 testpool'))
        self.assertEqual(result, ' (undo failed: no such handler %s)' % (
            'x' * 144))
        self.assertEqual(
            hy.undo_result(self.change('leaked', 'zinject 2 p')), ' (undone)')
        self.assertEqual(
            hy.undo_result(self.change('leaked', 'group wheel2')), '')


class TunableTest(HygieneTest):
    def test_linux_values(self):
        self.assertEqual(hy.linux_tunable('zfs/zfs_arc_max', '0\n'),
                         ['tunable zfs/zfs_arc_max 0'])
        self.assertEqual(
            hy.linux_tunable('zfs/zfs_vdev_raidz_impl',
                             'cycle [fastest] original scalar\n'),
            ['tunable zfs/zfs_vdev_raidz_impl fastest'])
        self.assertEqual(hy.linux_tunable('zfs/a', 'x [a] y [b] z\n'),
                         ['tunable zfs/a b'])
        self.assertEqual(hy.linux_tunable('spl/spl_hostid', '0x1:2\n\nb c\n'),
                         ['tunable spl/spl_hostid 0x1:2',
                          'tunable spl/spl_hostid b c'])
        self.assertEqual(hy.linux_tunable('zfs/empty', '\n'), [])
        self.assertEqual(hy.linux_tunable('zfs/zfs_zevent_len_max', '512\n'),
                         [])
        self.assertEqual(hy.linux_tunable('zfs/zfs_dbgmsg_enable', '1\n'), [])

    def test_linux_state_is_the_writable_parameters(self):
        os.makedirs(self.tmp + '/sys/zfs')
        os.makedirs(self.tmp + '/sys/spl')
        for name, text, mode in (('zfs/zfs_a', '1\n', 0o644),
                                 ('zfs/zfs_ro', '2\n', 0o444),
                                 ('spl/spl_b', '3\n', 0o644)):
            path = self.tmp + '/sys/' + name
            hy.write(path, [text[:-1]])
            os.chmod(path, mode)
        self.assertEqual(hy.state_tunables(), ['tunable zfs/zfs_a 1',
                                               'tunable spl/spl_b 3'])

    def test_freebsd_state(self):
        self.patch(UNAME='FreeBSD')
        self.commands['sysctl -e vfs.zfs'] = (
            'vfs.zfs.arc.max=0\nvfs.zfs.version.module=v2=x\n')
        self.assertEqual(hy.state_tunables(), [
            'tunable vfs.zfs.arc.max 0',
            'tunable vfs.zfs.version.module v2=x'])

    def test_illumos_names_used_by_the_tests(self):
        script = ('log_must set_tunable64 zfs_arc_max 0\n'
                  'set_tunable32   "zfs_flags" 1; get_tunable zfs_dirty\n'
                  'save_tunable ARC_MAX\nrestore_tunable $name\n'
                  'unset_tunable64 zfs_x\n')
        self.assertEqual(hy.TUNABLE_USE.findall(script),
                         ['zfs_arc_max', 'zfs_flags', 'zfs_dirty', 'zfs_x'])

    def test_illumos_sizes(self):
        symbols = (
            'Value              |Size              |Type  |Bind |Other|'
            'Shndx   |Name\n'
            '0xfffffffffbc0e2a8|0x0000000000000008|OBJT |GLOB |0x0  |'
            '19      |zfs_arc_max\n'
            '0xfffffffffbc0e2b0|0x0000000000000004|OBJT |GLOB |0x0  |'
            '19      |zfs_flags\n'
            '0xfffffffffbc0e2b8|0x0000000000000010|OBJT |LOCL |0x0  |'
            '19      |zfs_big\n'
            '0xfffffffffb800000|0x0000000000000008|FUNC |GLOB |0x0  |'
            '1       |zfs_func\n'
            '0xfffffffffb800000|0x0000000000000008|OBJT |GLOB |0x0  |'
            '1       |not_a_tunable\n')
        names = {'zfs_arc_max', 'zfs_flags', 'zfs_big', 'zfs_func', 'absent'}
        self.assertEqual(hy.sunos_tunable_sizes(symbols, names),
                         ['zfs_arc_max 8', 'zfs_flags 4', 'zfs_big 10'])

    def test_illumos_values(self):
        output = ('zfs_arc_max:\n'
                  'zfs_arc_max:    0               \n'
                  'zfs_flags:\n'
                  'zfs_flags:      1f\n'
                  'mdb: failed to dereference symbol: unknown symbol name\n')
        self.assertEqual(hy.sunos_tunables(output),
                         ['tunable zfs_arc_max 0x0', 'tunable zfs_flags 0x1f'])

    def test_illumos_state(self):
        self.patch(UNAME='SunOS', STF_SUITE=self.tmp + '/suite')
        os.makedirs(self.tmp + '/suite/tests/functional')
        # Not writable by others, whatever the umask: the list is kept
        # only in a directory that no one else can write.
        os.mkdir(hy.HYGIENE_DIR, 0o755)
        os.chmod(hy.HYGIENE_DIR, 0o755)
        hy.write(self.tmp + '/suite/tests/functional/t.ksh',
                 ['log_must set_tunable32 zfs_flags 1'])
        self.commands['mdb -k'] = (
            '0x1|0x0000000000000004|OBJT |GLOB |0x0  |19      |zfs_flags\n'
            '0x2|0x0000000000000008|OBJT |GLOB |0x0  |19      |kmem_mtb_move\n'
            'zfs_flags:\nzfs_flags:      0\n')
        with mock.patch.object(hy, 'out', wraps=hy.out) as out:
            self.assertEqual(hy.state_tunables(), ['tunable zfs_flags 0x0'])
        self.assertEqual(out.call_args[1]['stdin'],
                         'zfs_flags/X\nkmem_mtb_move/J\n')
        self.assertEqual(hy.read(hy.TUNABLE_LIST),
                         'zfs_flags 4\nkmem_mtb_move 8\n')


class TunableListTest(HygieneTest):
    def setUp(self):
        super().setUp()
        self.patch(UNAME='SunOS', STF_SUITE=self.tmp + '/suite')
        os.makedirs(self.tmp + '/suite/tests')
        self.commands['mdb -k'] = (
            '0x1|0x0000000000000004|OBJT |GLOB |0x0  |19      |zfs_flags\n')
        hy.write(self.tmp + '/suite/tests/t.ksh', ['set_tunable32 zfs_flags'])

    def test_list_in_a_directory_others_can_write_is_not_used(self):
        os.mkdir(hy.HYGIENE_DIR, 0o777)
        os.chmod(hy.HYGIENE_DIR, 0o777)
        hy.write(hy.TUNABLE_LIST, ['!/tmp/x; 4', 'zfs_a 4'])
        self.assertEqual(hy.sunos_tunable_list(), {'zfs_flags': '4'})
        self.assertEqual(hy.lines(hy.read(hy.TUNABLE_LIST)),
                         ['!/tmp/x; 4', 'zfs_a 4'])

    def test_symlink_is_not_used(self):
        os.mkdir(self.tmp + '/elsewhere')
        os.symlink(self.tmp + '/elsewhere', hy.HYGIENE_DIR)
        self.assertEqual(hy.sunos_tunable_list(), {'zfs_flags': '4'})
        self.assertEqual(os.listdir(self.tmp + '/elsewhere'), [])

    def test_only_names_go_to_mdb(self):
        os.mkdir(hy.HYGIENE_DIR, 0o755)
        hy.write(hy.TUNABLE_LIST, ['!/tmp/x; 4', 'zfs_a 4'])
        self.assertEqual(hy.sunos_tunable_list(), {'zfs_a': '4'})


class StateTest(HygieneTest):
    def test_suspended_pool(self):
        self.pool('testpool', '/dev/sdb1')
        self.commands['zpool status testpool'] = 'pool is suspended\n'
        self.patch(state_tunables=lambda: [], state_arc=lambda: [],
                   other_mounts=lambda: [], state_devices=lambda: [],
                   state_misc=lambda: [])
        self.assertEqual(hy.state(), ['pool testpool',
                                      'suspended testpool'])
        self.assertNotIn('zfs list -H -t all -o name -r testpool', self.ran)

    def test_linux_state(self):
        self.pool('rpool', '/dev/sda1')
        self.pool('testpool', '/dev/sdb1')
        self.commands['zfs list -H -t all -o name -r testpool'] = (
            'testpool\ntestpool/fs\ntestpool/fs@snap\ntestpool/fs#mark\n')
        self.commands['zinject'] = (
            ' ID  POOL      OBJSET\n---  ----\n  1  testpool  0\n')
        self.commands['hostid'] = '00bab10c\n'
        self.commands['losetup -nO NAME,BACK-FILE -l'] = (
            '/dev/loop0  /f\n/dev/loop10 /a b\n/dev/loop2\n')
        self.commands['dmsetup ls'] = 'No devices found\n'
        self.files.update({
            '/proc/self/mounts': ('/dev/sda1 / ext4 rw 0 0\n'
                                  'testpool /testpool zfs rw 0 0\n'
                                  'tmpfs %s/t tmpfs rw 0 0\n'
                                  'tmpfs /run tmpfs rw 0 0\n' % self.base),
            '/proc/swaps': 'Filename Type Size\n/dev/sda2 partition 1\n',
            '/proc/spl/kstat/zfs/arcstats': ('c 4 100\nc_min 4 10\n'
                                             'c_max 4 1000\n'),
            '/etc/passwd': 'root:x:0:0:root:/root:/bin/sh\n# comment\n',
            '/etc/group': 'wheel:x:0:\n',
            '/etc/exports.d/zfs.exports': '# comment\n/testpool/fs *(rw)\n',
            '/etc/zfs/exports': ''})
        for name in ('err', 'out', 'file-vdev0', 'zts-report', 'testfile'):
            open(os.path.join(self.base, name), 'w').close()
        self.assertEqual(hy.state(), [
            'arc c_max 1000', 'arc c_min 10', 'dataset testpool/fs',
            'dataset testpool/fs#mark', 'dataset testpool/fs@snap',
            'file testfile', 'group wheel', 'hostid 00bab10c',
            'loop /dev/loop0 /f', 'loop /dev/loop10 /a b',
            'loop /dev/loop2', 'mount / ext4 /dev/sda1',
            'mount %s/t tmpfs tmpfs' % self.base, 'pool rpool',
            'pool testpool', 'share /testpool/fs', 'swap /dev/sda2',
            'user # comment ', 'user root /root',
            'zinject 1 testpool'])

    def test_illumos_and_freebsd_devices(self):
        self.patch(UNAME='SunOS')
        self.commands['lofiadm'] = (
            'Block Device   File      Options\n/dev/lofi/1  /f  -\n')
        self.commands['swap -l'] = (
            'swapfile dev swaplo\n/dev/zvol/dsk/rpool/swap 1,2 8\n')
        self.commands['dumpadm'] = (
            '      Dump content: kernel pages\n'
            '       Dump device: /dev/zvol/dsk/rpool/dump (dedicated)\n')
        self.commands['kstat -p zfs:0:arcstats:c_max zfs:0:arcstats:c_min'] = (
            'zfs:0:arcstats:c_max\t100\nzfs:0:arcstats:c_min\t10\n')
        self.assertEqual(sorted(hy.state_devices()), [
            'dump /dev/zvol/dsk/rpool/dump', 'lofi /dev/lofi/1 /f',
            'swap /dev/zvol/dsk/rpool/swap'])
        self.assertEqual(hy.state_arc(), ['arc c_max 100', 'arc c_min 10'])
        self.patch(UNAME='FreeBSD')
        self.commands['mdconfig -lv'] = 'md0\tvnode\t 1024M\t/f\n'
        self.commands['swapctl -l'] = 'Device: Used:\n/dev/da0p3 1\n'
        self.commands['sysctl kstat.zfs.misc.arcstats.c_max '
                      'kstat.zfs.misc.arcstats.c_min'] = (
            'kstat.zfs.misc.arcstats.c_max: 100\n'
            'kstat.zfs.misc.arcstats.c_min: 10\n')
        self.assertEqual(hy.state_devices(), ['swap /dev/da0p3', 'md md0 /f'])
        self.assertEqual(hy.state_arc(), ['arc c_max 100', 'arc c_min 10'])


class CheckTest(HygieneTest):
    def setUp(self):
        super().setUp()
        self.statefile = os.path.join(self.tmp, '.leakcheck-test')
        self.groupfile = os.path.join(self.tmp, '.leakcheck-group')
        self.reported = os.path.join(self.tmp, '.leakcheck-reported')
        self.now = ['hostid 1', 'pool rpool', 'tunable zfs/a 1']
        self.patch(state=lambda: sorted(self.now, key=os.fsencode))

    def check(self, *args, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            hy.check(self.statefile, 'tests/a/b', *args, **kwargs)
        return hy.lines(output.getvalue())

    def record(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            hy.record(self.statefile)
        self.assertEqual(output.getvalue(), '')

    def test_record_saves_the_state(self):
        hy.write(self.statefile, ['pool old'])
        self.record()
        self.assertEqual(hy.lines(hy.read(self.statefile)), self.now)
        self.assertEqual(hy.lines(hy.read(self.statefile + '.base')), self.now)
        self.assertEqual(self.check(), [])

    def test_failed_record_leaves_no_old_state(self):
        hy.write(self.statefile, ['pool old'])
        hy.write(self.statefile + '.base', ['pool old'])

        def fail():
            raise OSError('state failed')
        self.patch(state=fail)
        self.assertRaises(OSError, self.record)
        self.assertFalse(os.path.lexists(self.statefile))
        self.assertFalse(os.path.lexists(self.statefile + '.base'))

    def test_check_without_a_recorded_state_says_so(self):
        self.assertEqual(self.check(), [
            'tests/a/b: no state was recorded before it; recording it now'])
        self.assertEqual(hy.lines(hy.read(self.statefile)), self.now)
        self.assertEqual(hy.lines(hy.read(self.statefile + '.base')), self.now)
        self.assertEqual(self.check(), [])

    def test_reports_undoes_and_saves(self):
        self.record()
        self.pool('testpool', '/dev/sdb1')
        self.commands['zinject -c 3'] = ''
        self.commands['groupdel zfsgrp'] = (1, '', 'groupdel:  busy\n')
        os.mkdir(self.base + '/testdir5')
        self.now += ['pool testpool', 'dataset testpool/fs', 'file testdir5',
                     'zinject 3 testpool', 'group zfsgrp', 'share /x',
                     'loop /dev/loop1 /home/x  ']
        self.assertEqual(self.check(), [
            'tests/a/b: leaked zinject 3 testpool (undone)',
            'tests/a/b: leaked pool testpool (undone)',
            'tests/a/b: leaked loop /dev/loop1 /home/x',
            'tests/a/b: leaked group zfsgrp (undo failed: groupdel: busy)',
            'tests/a/b: leaked share /x'])
        self.assertLess(self.ran.index('zinject -c 3'),
                        self.ran.index('zpool destroy -f testpool'))
        self.assertFalse(os.path.exists(self.base + '/testdir5'))
        self.assertEqual(hy.lines(hy.read(self.reported)), [
            'leaked loop /dev/loop1 /home/x', 'leaked group zfsgrp',
            'leaked share /x'])
        self.assertEqual(hy.lines(hy.read(self.statefile)),
                         sorted(self.now, key=os.fsencode))
        self.assertEqual(self.check(), [])

    def test_group_check_does_not_repeat_the_tests_report(self):
        self.statefile, test = self.groupfile, self.statefile
        self.record()
        self.statefile, group = test, self.groupfile
        self.record()
        self.now += ['share /x']
        self.now.remove('pool rpool')
        self.assertEqual(self.check('123', group), [
            'tests/a/b: removed pool rpool', 'tests/a/b: leaked share /x'])
        self.statefile = group
        self.assertEqual(self.check(), [])

    def test_reported_leak_that_is_gone_is_reported_again(self):
        self.record()
        self.now += ['share /x', 'share /y']
        self.now[self.now.index('tunable zfs/a 1')] = 'tunable zfs/a 2'
        self.patch(set_tunable=mock.Mock(side_effect=hy.Failed('no')))
        self.assertEqual(self.check(), [
            'tests/a/b: changed tunable zfs/a 1 -> 2 (undo failed: no)',
            'tests/a/b: leaked share /x', 'tests/a/b: leaked share /y'])
        # A cleanup removes /x and puts the tunable back.
        self.now.remove('share /x')
        self.now[self.now.index('tunable zfs/a 2')] = 'tunable zfs/a 1'
        self.check()
        reported = hy.lines(hy.read(self.reported))
        self.assertIn('leaked share /y', reported)
        self.assertNotIn('leaked share /x', reported)
        self.assertNotIn('changed tunable zfs/a 1 -> 2', reported)
        # A later test leaks them again.
        self.now += ['share /x']
        self.now[self.now.index('tunable zfs/a 1')] = 'tunable zfs/a 2'
        self.assertEqual(self.check(), [
            'tests/a/b: changed tunable zfs/a 1 -> 2 (undo failed: no)',
            'tests/a/b: leaked share /x'])

    def test_removed_by_a_test_only_if_in_the_group_base(self):
        self.statefile = self.groupfile
        self.record()
        self.statefile = os.path.join(self.tmp, '.leakcheck-test')
        self.now += ['file made-by-earlier-test']
        self.record()
        self.now = ['pool rpool', 'tunable zfs/a 1', 'hostid 2']
        self.assertEqual(self.check('123', self.groupfile), [])
        self.now = ['tunable zfs/a 1', 'hostid 2']
        self.assertEqual(self.check('123', self.groupfile),
                         ['tests/a/b: removed pool rpool'])

    def test_report_only_undoes_nothing(self):
        self.record()
        self.pool('testpool', '/dev/sdb1')
        os.mkdir(self.base + '/testdir5')
        os.mkdir(self.base + '/testdir6')
        open(self.base + '/testdir6/f', 'w').close()
        self.now += ['pool testpool', 'file testdir5', 'file testdir6']
        self.now[self.now.index('tunable zfs/a 1')] = 'tunable zfs/a 2'
        self.ran = []
        self.assertEqual(self.check(report_only=True), [
            'tests/a/b: changed tunable zfs/a 1 -> 2',
            'tests/a/b: leaked pool testpool',
            'tests/a/b: leaked file testdir6'])
        self.assertEqual(self.ran, [])
        self.assertTrue(os.path.isdir(self.base + '/testdir5'))
        self.assertEqual(hy.lines(hy.read(self.statefile)),
                         sorted(self.now, key=os.fsencode))

    def test_leaked_processes(self):
        self.record()
        hy.write(self.statefile, self.now + ['process zed'])
        self.now += ['process zed']
        self.commands.update({
            'pgrep -s 77': '10\n11\n12\n', 'ps -o comm= -p 10': 'zed\n',
            'ps -o comm= -p 11': 'sleep\n', 'ps -o comm= -p 12': 'dd\n',
            'ps -o args= -p 11': 'sleep 1000\n',
            'ps -o args= -p 12': 'dd ' + 'x' * 200 + '\n',
            'kill -0 12': '', 'ps -o s= -p 12': 'D\n'})
        self.patch(time=mock.Mock())
        self.assertEqual(self.check('77', self.groupfile), [
            'tests/a/b: leaked process 11 sleep 1000 (undone)',
            'tests/a/b: leaked process 12 dd %s (undo failed: still running, '
            'state D)' % ('x' * 97)])
        self.assertIn('kill -9 11', self.ran)
        self.assertNotIn('kill -9 10', self.ran)
        self.ran = []
        self.assertEqual(self.check('77', self.groupfile, report_only=True), [
            'tests/a/b: leaked process 11 sleep 1000',
            'tests/a/b: leaked process 12 dd ' + 'x' * 97])
        self.assertNotIn('kill -9 11', self.ran)

    def test_suspended_pool_keeps_its_datasets(self):
        self.now += ['pool testpool', 'dataset testpool/fs']
        self.record()
        # A test suspends the pool: its datasets can't be listed.
        self.now.remove('dataset testpool/fs')
        self.now.append('suspended testpool')
        self.assertEqual(self.check(),
                         ['tests/a/b: leaked suspended testpool'])
        self.assertIn('dataset testpool/fs', hy.lines(hy.read(self.statefile)))
        # The next one resumes it and leaks a dataset.
        self.now.remove('suspended testpool')
        self.now += ['dataset testpool/fs', 'dataset testpool/leak']
        self.commands['zfs list -t all testpool/leak'] = ''
        self.commands['zfs destroy -Rf testpool/leak'] = ''
        self.assertEqual(self.check(), [
            'tests/a/b: leaked dataset testpool/leak (undone)'])

    def test_process_gone_after_pgrep(self):
        self.record()
        self.commands['pgrep -s 77'] = '13\n'
        self.assertEqual(self.check('77', self.groupfile), [])
        self.assertNotIn('kill -9 13', self.ran)
        self.assertEqual(self.check('77', self.groupfile, report_only=True),
                         [])

    def test_nested_leaked_mounts_undone_deepest_first(self):
        self.record()
        for mnt in ('mnt', 'mnt/sub'):
            self.now.append('mount %s/%s tmpfs tmpfs' % (self.base, mnt))
            self.commands['umount -f %s/%s' % (self.base, mnt)] = ''
        self.check()
        umounts = [cmd for cmd in self.ran if cmd.startswith('umount')]
        self.assertEqual(umounts, ['umount -f %s/mnt/sub' % self.base,
                                   'umount -f %s/mnt' % self.base])

    def test_zinject_handler_counts_io(self):
        self.patch(state=hy.state, imported_pools=lambda: [],
                   state_tunables=lambda: [], state_arc=lambda: [],
                   other_mounts=lambda: [], state_devices=lambda: [],
                   state_misc=lambda: [])
        self.commands['zinject'] = '  1  rpool  0  0  0  data  0  0\n'
        self.record()
        self.commands['zinject'] = '  1  rpool  0  0  0  data  12  3\n'
        self.assertEqual(self.check(), [])
        self.assertNotIn('zinject -c 1', self.ran)


class PreflightTest(HygieneTest):
    def preflight(self, step, dryrun=False):
        preflight = hy.Preflight(dryrun)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            getattr(preflight, step)()
        return preflight, hy.lines(output.getvalue())

    def test_fix(self):
        self.commands['zinject'] = '  1  testpool  0\n  2  rpool  0\n'
        self.commands['zinject -c 1'] = ''
        preflight, output = self.preflight('clean_zinject')
        self.assertEqual(output, [
            'zfs_hygiene: cleaned: zinject handler 1 on testpool '
            '(zinject -c 1)',
            'zfs_hygiene: note: zinject handler 2 on kept pool rpool'])
        self.assertEqual((preflight.cleaned, preflight.problems), (1, 0))
        self.commands['zinject -c 1'] = (1, '', 'failed')
        _, output = self.preflight('clean_zinject')
        self.assertEqual(output[0], 'zfs_hygiene: PROBLEM: zinject handler 1 '
                         'on testpool (failed: zinject -c 1)')
        self.ran = []
        preflight, output = self.preflight('clean_zinject', dryrun=True)
        self.assertEqual(output[0], 'zfs_hygiene: PROBLEM: zinject handler 1 '
                         'on testpool (would run: zinject -c 1)')
        self.assertEqual((preflight.cleaned, preflight.problems), (0, 1))
        self.assertEqual(self.ran, ['zinject'])

    def test_foreign_pools(self):
        self.pool('rpool', '/dev/sda1')
        self.pool('testpool', '/dev/sdb1')
        self.pool('tank', '/dev/sdc1')
        self.pool('testpool9', '/dev/sdc2')
        self.pool('mixed', '/dev/sdb2', '/dev/sdd')
        _, output = self.preflight('check_foreign_pools')
        self.assertEqual(output, [
            'zfs_hygiene: PROBLEM: pool testpool9 uses devices outside DISKS '
            'and the test directory: /dev/sdc2',
            'zfs_hygiene: PROBLEM: pool mixed uses devices outside DISKS '
            'and the test directory: /dev/sdb2 /dev/sdd'])
        self.patch(KEEP_POOLS={'rpool', 'mixed'}, UNAME='SunOS')
        _, output = self.preflight('check_foreign_pools')
        self.assertEqual([output[0], output[2]], [
            'zfs_hygiene: PROBLEM: pool tank is not in KEEP; the suite would '
            'destroy it',
            'zfs_hygiene: PROBLEM: kept pool mixed uses DISKS'])

    def test_leftover_pools(self):
        self.pool('rpool', '/dev/sda1')
        self.pool('testpool', '/dev/sdb1')
        self.pool('stuck', self.base + '/file-vdev0')
        self.commands['zpool status stuck'] = 'pool is suspended\n'
        _, output = self.preflight('clean_pools')
        self.assertEqual(output, [
            'zfs_hygiene: cleaned: destroy leftover pool testpool '
            '(destroy_pool testpool)',
            'zfs_hygiene: PROBLEM: leftover pool stuck is SUSPENDED; clear '
            'it or reboot'])
        self.assertEqual(self.ran.count('zpool destroy -f testpool'), 1)
        self.assertNotIn('zpool destroy -f rpool', self.ran)

    def test_leftover_pool_destroyed_after_the_pool_on_it(self):
        # testpool1 is on a zvol of testpool, which is busy until testpool1
        # is gone.
        self.pool('testpool', '/dev/sdb1')
        self.pool('testpool1', '/dev/zvol/testpool/vol')
        busy = (1, '', 'cannot destroy testpool: pool is busy\n')
        self.commands['zpool destroy -f testpool'] = busy

        real_run = self.run_command

        def run(*argv, **kwargs):
            result = real_run(*argv, **kwargs)
            if argv == ('zpool', 'destroy', '-f', 'testpool1'):
                self.commands['zpool destroy -f testpool'] = ''
            return result
        with mock.patch.object(hy, 'run', run):
            preflight, output = self.preflight('clean_pools')
        self.assertEqual(self.ran.count('zpool destroy -f testpool'), 2)
        self.assertEqual((preflight.cleaned, preflight.problems), (2, 0))
        self.commands['zpool destroy -f testpool'] = busy
        self.commands['zpool destroy -f testpool1'] = busy
        preflight, output = self.preflight('clean_pools')
        self.assertEqual(preflight.problems, 2)
        self.assertEqual(len(output), 2)

    def test_freebsd_disk_paths(self):
        self.patch(UNAME='FreeBSD')
        self.patch_os('exists', lambda path: True)
        with mock.patch.object(hy.glob, 'glob', return_value=[
                '/dev/da1', '/dev/da1p1', '/dev/da1s1a', '/dev/da10',
                '/dev/da10p1']):
            self.patch(disk_of=REAL['disk_of'])
            self.assertEqual(hy.Preflight.disk_paths('da1'),
                             ['/dev/da1', '/dev/da1p1', '/dev/da1s1a'])

    def test_users_and_groups(self):
        self.files['/etc/passwd'] = (
            'root:x:0:0::/root:/bin/sh\n'
            'staff1:x:101:101::%s/staff1:/bin/sh\n'
            'tester:x:102:102::/var/tmp/tester:/bin/sh\n'
            'alice:x:103:103::/home/alice:/bin/sh\n' % self.base)
        self.files['/etc/group'] = 'wheel:x:0:\nzfsgrp:x:200:\n'
        self.commands['userdel staff1'] = ''
        self.commands['groupdel zfsgrp'] = ''
        _, output = self.preflight('clean_users')
        self.assertEqual(output, [
            'zfs_hygiene: cleaned: test user staff1 (del_account user staff1)',
            'zfs_hygiene: cleaned: test group zfsgrp '
            '(del_account group zfsgrp)'])

    def test_saved_tunables_and_files(self):
        os.makedirs(self.tmp + '/sys/zfs')
        os.makedirs(self.tmp + '/hygiene')
        hy.write(self.tmp + '/sys/zfs/zfs_arc_max', ['5'])
        saved = self.base + '/tunable-ARC_MAX'
        hy.write(saved, ['0'])
        hy.write(hy.TUNABLE_LIST, ['zfs_a 4'])
        self.files[hy.STF_SUITE + '/include/tunables.cfg'] = (
            '# NAME FreeBSD Linux\nARC_MAX\tarc.max\tzfs_arc_max\n')
        self.commands['rm -f ' + saved] = ''
        _, output = self.preflight('clean_tunables')
        self.assertEqual(output, [
            'zfs_hygiene: cleaned: tunable zfs/zfs_arc_max is 5, saved 0 '
            '(set_tunable zfs/zfs_arc_max 0)',
            'zfs_hygiene: cleaned: saved tunable file %s (rm -f %s)' % (
                saved, saved)])
        self.assertEqual(hy.read(self.tmp + '/sys/zfs/zfs_arc_max'), '0\n')
        self.assertFalse(os.path.exists(hy.TUNABLE_LIST))

        os.mkdir(self.base + '/testdir1')
        os.mkdir(self.base + '/testdir2')
        for name in ('file-vdev1', 'file-vdev10', 'file-vdev2', 'keep'):
            open(os.path.join(self.base, name), 'w').close()
        self.files['/proc/self/mounts'] = (
            'tank/fs %s/testdir2/m zfs rw 0 0\n' % self.base)
        self.commands['zpool list -vHP'] = (
            'p\t1G\n\t%s/file-vdev10\t1G\n' % self.base)
        self.patch(must=lambda *argv, **kwargs: None)
        _, output = self.preflight('clean_files')
        self.assertEqual([line.split(self.base)[0] + line.split('/')[-1]
                          for line in output], [
            'zfs_hygiene: cleaned: leftover testdir1)',
            'zfs_hygiene: PROBLEM: leftover testdir2 has a file system '
            'mounted in it',
            'zfs_hygiene: cleaned: leftover vdev file file-vdev1)',
            'zfs_hygiene: cleaned: leftover vdev file file-vdev2)'])

    def test_test_zed_left_running(self):
        pattern = 'zed .*-d %s/zed' % self.base
        self.commands['pgrep -f ' + pattern] = '1432\n'
        self.commands['pkill -f ' + pattern] = ''
        _, output = self.preflight('clean_files')
        self.assertIn('zfs_hygiene: cleaned: test zed (pkill -f %s)' % pattern,
                      output)
        # A stale pid file is not used.
        os.makedirs(self.base + '/zed')
        hy.write(self.base + '/zed/zed.pid', ['1'])
        del self.commands['pgrep -f ' + pattern]
        self.ran = []
        self.preflight('clean_files')
        self.assertFalse([cmd for cmd in self.ran if 'kill' in cmd])

    def test_saved_tunables_on_freebsd_and_illumos(self):
        self.patch(UNAME='FreeBSD')
        cfg = hy.STF_SUITE + '/include/tunables.cfg'
        self.files[cfg] = ('ARC_MAX\tarc.max\tzfs_arc_max\n'
                           'ONLY_LINUX\tUNSUPPORTED\tzfs_x\n')
        saved = self.base + '/tunable-ARC_MAX'
        other = self.base + '/tunable-ONLY_LINUX'
        hy.write(saved, ['0'])
        hy.write(other, ['1'])
        self.commands['sysctl -n vfs.zfs.arc.max'] = '5\n'
        self.commands['sysctl vfs.zfs.arc.max=0'] = ''
        self.commands['rm -f ' + saved] = ''
        preflight, output = self.preflight('clean_tunables')
        self.assertEqual(output, [
            'zfs_hygiene: cleaned: tunable vfs.zfs.arc.max is 5, saved 0 '
            '(set_tunable vfs.zfs.arc.max 0)',
            'zfs_hygiene: cleaned: saved tunable file %s (rm -f %s)' % (
                saved, saved),
            'zfs_hygiene: PROBLEM: %s saves a tunable that this script '
            'cannot restore; restore it and remove the file' % other])
        # A failed restore keeps the file.
        self.commands['sysctl vfs.zfs.arc.max=0'] = (1, '', 'denied')
        self.ran = []
        preflight, output = self.preflight('clean_tunables')
        self.assertNotIn('rm -f ' + saved, self.ran)
        self.assertEqual(preflight.problems, 2)
        # Nothing to restore it with on illumos.
        self.patch(UNAME='SunOS')
        self.ran = []
        preflight, _ = self.preflight('clean_tunables')
        self.assertEqual(preflight.problems, 2)
        self.assertNotIn('rm -f ' + saved, self.ran)

    def test_nested_mounts_deepest_first(self):
        self.files['/proc/self/mounts'] = (
            '/dev/sdc1 %s/mnt ext4 rw 0 0\ntmpfs %s/mnt/sub tmpfs rw 0 0\n'
            % (self.base, self.base))
        self.commands['umount -f %s/mnt' % self.base] = ''
        self.commands['umount -f %s/mnt/sub' % self.base] = ''
        self.preflight('clean_mounts')
        self.assertEqual(self.ran, ['umount -f %s/mnt/sub' % self.base,
                                    'umount -f %s/mnt' % self.base])

    def test_partition_in_disks(self):
        self.patch(DISKS='sdb sdc1 loop0 %s/file-vdev0' % self.base)
        open(self.base + '/file-vdev0', 'w').close()
        self.commands['lsblk -dno TYPE /dev/sdb'] = 'disk\n'
        self.commands['lsblk -dno TYPE /dev/sdc1'] = 'part\n'
        self.commands['lsblk -dno TYPE /dev/loop0'] = 'loop\n'
        self.commands['df -k ' + self.base] = ''
        _, output = self.preflight('check_env')
        self.assertEqual(output, [
            'zfs_hygiene: PROBLEM: DISKS includes sdc1, which is not a whole '
            'disk; the suite would take all of sdc'])
        self.patch(UNAME='FreeBSD', DISKS='da1 da2p1',
                   disk_of=REAL['disk_of'])
        _, output = self.preflight('check_env')
        self.assertEqual(output, [
            'zfs_hygiene: PROBLEM: DISKS includes da2p1, which is not a whole '
            'disk; the suite would take all of da2'])

    def test_env(self):
        self.patch(DISKS='sdb sdc1 sdc2', SUDO_USER='zfstest')
        hy.test_disks.cache_clear()
        self.commands['df -k ' + self.base] = (
            'Filesystem 1K-blocks Used Available Use% Mounted on\n'
            '/dev/sda1 100 50 1048576 50% /\n')
        self.files['/proc/self/mounts'] = (
            '/dev/sdb1 /data ext4 rw 0 0\n/dev/sdc1 %s/m ext4 rw 0 0\n'
            % self.base)
        self.files['/proc/swaps'] = (
            'Filename Type\n/dev/sdc2 partition\n/dev/zd0 partition\n')
        _, output = self.preflight('check_env')
        self.assertEqual([line[len('zfs_hygiene: PROBLEM: '):]
                          for line in output], [
            'DISKS lists sdc more than once',
            'the tests create and delete user zfstest; run the suite as '
            'another user',
            'only 1024 MB free in ' + self.base,
            'DISKS includes sdb, which has /data mounted; check DISKS',
            'DISKS includes sdc, which holds swap or dump; check DISKS'])


class MainTest(HygieneTest):
    def main(self, *argv):
        stdout = io.TextIOWrapper(io.BytesIO())
        with mock.patch.object(hy.sys, 'stdout', stdout), \
                mock.patch.object(hy.sys, 'stderr', io.StringIO()), \
                mock.patch.dict(os.environ), \
                mock.patch.object(os, 'geteuid', return_value=self.uid):
            return hy.main(list(argv))

    def setUp(self):
        super().setUp()
        self.uid = 0
        self.patch(check=mock.Mock(), record=mock.Mock(),
                   Preflight=mock.Mock(), state=lambda: ['hostid 1'])
        hy.Preflight.return_value.run.return_value = 1

    def test_commands(self):
        self.assertEqual(self.main('check', 'file', 'tests/a'), 0)
        hy.check.assert_called_with('file', 'tests/a', '', '',
                                    report_only=False)
        self.assertEqual(self.main('check', '-n', 'f', 'a', '7', 'g'), 0)
        hy.check.assert_called_with('f', 'a', '7', 'g', report_only=True)
        self.assertEqual(self.main('check', 'f', 'a', '7', '-n'), 0)
        hy.check.assert_called_with('f', 'a', '7', '', report_only=True)
        self.assertEqual(self.main('preflight'), 1)
        hy.Preflight.assert_called_with(dryrun=False)
        self.assertEqual(self.main('preflight', '-n'), 1)
        hy.Preflight.assert_called_with(dryrun=True)
        self.assertEqual(self.main('record', 'file'), 0)
        hy.record.assert_called_with('file')
        self.assertEqual(self.main('state'), 0)

    def test_usage_and_root(self):
        for argv in ((), ('bogus',), ('check', 'file'), ('check', '-n', 'f'),
                     ('check', 'f', 'a', '7', 'g', 'extra'),
                     ('state', 'extra'), ('preflight', '-x'), ('record',),
                     ('record', 'f', 'extra'), ('record', '-n', 'f')):
            self.assertEqual(self.main(*argv), 2)
        hy.check.assert_not_called()
        hy.record.assert_not_called()
        hy.Preflight.assert_not_called()
        self.uid = 501
        self.assertEqual(self.main('state'), 2)
        # Help needs no root.
        self.assertEqual(self.main('-h'), 0)
        self.assertEqual(self.main('check', '-h'), 0)


if __name__ == '__main__':
    unittest.main()
