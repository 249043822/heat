# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


import sys
import os

import nose
import unittest
import mox
import json
import sqlalchemy

from nose.plugins.attrib import attr
from nose import with_setup

from heat.tests.v1_1 import fakes
from heat.engine.resources import instance as instances
import heat.db as db_api
from heat.engine import parser


@attr(tag=['unit', 'resource'])
@attr(speed='fast')
class instancesTest(unittest.TestCase):
    def setUp(self):
        self.m = mox.Mox()
        self.fc = fakes.FakeClient()
        self.path = os.path.dirname(os.path.realpath(__file__)).\
            replace('heat/tests', 'templates')

    def tearDown(self):
        self.m.UnsetStubs()
        print "instancesTest teardown complete"

    def test_instance_create(self):
        f = open("%s/WordPress_Single_Instance_gold.template" % self.path)
        t = json.loads(f.read())
        f.close()

        t['Parameters']['KeyName']['Value'] = 'test'
        stack = parser.Stack(None, 'test_stack', parser.Template(t),
                             stack_id=-1)

        self.m.StubOutWithMock(db_api, 'resource_get_by_name_and_stack')
        db_api.resource_get_by_name_and_stack(None, 'test_resource_name',
                                              stack).AndReturn(None)

        self.m.StubOutWithMock(instances.Instance, 'nova')
        instances.Instance.nova().MultipleTimes().AndReturn(self.fc)

        self.m.ReplayAll()

        t['Resources']['WebServer']['Properties']['ImageId'] = 'CentOS 5.2'
        t['Resources']['WebServer']['Properties']['InstanceType'] = \
            '256 MB Server'
        instance = instances.Instance('test_resource_name',
                                      t['Resources']['WebServer'], stack)

        instance.t = instance.stack.resolve_runtime_data(instance.t)

        # need to resolve the template functions
        server_userdata = instance._build_userdata(
                                instance.t['Properties']['UserData'])
        self.m.StubOutWithMock(self.fc.servers, 'create')
        self.fc.servers.create(image=1, flavor=1, key_name='test',
                name='test_stack.test_resource_name', security_groups=None,
                userdata=server_userdata, scheduler_hints=None,
                meta=None).AndReturn(self.fc.servers.list()[1])
        self.m.ReplayAll()

        instance.create()

        # this makes sure the auto increment worked on instance creation
        self.assertTrue(instance.id > 0)

    def test_instance_create_delete(self):
        f = open("%s/WordPress_Single_Instance_gold.template" % self.path)
        t = json.loads(f.read())
        f.close()

        t['Parameters']['KeyName']['Value'] = 'test'
        stack = parser.Stack(None, 'test_stack', parser.Template(t),
                             stack_id=-1)

        self.m.StubOutWithMock(db_api, 'resource_get_by_name_and_stack')
        db_api.resource_get_by_name_and_stack(None, 'test_resource_name',
                                              stack).AndReturn(None)

        self.m.StubOutWithMock(instances.Instance, 'nova')
        instances.Instance.nova().AndReturn(self.fc)
        instances.Instance.nova().AndReturn(self.fc)
        instances.Instance.nova().AndReturn(self.fc)
        instances.Instance.nova().AndReturn(self.fc)

        self.m.ReplayAll()

        t['Resources']['WebServer']['Properties']['ImageId'] = 'CentOS 5.2'
        t['Resources']['WebServer']['Properties']['InstanceType'] = \
            '256 MB Server'
        instance = instances.Instance('test_resource_name',
                                      t['Resources']['WebServer'], stack)

        instance.t = instance.stack.resolve_runtime_data(instance.t)

        # need to resolve the template functions
        server_userdata = instance._build_userdata(
                                instance.t['Properties']['UserData'])
        self.m.StubOutWithMock(self.fc.servers, 'create')
        self.fc.servers.create(image=1, flavor=1, key_name='test',
                name='test_resource_name', security_groups=None,
                userdata=server_userdata, scheduler_hints=None,
                meta=None).AndReturn(self.fc.servers.list()[1])
        self.m.ReplayAll()

        instance.instance_id = 1234
        instance.create()

        # this makes sure the auto increment worked on instance creation
        self.assertTrue(instance.id > 0)

        instance.delete()
        self.assertTrue(instance.instance_id is None)
        self.assertEqual(instance.state, instance.DELETE_COMPLETE)

    # allows testing of the test directly, shown below
    if __name__ == '__main__':
        sys.argv.append(__file__)
        nose.main()
