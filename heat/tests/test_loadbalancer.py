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


import re
import sys
import os

import nose
import unittest
import mox
import json

from nose.plugins.attrib import attr

from heat.common import exception
from heat.engine import instance
from heat.engine import loadbalancer as lb
from heat.engine import parser
from heat.engine import resources
from heat.engine import stack
from heat.tests.v1_1 import fakes


@attr(tag=['unit', 'resource'])
@attr(speed='fast')
class LoadBalancerTest(unittest.TestCase):
    def setUp(self):
        self.m = mox.Mox()
        self.fc = fakes.FakeClient()
        self.m.StubOutWithMock(parser.Stack, 'store')
        self.m.StubOutWithMock(lb.LoadBalancer, 'nova')
        self.m.StubOutWithMock(instance.Instance, 'nova')
        self.m.StubOutWithMock(self.fc.servers, 'create')
        self.m.StubOutWithMock(resources.Metadata, '__set__')

    def tearDown(self):
        self.m.UnsetStubs()
        print "LoadBalancerTest teardown complete"

    def load_template(self):
        self.path = os.path.dirname(os.path.realpath(__file__)).\
            replace('heat/tests', 'templates')
        f = open("%s/WordPress_With_LB.template" % self.path)
        t = json.loads(f.read())
        f.close()
        return t

    def parse_stack(self, t):
        class DummyContext():
            tenant = 'test_tenant'
            tenant_id = '1234abcd'
            username = 'test_username'
            password = 'password'
            auth_url = 'http://localhost:5000/v2.0'
        t['Parameters']['KeyName']['Value'] = 'test'
        t['Parameters']['KeyName']['Value'] = 'test'

        stack = parser.Stack(DummyContext(), 'test_stack', parser.Template(t),
                             stack_id=-1)

        return stack

    def create_loadbalancer(self, t, stack, resource_name):
        resource = lb.LoadBalancer(resource_name,
                                      t['Resources'][resource_name],
                                      stack)
        self.assertEqual(None, resource.validate())
        self.assertEqual(None, resource.create())
        self.assertEqual(lb.LoadBalancer.CREATE_COMPLETE, resource.state)
        return resource

    def test_loadbalancer(self):
        lb.LoadBalancer.nova().AndReturn(self.fc)
        parser.Stack.store(mox.IgnoreArg()).AndReturn('5678')
        instance.Instance.nova().MultipleTimes().AndReturn(self.fc)
        self.fc.servers.create(flavor=2, image=745, key_name='test',
                   meta=None, name=u'test_stack.LoadBalancer.LB_instance',
                   scheduler_hints=None, userdata=mox.IgnoreArg(),
                   security_groups=None).AndReturn(self.fc.servers.list()[1])
        #stack.Stack.create_with_template(mox.IgnoreArg()).AndReturn(None)
        resources.Metadata.__set__(mox.IgnoreArg(),
                                   mox.IgnoreArg()).AndReturn(None)

        lb.LoadBalancer.nova().MultipleTimes().AndReturn(self.fc)
        self.m.ReplayAll()

        t = self.load_template()
        s = self.parse_stack(t)
        resource = self.create_loadbalancer(t, s, 'LoadBalancer')

        hc = {
            'Target': 'HTTP:80/',
            'HealthyThreshold': '3',
            'UnhealthyThreshold': '5',
            'Interval': '30',
            'Timeout': '5'}
        resource.properties['HealthCheck'] = hc
        self.assertEqual(None, resource.validate())

        hc['Timeout'] = 35
        self.assertEqual({'Error':
                          'Interval must be larger than Timeout'},
                          resource.validate())
        hc['Timeout'] = 5

        self.assertEqual('LoadBalancer', resource.FnGetRefId())

        templ = json.loads(lb.lb_template)
        ha_cfg = resource._haproxy_config(templ)
        self.assertRegexpMatches(ha_cfg, 'bind \*:80')
        self.assertRegexpMatches(ha_cfg, 'server server1 1\.2\.3\.4:80 '
                                 'check inter 30s fall 5 rise 3')
        self.assertRegexpMatches(ha_cfg, 'timeout check 5s')

        id_list = []
        for inst_name in ['WikiServerOne1', 'WikiServerOne2']:
            inst = instance.Instance(inst_name,
                                     s.t['Resources']['WikiServerOne'],
                                     s)
            id_list.append(inst.FnGetRefId())

        resource.nested().create()

        resource.reload(id_list)

        self.assertEqual('4.5.6.7', resource.FnGetAtt('DNSName'))
        self.assertEqual('', resource.FnGetAtt('SourceSecurityGroupName'))

        try:
            resource.FnGetAtt('Foo')
            raise Exception('Expected InvalidTemplateAttribute')
        except exception.InvalidTemplateAttribute:
            pass

        self.assertEqual(lb.LoadBalancer.UPDATE_REPLACE,
                         resource.handle_update())

        self.m.VerifyAll()

    def assertRegexpMatches(self, text, expected_regexp, msg=None):
        """Fail the test unless the text matches the regular expression."""
        if isinstance(expected_regexp, basestring):
            expected_regexp = re.compile(expected_regexp)
        if not expected_regexp.search(text):
            msg = msg or "Regexp didn't match"
            msg = '%s: %r not found in %r' % (msg,
                                              expected_regexp.pattern, text)
            raise self.failureException(msg)

    # allows testing of the test directly, shown below
    if __name__ == '__main__':
        sys.argv.append(__file__)
        nose.main()
