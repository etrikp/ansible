#!/usr/bin/env python
"""
Vagrant external inventory script. Automatically finds the IP of the booted vagrant vm(s), and
returns it under the host group 'vagrant'

Example Vagrant configuration using this script:

    config.vm.provision :ansible do |ansible|
      ansible.playbook = "./provision/your_playbook.yml"
      ansible.inventory_file = "./provision/inventory/vagrant.py"
      ansible.verbose = true
    end
"""

# Copyright (C) 2013  Mark Mandel <mark@compoundtheory.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

#
# $File: md5sumd.py $
# $LastChangedDate: 2013-01-30 18:29:35 -0600 (Wed, 30 Jan 2013) $
#
# Copyright (C) 2013 Bo Peng (bpeng@mdanderson.org)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#

# Thanks to ActiveState for the PersistentDict class
# http://code.activestate.com/recipes/576642-persistent-dict-with-multiple-standard-file-format/

#
# Thanks to the spacewalk.py inventory script for giving me the basic structure
# of this.
#

import sys
import subprocess
import re
import string
import shutil
import csv
import pickle
import os
import uuid
import time
import hashlib
import operator
import tempfile

from optparse import OptionParser
try:
    import json
except:
    import simplejson as json

now = time.time()


PY3 = sys.version_info.major == 3

# Options
#------------------------------

parser = OptionParser(usage="%prog [options] --list | --host <machine>")
parser.add_option('--list', default=False, dest="list", action="store_true",
                  help="Produce a JSON consumable grouping of Vagrant servers for Ansible")
parser.add_option('--host', default=None, dest="host",
                  help="Generate additional host specific details for given host for Ansible")
(options, args) = parser.parse_args()

#
# helper functions
#

class PersistentDict(dict):
    ''' Persistent dictionary with an API compatible with shelve and anydbm.

    The dict is kept in memory, so the dictionary operations run as fast as
    a regular dictionary.

    Write to disk is delayed until close or sync (similar to gdbm's fast mode).

    Input file format is automatically discovered.
    Output file format is selectable between pickle, json, and csv.
    All three serialization formats are backed by fast C implementations.

    '''

    def __init__(self, filename, flag='c', mode=None, format='pickle', ttl=10, *args, **kwds):
        self.flag = flag                    # r=readonly, c=create, or n=new
        self.mode = mode                    # None or an octal triple like 0644
        self.format = format                # 'csv', 'json', or 'pickle'
        self.filename = filename

        if flag != 'n' and os.access(filename, os.R_OK):
            fileobj = open(filename, 'rb' if format=='pickle' else 'r')
            with fileobj:
                self.load(fileobj)
        dict.__init__(self, *args, **kwds)

    def clear(self):
        print 'Purging cache %s' % self.filename
        os.remove(self.filename)
        self.close()

    def sync(self):
        'Write dict to disk'
        if self.flag == 'r':
            return
        filename = self.filename
        tempname = filename + '.tmp'

        fileobj = open(tempname, 'wb' if self.format=='pickle' else 'w')
        try:
            self.dump(fileobj)
        except Exception:
            os.remove(tempname)
            raise
        finally:
            fileobj.close()
        shutil.move(tempname, self.filename)    # atomic commit
        if self.mode is not None:
            os.chmod(self.filename, self.mode)

    def close(self):
        self.sync()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def dump(self, fileobj):
        if self.format == 'csv':
            csv.writer(fileobj).writerows(self.items())
        elif self.format == 'json':
            json.dump(self, fileobj, separators=(',', ':'))
        elif self.format == 'pickle':
            pickle.dump(dict(self), fileobj, 2)
        else:
            raise NotImplementedError('Unknown format: ' + repr(self.format))

    def load(self, fileobj):
        # try formats from most restrictive to least restrictive
        for loader in (pickle.load, json.load, csv.reader):
            fileobj.seek(0)
            try:
                return self.update(loader(fileobj))
            except Exception:
                pass
        raise ValueError('File not in a supported format')



def probeDir(basedir):
    '''Return the number and total size of files that will be processed by calculateDirMD5'''
    if os.path.isfile(basedir):
        return (1, os.path.getsize(basedir))
    count = 0
    filesize = 0
    for item in os.listdir(basedir):
        fullname = os.path.join(basedir, item)
        if os.path.isfile(fullname):
            count += 1
            filesize += min(2**26, os.path.getsize(fullname))
        elif os.path.isdir(fullname):
            item_count, item_filesize = probeDir(fullname)
            count += item_count
            filesize += item_filesize
    return count, filesize

def calculateFileMD5(filename):
    '''calculate md5 for specified file, using the first 64M of the content'''
    md5 = hashlib.md5()
    # limit the calculation to the first 1G of the file content
    block_size = 2**20  # buffer of 1M
    filesize = os.path.getsize(filename)
    try:
        if filesize < 2**26:
            # for file less than 1G, use all its content
            with open(filename, 'rb') as f:
                while True:
                    data = f.read(block_size)
                    if not data:
                        break
                    md5.update(data)
        else:
            count = 64
            # otherwise, use the first and last 500M
            with open(filename, 'rb') as f:
                while True:
                    data = f.read(block_size)
                    count -= 1
                    if count == 32:
                        f.seek(-2**25, 2)
                    if not data or count == 0:
                        break
                    md5.update(data)
    except IOError as e:
        sys.exit('Failed to read {}: {}'.format(filename, e))
    return md5.hexdigest()

def calculateDirMD5(basedir, call_back):
    '''Calculate MD5 signature of specified directory. call_back is used to report progress.'''
    manifest = []
    for item in os.listdir(basedir):
        fullname = os.path.join(basedir, item)
        # if the item is a file, collect md5 and other information
        # linkes are ignored
        if os.path.isfile(fullname):
            filesize = os.path.getsize(fullname)
            manifest.append((fullname, calculateFileMD5(fullname), '-', 1, 0, filesize, 1, filesize))
            # advance the progress bar
            if call_back is not None:
                call_back(min(2**26, filesize))
        elif os.path.isdir(fullname):
            # for a directory, call calculateDirMD5 recursively
            md5, nfiles, ndirs, sfiles, total_nfiles, total_sfiles, manifests = calculateDirMD5(fullname, call_back)
            # folder md5 is required because name of an empty folder will otherwise not be recorded
            manifest.append((fullname, md5, 'd', nfiles, ndirs, sfiles, total_nfiles, total_sfiles))
            manifest.extend(manifests)
    # sort the entries to avoid folder md5 affected by file/directory order
    # here we sort by md5 key because sorting by filename can be unstable due to different encoding
    # of filenames obtained by python2/python3 from different operating systems
    # The final output is sorted by filename though, because that will make the output easier to read
    manifest.sort(key=operator.itemgetter(1))
    # calculate folder md5 from a temporary manifest file. To keep compatibility
    # the file is written in wb mode to avoid \n to \r\n translation under windows
    tmp = tempfile.NamedTemporaryFile(mode='wb', delete=False)
    for item in manifest:
        if PY3:
            # in pyhon 3, we need to worry about what has been returned for a non-ascii filename from os.listdir
            # however, as long as we use the same encoding (utf-8) the MD5 generated by the command is the same
            # across platform
            tmp.write('{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n'.format(item[1], item[2], item[3], item[4], item[5], item[6], item[7],
                os.path.normpath(os.path.relpath(item[0], basedir)).replace('\\', '/')).encode('utf-8'))
        # each line has md5, type, nfiles, ndirs, sfiles, total_nfiles, total_sfiles, name
        # to make the md5 work for both windows and linux, the path name is saved in / slash
        else:
            # in python 2, we can write a string directly to a 'wb' file, without worry about encoding
            tmp.write('{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n'.format(item[1], item[2], item[3], item[4], item[5], item[6], item[7],
                os.path.normpath(os.path.relpath(item[0], basedir)).replace('\\', '/')))
    tmp.close()
    # this is the md5 for the whole directory
    md5 = calculateFileMD5(tmp.name)
    os.remove(tmp.name)
    #
    nfiles = len([x for x in manifest if x[2] == '-'])
    ndirs = len(manifest) - nfiles
    sfiles = sum([x[5] for x in manifest if x[2] == '-'])
    total_nfiles = sum([x[6] for x in manifest])
    total_sfiles = sum([x[7] for x in manifest])
    return md5, nfiles, ndirs, sfiles, total_nfiles, total_sfiles, manifest


def checkDir(basedir, checksum, call_back):
    '''Validate directories against their md5 signatures.'''
    if os.path.isfile(basedir):
        md5 = calculateFileMD5(basedir)
        if md5 == checksum[basedir][0]:
            return True, ['{}: OK'.format(basedir)]
        else:
            return False, ['{}: FAILED'.format(basedir)]
    md5, nfiles, ndirs, sfiles, total_nfiles, total_sfiles, manifest = calculateDirMD5(basedir, call_back)
    if md5 == checksum[basedir][0]:
        return True, ['{}: OK'.format(basedir)]
    # if something goes wrong, we need to check more
    # find items that starts with basedir
    cs = {x:y for x,y in checksum.iteritems() if x.startswith(basedir) and x != basedir}
    if not cs:
        return False, ['{}: FAILED'.format(basedir)]
    #
    # if there are some information
    messages = []
    for line in manifest:
        # existing file
        if line[0] not in cs:
            if line[2] == 'd':
                messages.append('{}: new directory added.'.format(line[0]))
            else:
                messages.append('{}: new file added.'.format(line[0]))
            continue
        if line[1] != cs[line[0]][0]:
            if line[2] == 'd':
                messages.append('{}: directory modified.'.format(line[0]))
            else:
                messages.append('{}: file modified.'.format(line[0]))
            cs.pop(line[0])
            continue
        cs.pop(line[0])
    # Removed files?
    for item, value in cs.iteritems():
        if value[1] == 'd':
            messages.append('{}: directory removed.'.format(item))
        else:
            messages.append('{}: file removed.'.format(item))
    #
    messages.append('{}: FAILED'.format(basedir))
    return False, messages


# generate a unique to the runtime host id for the cache file name.
def node_id():
    return str(uuid.uuid1(uuid.getnode(), 1)).split('-',2)[2]

# get all the ssh configs for all boxes in an array of dictionaries.
def get_ssh_config():

    if 'configs' in cache:
        configs = cache['configs']
    else:
        configs = []

        boxes = list_running_boxes()

        for box in boxes:
            config = get_a_ssh_config(box)
            config['box_name'] = box
            configs.append(config)
        cache['configs'] = configs
        cache.sync()

    return configs

#list all the running boxes
def list_running_boxes():

    if 'boxes' in cache:
        boxes = cache['boxes']
    else:
        output = subprocess.check_output(["vagrant", "status"]).split('\n')

        boxes = []

        for line in output:
            matcher = re.search("([^\s]+)[\s]+running \(.+", line)
            if matcher:
                boxes.append(matcher.group(1))
        cache['boxes'] = boxes
        cache.sync()

    return boxes

#get the ssh config for a single box
def get_a_ssh_config(box_name):
    """Gives back a map of all the machine's ssh configurations"""

    if box_name in cache:
        config = cache[box_name]
    else:
        output = subprocess.check_output(["vagrant", "ssh-config", box_name]).split('\n')

        config = {}

        for line in output:
            if line.strip() != '':
                matcher = re.search("(  )?([a-zA-Z]+) (.*)", line)
                config[matcher.group(2)] = matcher.group(3)
        cache[box_name] = config
        cache.sync()

    return config


# Since spawning vagrant every time to check the inventory
# is really slow, attempt some intelligent caching in relation
# to vagrant changing the state of the machines it manages.

cache_file = tempfile.gettempdir() + '/ansible-vagrant-inventory-' + node_id()
cache = PersistentDict(cache_file, format='json')

vagrant_cwd = os.getenv('VAGRANT_CWD', '.vagrant')

if os.path.exists(vagrant_cwd):
    current_checksum = calculateDirMD5(vagrant_cwd, lambda x: x)[0]
else:
    current_checksum = None

if 'vagrant_checksum' in cache:
    if cache['vagrant_checksum'] != current_checksum:
        os.remove(cache_file)
        cache = PersistentDict(cache_file, format='json')
        cache['vagrant_checksum'] = current_checksum
        cache.sync()
else:
    cache['vagrant_checksum'] = current_checksum
    cache.sync()

# List out servers that vagrant has running
#------------------------------
if options.list:
    ssh_config = get_ssh_config()
    hosts = { 'vagrant': []}

    for data in ssh_config:
        host_alias = data['box_name']
        hosts['vagrant'].append(host_alias)

    print json.dumps(hosts)
    sys.exit(1)

# Get out the host details
#------------------------------
elif options.host:
    result = {}
    ssh_config = get_ssh_config()

    details = filter(lambda x: (x['box_name'] == options.host), ssh_config)
    if len(details) > 0:
        #pass through the port, in case it's non standard.
        result = details[0]
        result['ansible_ssh_port'] = result['Port']
        result['ansible_ssh_user'] = result['User']
        result['ansible_ssh_private_key_file'] = result['IdentityFile']
        result['ansible_ssh_host'] = result['HostName']

    print json.dumps(result)
    sys.exit(1)


# Print out help
#------------------------------
else:
    parser.print_help()
    sys.exit(1)
