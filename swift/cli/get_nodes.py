#!/usr/bin/env python
# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
from optparse import OptionParser
from os.path import basename

from swift.common.ring import Ring
from swift.common.storage_policy import reload_storage_policies
from swift.common.utils import set_swift_dir
from swift.cli.info import (parse_get_node_args, print_item_locations,
                            InfoSystemExit)


def main():

    usage = '''
    Shows the nodes responsible for the item specified.
    Usage: %prog [-a] <ring.gz> <account> [<container> [<object>]]
    Or:    %prog [-a] <ring.gz> -p partition
    Or:    %prog [-a] -P policy_name <account> [<container> [<object>]]
    Or:    %prog [-a] -P policy_name -p partition
    Note: account, container, object can also be a single arg separated by /
    Example:
        $ %prog -a /etc/swift/account.ring.gz MyAccount
        Partition 5743883
        Hash 96ae332a60b58910784e4417a03e1ad0
        10.1.1.7:8000 sdd1
        10.1.9.2:8000 sdb1
        10.1.5.5:8000 sdf1
        10.1.5.9:8000 sdt1 # [Handoff]
    '''
    parser = OptionParser(usage)
    parser.add_option('-a', '--all', action='store_true',
                      help='Show all handoff nodes')
    parser.add_option('-p', '--partition', metavar='PARTITION',
                      help='Show nodes for a given partition')
    parser.add_option('-P', '--policy-name', dest='policy_name',
                      help='Specify which policy to use')
    parser.add_option('-d', '--swift-dir', default='/etc/swift',
                      dest='swift_dir', help='Path to swift directory')
    parser.add_option('-Q', '--quoted', action='store_true',
                      help='Assume swift paths are quoted')
    options, args = parser.parse_args()

    if set_swift_dir(options.swift_dir):
        reload_storage_policies()

    try:
        ring_path, args = parse_get_node_args(options, args)
    except InfoSystemExit as e:
        parser.print_help()
        sys.exit('ERROR: %s' % e)

    ring = ring_name = None
    if ring_path:
        ring_name = basename(ring_path)[:-len('.ring.gz')]
        ring = Ring(ring_path)

    try:
        print_item_locations(ring, ring_name, *args, **vars(options))
    except InfoSystemExit:
        sys.exit(1)


if __name__ == '__main__':
    main()
