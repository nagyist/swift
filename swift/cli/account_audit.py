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

import os
import sys
import getopt
from itertools import chain

import json
from eventlet.greenpool import GreenPool
from eventlet.event import Event
from urllib.parse import quote

from swift.common.ring import Ring
from swift.common.utils import split_path
from swift.common.utils.base import md5
from swift.common.bufferedhttp import http_connect


usage = """
Usage!

%(cmd)s [options] [url 1] [url 2] ...
    -c [concurrency]      Set the concurrency, default 50
    -r [ring dir]         Ring locations, default /etc/swift
    -e [filename]         File for writing a list of inconsistent urls
    -d                    Also download files and verify md5

You can also feed a list of urls to the script through stdin.

Examples!

    %(cmd)s AUTH_88ad0b83-b2c5-4fa1-b2d6-60c597202076
    %(cmd)s AUTH_88ad0b83-b2c5-4fa1-b2d6-60c597202076/container/object
    %(cmd)s -e errors.txt AUTH_88ad0b83-b2c5-4fa1-b2d6-60c597202076/container
    %(cmd)s < errors.txt
    %(cmd)s -c 25 -d < errors.txt
""" % {'cmd': sys.argv[0]}


class Auditor(object):
    def __init__(self, swift_dir='/etc/swift', concurrency=50, deep=False,
                 error_file=None):
        self.pool = GreenPool(concurrency)
        self.object_ring = Ring(swift_dir, ring_name='object')
        self.container_ring = Ring(swift_dir, ring_name='container')
        self.account_ring = Ring(swift_dir, ring_name='account')
        self.deep = deep
        self.error_file = error_file
        # zero out stats
        self.accounts_checked = self.account_exceptions = \
            self.account_not_found = self.account_container_mismatch = \
            self.account_object_mismatch = self.objects_checked = \
            self.object_exceptions = self.object_not_found = \
            self.object_checksum_mismatch = self.containers_checked = \
            self.container_exceptions = self.container_count_mismatch = \
            self.container_not_found = self.container_obj_mismatch = 0
        self.list_cache = {}
        self.in_progress = {}

    def audit_object(self, account, container, name):
        path = '/%s/%s/%s' % (account, container, name)
        part, nodes = self.object_ring.get_nodes(
            account, container.encode('utf-8'), name.encode('utf-8'))
        container_listing = self.audit_container(account, container)
        consistent = True
        if name not in container_listing:
            print("  Object %s missing in container listing!" % path)
            consistent = False
            hash = None
        else:
            hash = container_listing[name]['hash']
        etags = []
        for node in nodes:
            try:
                if self.deep:
                    conn = http_connect(node['ip'], node['port'],
                                        node['device'], part, 'GET', path, {})
                    resp = conn.getresponse()
                    calc_hash = md5(usedforsecurity=False)
                    chunk = True
                    while chunk:
                        chunk = resp.read(8192)
                        calc_hash.update(chunk)
                    calc_hash = calc_hash.hexdigest()
                    if resp.status // 100 != 2:
                        self.object_not_found += 1
                        consistent = False
                        print('  Bad status %s GETting object "%s" on %s/%s'
                              % (resp.status, path,
                                 node['ip'], node['device']))
                        continue
                    if resp.getheader('ETag').strip('"') != calc_hash:
                        self.object_checksum_mismatch += 1
                        consistent = False
                        print('  MD5 does not match etag for "%s" on %s/%s'
                              % (path, node['ip'], node['device']))
                else:
                    conn = http_connect(node['ip'], node['port'],
                                        node['device'], part, 'HEAD',
                                        path.encode('utf-8'), {})
                    resp = conn.getresponse()
                    if resp.status // 100 != 2:
                        self.object_not_found += 1
                        consistent = False
                        print('  Bad status %s HEADing object "%s" on %s/%s'
                              % (resp.status, path,
                                 node['ip'], node['device']))
                        continue

                override_etag = resp.getheader(
                    'X-Object-Sysmeta-Container-Update-Override-Etag')
                if override_etag:
                    etags.append((override_etag, node))
                else:
                    etags.append((resp.getheader('ETag'), node))
            except Exception:
                self.object_exceptions += 1
                consistent = False
                print('  Exception fetching object "%s" on %s/%s'
                      % (path, node['ip'], node['device']))
                continue
        if not etags:
            consistent = False
            print("  Failed fo fetch object %s at all!" % path)
        elif hash:
            for etag, node in etags:
                if etag.strip('"') != hash:
                    consistent = False
                    self.object_checksum_mismatch += 1
                    print('  ETag mismatch for "%s" on %s/%s'
                          % (path, node['ip'], node['device']))
        if not consistent and self.error_file:
            with open(self.error_file, 'a') as err_file:
                print(path, file=err_file)
        self.objects_checked += 1

    def audit_container(self, account, name, recurse=False):
        if (account, name) in self.in_progress:
            self.in_progress[(account, name)].wait()
        if (account, name) in self.list_cache:
            return self.list_cache[(account, name)]
        self.in_progress[(account, name)] = Event()
        print('Auditing container "%s"' % name)
        path = '/%s/%s' % (account, name)
        account_listing = self.audit_account(account)
        consistent = True
        if name not in account_listing:
            consistent = False
            print("  Container %s not in account listing!" % path)
        part, nodes = \
            self.container_ring.get_nodes(account, name.encode('utf-8'))
        rec_d = {}
        responses = {}
        for node in nodes:
            marker = ''
            results = True
            while results:
                try:
                    conn = http_connect(node['ip'], node['port'],
                                        node['device'], part, 'GET',
                                        path.encode('utf-8'), {},
                                        'format=json&marker=%s' %
                                        quote(marker.encode('utf-8')))
                    resp = conn.getresponse()
                    if resp.status // 100 != 2:
                        self.container_not_found += 1
                        consistent = False
                        print('  Bad status GETting container "%s" on %s/%s' %
                              (path, node['ip'], node['device']))
                        break
                    if node['id'] not in responses:
                        responses[node['id']] = {
                            h.lower(): v for h, v in resp.getheaders()}
                    results = json.loads(resp.read())
                except Exception:
                    self.container_exceptions += 1
                    consistent = False
                    print('  Exception GETting container "%s" on %s/%s' %
                          (path, node['ip'], node['device']))
                    break
                if results:
                    marker = results[-1]['name']
                    for obj in results:
                        obj_name = obj['name']
                        if obj_name not in rec_d:
                            rec_d[obj_name] = obj
                        if (obj['last_modified'] !=
                                rec_d[obj_name]['last_modified']):
                            self.container_obj_mismatch += 1
                            consistent = False
                            print("  Different versions of %s/%s "
                                  "in container dbs." % (name, obj['name']))
                            if (obj['last_modified'] >
                                    rec_d[obj_name]['last_modified']):
                                rec_d[obj_name] = obj
        obj_counts = [int(header['x-container-object-count'])
                      for header in responses.values()]
        if not obj_counts:
            consistent = False
            print("  Failed to fetch container %s at all!" % path)
        else:
            if len(set(obj_counts)) != 1:
                self.container_count_mismatch += 1
                consistent = False
                print(
                    "  Container databases don't agree on number of objects.")
                print(
                    "  Max: %s, Min: %s" % (max(obj_counts), min(obj_counts)))
        self.containers_checked += 1
        self.list_cache[(account, name)] = rec_d
        self.in_progress[(account, name)].send(True)
        del self.in_progress[(account, name)]
        if recurse:
            for obj in rec_d.keys():
                self.pool.spawn_n(self.audit_object, account, name, obj)
        if not consistent and self.error_file:
            with open(self.error_file, 'a') as error_file:
                print(path, file=error_file)
        return rec_d

    def audit_account(self, account, recurse=False):
        if account in self.in_progress:
            self.in_progress[account].wait()
        if account in self.list_cache:
            return self.list_cache[account]
        self.in_progress[account] = Event()
        print('Auditing account "%s"' % account)
        consistent = True
        path = '/%s' % account
        part, nodes = self.account_ring.get_nodes(account)
        responses = {}
        for node in nodes:
            marker = ''
            results = True
            while results:
                node_id = node['id']
                try:
                    conn = http_connect(node['ip'], node['port'],
                                        node['device'], part, 'GET', path, {},
                                        'format=json&marker=%s' %
                                        quote(marker.encode('utf-8')))
                    resp = conn.getresponse()
                    if resp.status // 100 != 2:
                        self.account_not_found += 1
                        consistent = False
                        print("  Bad status GETting account '%s' "
                              " from %s:%s" %
                              (account, node['ip'], node['device']))
                        break
                    results = json.loads(resp.read())
                except Exception:
                    self.account_exceptions += 1
                    consistent = False
                    print("  Exception GETting account '%s' on %s:%s" %
                          (account, node['ip'], node['device']))
                    break
                if node_id not in responses:
                    responses[node_id] = [
                        {h.lower(): v for h, v in resp.getheaders()}, []]
                responses[node_id][1].extend(results)
                if results:
                    marker = results[-1]['name']
        headers = [r[0] for r in responses.values()]
        cont_counts = [int(header['x-account-container-count'])
                       for header in headers]
        if len(set(cont_counts)) != 1:
            self.account_container_mismatch += 1
            consistent = False
            print("  Account databases for '%s' don't agree on"
                  " number of containers." % account)
            if cont_counts:
                print("  Max: %s, Min: %s" % (max(cont_counts),
                                              min(cont_counts)))
        obj_counts = [int(header['x-account-object-count'])
                      for header in headers]
        if len(set(obj_counts)) != 1:
            self.account_object_mismatch += 1
            consistent = False
            print("  Account databases for '%s' don't agree on"
                  " number of objects." % account)
            if obj_counts:
                print("  Max: %s, Min: %s" % (max(obj_counts),
                                              min(obj_counts)))
        containers = set()
        for resp in responses.values():
            containers.update(container['name'] for container in resp[1])
        self.list_cache[account] = containers
        self.in_progress[account].send(True)
        del self.in_progress[account]
        self.accounts_checked += 1
        if recurse:
            for container in containers:
                self.pool.spawn_n(self.audit_container, account,
                                  container, True)
        if not consistent and self.error_file:
            with open(self.error_file, 'a') as error_file:
                print(path, error_file)
        return containers

    def audit(self, account, container=None, obj=None):
        if obj and container:
            self.pool.spawn_n(self.audit_object, account, container, obj)
        elif container:
            self.pool.spawn_n(self.audit_container, account, container, True)
        else:
            self.pool.spawn_n(self.audit_account, account, True)

    def wait(self):
        self.pool.waitall()

    def print_stats(self):

        def _print_stat(name, stat):
            # Right align stat name in a field of 18 characters
            print("{0:>18}: {1}".format(name, stat))

        print()
        _print_stat("Accounts checked", self.accounts_checked)
        if self.account_not_found:
            _print_stat("Missing Replicas", self.account_not_found)
        if self.account_exceptions:
            _print_stat("Exceptions", self.account_exceptions)
        if self.account_container_mismatch:
            _print_stat("Container mismatch", self.account_container_mismatch)
        if self.account_object_mismatch:
            _print_stat("Object mismatch", self.account_object_mismatch)
        print()
        _print_stat("Containers checked", self.containers_checked)
        if self.container_not_found:
            _print_stat("Missing Replicas", self.container_not_found)
        if self.container_exceptions:
            _print_stat("Exceptions", self.container_exceptions)
        if self.container_count_mismatch:
            _print_stat("Count mismatch", self.container_count_mismatch)
        if self.container_obj_mismatch:
            _print_stat("Object mismatch", self.container_obj_mismatch)
        print()
        _print_stat("Objects checked", self.objects_checked)
        if self.object_not_found:
            _print_stat("Missing Replicas", self.object_not_found)
        if self.object_exceptions:
            _print_stat("Exceptions", self.object_exceptions)
        if self.object_checksum_mismatch:
            _print_stat("MD5 Mismatch", self.object_checksum_mismatch)


def main():
    try:
        optlist, args = getopt.getopt(sys.argv[1:], 'c:r:e:d')
    except getopt.GetoptError as err:
        print(str(err))
        print(usage)
        sys.exit(2)
    if not args and os.isatty(sys.stdin.fileno()):
        print(usage)
        sys.exit()
    opts = dict(optlist)
    options = {
        'concurrency': int(opts.get('-c', 50)),
        'error_file': opts.get('-e', None),
        'swift_dir': opts.get('-r', '/etc/swift'),
        'deep': '-d' in opts,
    }
    auditor = Auditor(**options)
    if not os.isatty(sys.stdin.fileno()):
        args = chain(args, sys.stdin)
    for path in args:
        path = '/' + path.rstrip('\r\n').lstrip('/')
        auditor.audit(*split_path(path, 1, 3, True))
    auditor.wait()
    auditor.print_stats()


if __name__ == '__main__':
    main()
