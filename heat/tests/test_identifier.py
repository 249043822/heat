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


import nose
import unittest
from nose.plugins.attrib import attr
import mox
import json

from heat.engine import identifier


@attr(tag=['unit', 'identifier'])
@attr(speed='fast')
class IdentifierTest(unittest.TestCase):

    def test_attrs(self):
        hi = identifier.HeatIdentifier('t', 's', 'i', 'p')
        self.assertEqual(hi.tenant, 't')
        self.assertEqual(hi.stack_name, 's')
        self.assertEqual(hi.stack_id, 'i')
        self.assertEqual(hi.path, '/p')

    def test_path_default(self):
        hi = identifier.HeatIdentifier('t', 's', 'i')
        self.assertEqual(hi.path, '')

    def test_items(self):
        hi = identifier.HeatIdentifier('t', 's', 'i', 'p')
        self.assertEqual(hi['tenant'], 't')
        self.assertEqual(hi['stack_name'], 's')
        self.assertEqual(hi['stack_id'], 'i')
        self.assertEqual(hi['path'], '/p')

    def test_invalid_attr(self):
        hi = identifier.HeatIdentifier('t', 's', 'i', 'p')
        hi.identity['foo'] = 'bar'
        self.assertRaises(AttributeError, getattr, hi, 'foo')

    def test_invalid_item(self):
        hi = identifier.HeatIdentifier('t', 's', 'i', 'p')
        hi.identity['foo'] = 'bar'
        self.assertRaises(KeyError, lambda o, k: o[k], hi, 'foo')

    def test_arn(self):
        hi = identifier.HeatIdentifier('t', 's', 'i', 'p')
        self.assertEqual(hi.arn(), 'arn:openstack:heat::t:stacks/s/i/p')

    def test_arn_id_int(self):
        hi = identifier.HeatIdentifier('t', 's', 42, 'p')
        self.assertEqual(hi.arn(), 'arn:openstack:heat::t:stacks/s/42/p')

    def test_arn_parse(self):
        arn = 'arn:openstack:heat::t:stacks/s/i/p'
        hi = identifier.HeatIdentifier.from_arn(arn)
        self.assertEqual(hi.tenant, 't')
        self.assertEqual(hi.stack_name, 's')
        self.assertEqual(hi.stack_id, 'i')
        self.assertEqual(hi.path, '/p')

    def test_arn_parse_path_default(self):
        arn = 'arn:openstack:heat::t:stacks/s/i'
        hi = identifier.HeatIdentifier.from_arn(arn)
        self.assertEqual(hi.tenant, 't')
        self.assertEqual(hi.stack_name, 's')
        self.assertEqual(hi.stack_id, 'i')
        self.assertEqual(hi.path, '')

    def test_arn_parse_upper(self):
        arn = 'ARN:openstack:heat::t:stacks/s/i/p'
        hi = identifier.HeatIdentifier.from_arn(arn)
        self.assertEqual(hi.stack_name, 's')

    def test_arn_parse_arn_invalid(self):
        arn = 'urn:openstack:heat::t:stacks/s/i'
        self.assertRaises(ValueError, identifier.HeatIdentifier.from_arn, arn)

    def test_arn_parse_os_invalid(self):
        arn = 'arn:aws:heat::t:stacks/s/i'
        self.assertRaises(ValueError, identifier.HeatIdentifier.from_arn, arn)

    def test_arn_parse_heat_invalid(self):
        arn = 'arn:openstack:cool::t:stacks/s/i'
        self.assertRaises(ValueError, identifier.HeatIdentifier.from_arn, arn)

    def test_arn_parse_stacks_invalid(self):
        arn = 'arn:openstack:heat::t:sticks/s/i'
        self.assertRaises(ValueError, identifier.HeatIdentifier.from_arn, arn)

    def test_arn_parse_missing_field(self):
        arn = 'arn:openstack:heat::t:stacks/s'
        self.assertRaises(ValueError, identifier.HeatIdentifier.from_arn, arn)

    def test_arn_parse_empty_field(self):
        arn = 'arn:openstack:heat::t:stacks//i'
        self.assertRaises(ValueError, identifier.HeatIdentifier.from_arn, arn)

    def test_arn_round_trip(self):
        hii = identifier.HeatIdentifier('t', 's', 'i', 'p')
        hio = identifier.HeatIdentifier.from_arn(hii.arn())
        self.assertEqual(hio.tenant, hii.tenant)
        self.assertEqual(hio.stack_name, hii.stack_name)
        self.assertEqual(hio.stack_id, hii.stack_id)
        self.assertEqual(hio.path, hii.path)

    def test_arn_parse_round_trip(self):
        arn = 'arn:openstack:heat::t:stacks/s/i/p'
        hi = identifier.HeatIdentifier.from_arn(arn)
        self.assertEqual(hi.arn(), arn)

    def test_dict_round_trip(self):
        hii = identifier.HeatIdentifier('t', 's', 'i', 'p')
        hio = identifier.HeatIdentifier(**dict(hii))
        self.assertEqual(hio.tenant, hii.tenant)
        self.assertEqual(hio.stack_name, hii.stack_name)
        self.assertEqual(hio.stack_id, hii.stack_id)
        self.assertEqual(hio.path, hii.path)

    def test_url_path(self):
        hi = identifier.HeatIdentifier('t', 's', 'i', 'p')
        self.assertEqual(hi.url_path(), '/t/stacks/s/i/p')

    def test_url_path_default(self):
        hi = identifier.HeatIdentifier('t', 's', 'i')
        self.assertEqual(hi.url_path(), '/t/stacks/s/i')

    def test_tenant_escape(self):
        hi = identifier.HeatIdentifier(':/', 's', 'i')
        self.assertEqual(hi.tenant, ':/')
        self.assertEqual(hi.url_path(), '/%3A%2F/stacks/s/i')
        self.assertEqual(hi.arn(), 'arn:openstack:heat::%3A%2F:stacks/s/i')

    def test_name_escape(self):
        hi = identifier.HeatIdentifier('t', ':/', 'i')
        self.assertEqual(hi.stack_name, ':/')
        self.assertEqual(hi.url_path(), '/t/stacks/%3A%2F/i')
        self.assertEqual(hi.arn(), 'arn:openstack:heat::t:stacks/%3A%2F/i')

    def test_id_escape(self):
        hi = identifier.HeatIdentifier('t', 's', ':/')
        self.assertEqual(hi.stack_id, ':/')
        self.assertEqual(hi.url_path(), '/t/stacks/s/%3A%2F')
        self.assertEqual(hi.arn(), 'arn:openstack:heat::t:stacks/s/%3A%2F')

    def test_path_escape(self):
        hi = identifier.HeatIdentifier('t', 's', 'i', ':/')
        self.assertEqual(hi.path, '/:/')
        self.assertEqual(hi.url_path(), '/t/stacks/s/i/%3A/')
        self.assertEqual(hi.arn(), 'arn:openstack:heat::t:stacks/s/i/%3A/')

    def test_tenant_decode(self):
        arn = 'arn:openstack:heat::%3A%2F:stacks/s/i'
        hi = identifier.HeatIdentifier.from_arn(arn)
        self.assertEqual(hi.tenant, ':/')

    def test_name_decode(self):
        arn = 'arn:openstack:heat::t:stacks/%3A%2F/i'
        hi = identifier.HeatIdentifier.from_arn(arn)
        self.assertEqual(hi.stack_name, ':/')

    def test_id_decode(self):
        arn = 'arn:openstack:heat::t:stacks/s/%3A%2F'
        hi = identifier.HeatIdentifier.from_arn(arn)
        self.assertEqual(hi.stack_id, ':/')

    def test_path_decode(self):
        arn = 'arn:openstack:heat::t:stacks/s/i/%3A%2F'
        hi = identifier.HeatIdentifier.from_arn(arn)
        self.assertEqual(hi.path, '/:/')

    def test_arn_escape_decode_round_trip(self):
        hii = identifier.HeatIdentifier(':/', ':/', ':/', ':/')
        hio = identifier.HeatIdentifier.from_arn(hii.arn())
        self.assertEqual(hio.tenant, hii.tenant)
        self.assertEqual(hio.stack_name, hii.stack_name)
        self.assertEqual(hio.stack_id, hii.stack_id)
        self.assertEqual(hio.path, hii.path)

    def test_arn_decode_escape_round_trip(self):
        arn = 'arn:openstack:heat::%3A%2F:stacks/%3A%2F/%3A%2F/%3A/'
        hi = identifier.HeatIdentifier.from_arn(arn)
        self.assertEqual(hi.arn(), arn)

    def test_equal(self):
        hi1 = identifier.HeatIdentifier('t', 's', 'i', 'p')
        hi2 = identifier.HeatIdentifier('t', 's', 'i', 'p')
        self.assertTrue(hi1 == hi2)

    def test_equal_dict(self):
        hi = identifier.HeatIdentifier('t', 's', 'i', 'p')
        self.assertTrue(hi == dict(hi))
        self.assertTrue(dict(hi) == hi)

    def test_not_equal(self):
        hi1 = identifier.HeatIdentifier('t', 's', 'i', 'p')
        hi2 = identifier.HeatIdentifier('t', 's', 'i', 'q')
        self.assertFalse(hi1 == hi2)
        self.assertFalse(hi2 == hi1)

    def test_not_equal_dict(self):
        hi1 = identifier.HeatIdentifier('t', 's', 'i', 'p')
        hi2 = identifier.HeatIdentifier('t', 's', 'i', 'q')
        self.assertFalse(hi1 == dict(hi2))
        self.assertFalse(dict(hi1) == hi2)
        self.assertFalse(hi1 == {'tenant': 't',
                                 'stack_name': 's',
                                 'stack_id': 'i'})
        self.assertFalse({'tenant': 't',
                          'stack_name': 's',
                          'stack_id': 'i'} == hi1)


# allows testing of the test directly, shown below
if __name__ == '__main__':
    sys.argv.append(__file__)
    nose.main()
