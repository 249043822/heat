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

import json
import uuid

import keystoneclient.exceptions as kc_exception
from keystoneclient.v3 import client as kc_v3

from heat.common import exception
from heat.common import heat_keystoneclient
from heat.tests.common import HeatTestCase
from heat.tests import utils


class KeystoneClientTest(HeatTestCase):
    """Test cases for heat.common.heat_keystoneclient."""

    def setUp(self):
        super(KeystoneClientTest, self).setUp()
        self.addCleanup(self.m.VerifyAll)

    def _stub_config(self, multiple=1):
        # Stub out cfg.CONF with dummy config
        mock_config = self.m.CreateMockAnything()
        mock_config.keystone_authtoken = self.m.CreateMockAnything()
        dummy_url = 'http://server.test:5000/v2.0'
        mock_config.keystone_authtoken.auth_uri = dummy_url
        mock_config.keystone_authtoken.admin_user = 'heat'
        mock_config.keystone_authtoken.admin_password = 'verybadpass'
        mock_config.keystone_authtoken.admin_tenant_name = 'service'

        mock_config.import_opt = self.m.CreateMockAnything()
        mock_config.clients_keystone = self.m.CreateMockAnything()
        for i in range(0, multiple):
            for cfg, ret in (('ca_file', None), ('insecure', False),
                             ('cert_file', None), ('key_file', None)):
                mock_config.import_opt(cfg,
                                       'heat.common.config',
                                       group='clients_keystone'
                                       ).AndReturn(None)
                setattr(mock_config.clients_keystone, cfg, ret)
        self.mock_config = mock_config
        heat_keystoneclient.KeystoneClient.conf = mock_config

    def _stub_admin_client(self, auth_ok=True):
        self.m.StubOutClassWithMocks(kc_v3, "Client")
        self.mock_admin_client = kc_v3.Client(
            auth_url='http://server.test:5000/v3',
            cacert=None,
            cert=None,
            endpoint='http://server.test:5000/v3',
            insecure=False,
            key=None,
            password='verybadpass',
            project_name='service',
            username='heat')
        self.mock_admin_client.authenticate().AndReturn(auth_ok)
        if auth_ok:
            self.mock_admin_client.auth_ref = self.m.CreateMockAnything()
            self.mock_admin_client.auth_ref.user_id = '1234'

    def _stubs_v3(self, method='token', auth_ok=True, trust_scoped=True,
                  user_id='trustor_user_id', mock_client=True,
                  config_multiple=1):
        self._stub_config(multiple=config_multiple)
        if mock_client:
            self.m.StubOutClassWithMocks(kc_v3, "Client")

        if method == 'token':
            self.mock_ks_v3_client = kc_v3.Client(
                token='abcd1234', project_name='test_tenant',
                auth_url='http://server.test:5000/v3',
                endpoint='http://server.test:5000/v3',
                cacert=None,
                cert=None,
                insecure=False,
                key=None)
        elif method == 'password':
            self.mock_ks_v3_client = kc_v3.Client(
                username='test_username',
                password='password',
                project_name='test_tenant',
                project_id='test_tenant_id',
                auth_url='http://server.test:5000/v3',
                endpoint='http://server.test:5000/v3',
                cacert=None,
                cert=None,
                insecure=False,
                key=None)
        elif method == 'trust':
            self.mock_ks_v3_client = kc_v3.Client(
                username='heat',
                password='verybadpass',
                project_name='service',
                auth_url='http://server.test:5000/v3',
                endpoint='http://server.test:5000/v3',
                trust_id='atrust123',
                cacert=None,
                cert=None,
                insecure=False,
                key=None)
            self.mock_ks_v3_client.auth_ref = self.m.CreateMockAnything()
            self.mock_ks_v3_client.auth_ref.user_id = user_id
            self.mock_ks_v3_client.auth_ref.trust_scoped = trust_scoped
            self.mock_ks_v3_client.auth_ref.auth_token = 'atrusttoken'

        self.mock_ks_v3_client.authenticate().AndReturn(auth_ok)

    def test_username_length(self):
        """Test that user names >64 characters are properly truncated."""

        self._stubs_v3()

        ctx = utils.dummy_context()
        ctx.trust_id = None

        # a >64 character user name and the expected version
        long_user_name = 'U' * 64 + 'S'
        good_user_name = long_user_name[-64:]
        # mock keystone client user functions
        self.mock_ks_v3_client.users = self.m.CreateMockAnything()
        mock_user = self.m.CreateMockAnything()
        mock_user.id = 'auser123'
        # when keystone is called, the name should have been truncated
        # to the last 64 characters of the long name
        self.mock_ks_v3_client.users.create(name=good_user_name,
                                            password='password',
                                            default_project=ctx.tenant_id
                                            ).AndReturn(mock_user)

        self.mock_ks_v3_client.roles = self.m.CreateMockAnything()
        self.mock_ks_v3_client.roles.list().AndReturn(self._mock_roles_list())
        self.mock_ks_v3_client.roles.grant(project=ctx.tenant_id,
                                           role='4546',
                                           user='auser123').AndReturn(None)
        self.m.ReplayAll()
        # call create_stack_user with a long user name.
        # the cleanup VerifyAll should verify that though we passed
        # long_user_name, keystone was actually called with a truncated
        # user name
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        heat_ks_client.create_stack_user(long_user_name, password='password')

    def test_create_stack_user_error_norole(self):
        """Test error path when no role is found."""

        self._stubs_v3()

        ctx = utils.dummy_context()
        ctx.trust_id = None

        self.mock_ks_v3_client.roles = self.m.CreateMockAnything()
        mock_roles_list = self._mock_roles_list(heat_stack_user='badrole')
        self.mock_ks_v3_client.roles.list().AndReturn(mock_roles_list)
        self.m.ReplayAll()
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        err = self.assertRaises(exception.Error,
                                heat_ks_client.create_stack_user,
                                'auser', password='password')
        self.assertIn('Can\'t find role heat_stack_user', err)

    def _mock_roles_list(self, heat_stack_user='heat_stack_user'):
        self.mock_config.heat_stack_user_role = 'heat_stack_user'
        mock_roles_list = []
        for r_id, r_name in (('1234', 'blah'), ('4546', heat_stack_user)):
            mock_role = self.m.CreateMockAnything()
            mock_role.id = r_id
            mock_role.name = r_name
            mock_roles_list.append(mock_role)
        return mock_roles_list

    def test_create_stack_domain_user(self):
        """Test creating a stack domain user."""

        ctx = utils.dummy_context()
        ctx.trust_id = None

        # mock keystone client functions
        self._stub_domain(ret_id='adomain123')
        self.mock_admin_client.users = self.m.CreateMockAnything()
        mock_user = self.m.CreateMockAnything()
        mock_user.id = 'duser123'
        self.mock_admin_client.users.create(name='duser',
                                            password=None,
                                            default_project='aproject',
                                            domain='adomain123'
                                            ).AndReturn(mock_user)
        self.mock_admin_client.roles = self.m.CreateMockAnything()
        self.mock_admin_client.roles.list().AndReturn(self._mock_roles_list())
        self.mock_admin_client.roles.grant(project='aproject',
                                           role='4546',
                                           user='duser123').AndReturn(None)
        self.m.ReplayAll()

        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        heat_ks_client.create_stack_domain_user(username='duser',
                                                project_id='aproject')

    def test_create_stack_domain_user_error_norole(self):
        """Test creating a stack domain user, no role error path."""

        ctx = utils.dummy_context()
        ctx.trust_id = None

        self._stub_config()
        self._stub_admin_client()

        # mock keystone client functions
        self.mock_admin_client.roles = self.m.CreateMockAnything()
        mock_roles_list = self._mock_roles_list(heat_stack_user='badrole')
        self.mock_admin_client.roles.list().AndReturn(mock_roles_list)
        self.m.ReplayAll()

        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        err = self.assertRaises(exception.Error,
                                heat_ks_client.create_stack_domain_user,
                                username='duser', project_id='aproject')
        self.assertIn('Can\'t find role heat_stack_user', err)

    def test_delete_stack_user(self):

        """Test deleting a stack user."""

        self._stubs_v3()

        ctx = utils.dummy_context()
        ctx.trust_id = None

        # mock keystone client delete function
        self.mock_ks_v3_client.users = self.m.CreateMockAnything()
        self.mock_ks_v3_client.users.delete(user='atestuser').AndReturn(None)
        self.m.ReplayAll()
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        heat_ks_client.delete_stack_user('atestuser')

    def test_init_v3_token(self):

        """Test creating the client, token auth."""

        self._stubs_v3()
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.username = None
        ctx.password = None
        ctx.trust_id = None
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        heat_ks_client.client_v3
        self.assertIsNotNone(heat_ks_client._client_v3)

    def test_init_v3_password(self):

        """Test creating the client, password auth."""

        self._stubs_v3(method='password')
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.auth_token = None
        ctx.trust_id = None
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        client_v3 = heat_ks_client.client_v3
        self.assertIsNotNone(client_v3)

    def test_init_v3_bad_nocreds(self):

        """Test creating the client, no credentials."""

        ctx = utils.dummy_context()
        ctx.auth_token = None
        ctx.trust_id = None
        ctx.username = None
        ctx.password = None
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertRaises(exception.AuthorizationFailure,
                          heat_ks_client._v3_client_init)

    def test_create_trust_context_trust_id(self):

        """Test create_trust_context with existing trust_id."""

        self._stubs_v3(method='trust')
        self.mock_config.deferred_auth_method = 'trusts'
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.trust_id = 'atrust123'
        ctx.trustor_user_id = 'trustor_user_id'

        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        trust_context = heat_ks_client.create_trust_context()
        self.assertEqual(ctx.to_dict(), trust_context.to_dict())

    def test_create_trust_context_trust_create(self):

        """Test create_trust_context when creating a trust."""

        class MockTrust(object):
            id = 'atrust123'

        self._stub_admin_client()

        self._stubs_v3(mock_client=False, config_multiple=2)
        self.mock_config.deferred_auth_method = 'trusts'
        self.mock_config.trusts_delegated_roles = ['heat_stack_owner']

        self.mock_ks_v3_client.auth_ref = self.m.CreateMockAnything()
        self.mock_ks_v3_client.auth_ref.user_id = '5678'
        self.mock_ks_v3_client.auth_ref.project_id = '42'
        self.mock_ks_v3_client.trusts = self.m.CreateMockAnything()
        self.mock_ks_v3_client.trusts.create(
            trustor_user='5678',
            trustee_user='1234',
            project='42',
            impersonation=True,
            role_names=['heat_stack_owner']).AndReturn(MockTrust())

        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.trust_id = None
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        trust_context = heat_ks_client.create_trust_context()
        self.assertEqual('atrust123', trust_context.trust_id)
        self.assertEqual('5678', trust_context.trustor_user_id)

    def test_init_admin_client(self):

        """Test the admin_client property."""

        self._stub_config()
        self._stub_admin_client()
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.username = None
        ctx.password = None
        ctx.trust_id = None
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertEqual(self.mock_admin_client, heat_ks_client.admin_client)
        self.assertEqual(self.mock_admin_client, heat_ks_client._admin_client)

    def test_init_admin_client_denied(self):

        """Test the admin_client property, auth failure path."""

        self._stub_config()
        self._stub_admin_client(auth_ok=False)
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.username = None
        ctx.password = None
        ctx.trust_id = None
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)

        # Define wrapper for property or the property raises the exception
        # outside of the assertRaises which fails the test
        def get_admin_client():
            heat_ks_client.admin_client

        self.assertRaises(exception.AuthorizationFailure,
                          get_admin_client)

    def test_trust_init(self):

        """Test consuming a trust when initializing."""

        self._stubs_v3(method='trust')
        self.mock_config.deferred_auth_method = 'trusts'
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.username = None
        ctx.password = None
        ctx.auth_token = None
        ctx.trust_id = 'atrust123'
        ctx.trustor_user_id = 'trustor_user_id'
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertIsNotNone(heat_ks_client.client_v3)

    def test_trust_init_fail(self):

        """Test consuming a trust when initializing, error scoping."""

        self._stubs_v3(method='trust', trust_scoped=False)
        self.mock_config.deferred_auth_method = 'trusts'
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.username = None
        ctx.password = None
        ctx.auth_token = None
        ctx.trust_id = 'atrust123'
        ctx.trustor_user_id = 'trustor_user_id'
        self.assertRaises(exception.AuthorizationFailure,
                          heat_keystoneclient.KeystoneClient, ctx)

    def test_trust_init_fail_impersonation(self):

        """Test consuming a trust when initializing, impersonation error."""

        self._stubs_v3(method='trust', user_id='wrong_user_id')
        self.mock_config.deferred_auth_method = 'trusts'
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.username = 'heat'
        ctx.password = None
        ctx.auth_token = None
        ctx.trust_id = 'atrust123'
        ctx.trustor_user_id = 'trustor_user_id'
        self.assertRaises(exception.AuthorizationFailure,
                          heat_keystoneclient.KeystoneClient, ctx)

    def test_trust_init_pw(self):

        """Test trust_id is takes precedence username/password specified."""

        self._stubs_v3(method='trust')
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.auth_token = None
        ctx.trust_id = 'atrust123'
        ctx.trustor_user_id = 'trustor_user_id'
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertIsNotNone(heat_ks_client._client_v3)

    def test_trust_init_token(self):

        """Test trust_id takes precedence when token specified."""

        self._stubs_v3(method='trust')
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.username = None
        ctx.password = None
        ctx.trust_id = 'atrust123'
        ctx.trustor_user_id = 'trustor_user_id'
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertIsNotNone(heat_ks_client._client_v3)

    def test_delete_trust(self):

        """Test delete_trust when deleting trust."""

        self._stubs_v3()
        self.mock_config.deferred_auth_method = 'trusts'
        self.mock_ks_v3_client.trusts = self.m.CreateMockAnything()
        self.mock_ks_v3_client.trusts.delete('atrust123').AndReturn(None)

        self.m.ReplayAll()
        ctx = utils.dummy_context()
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertIsNone(heat_ks_client.delete_trust(trust_id='atrust123'))

    def test_delete_trust_not_found(self):

        """Test delete_trust when trust already deleted."""

        self._stubs_v3()
        self.mock_config.deferred_auth_method = 'trusts'
        self.mock_ks_v3_client.trusts = self.m.CreateMockAnything()
        self.mock_ks_v3_client.trusts.delete('atrust123').AndRaise(
            kc_exception.NotFound)

        self.m.ReplayAll()
        ctx = utils.dummy_context()
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertIsNone(heat_ks_client.delete_trust(trust_id='atrust123'))

    def test_disable_stack_user(self):

        """Test disabling a stack user."""

        self._stubs_v3()

        ctx = utils.dummy_context()
        ctx.trust_id = None

        # mock keystone client update function
        self.mock_ks_v3_client.users = self.m.CreateMockAnything()
        self.mock_ks_v3_client.users.update(user='atestuser', enabled=False
                                            ).AndReturn(None)
        self.m.ReplayAll()
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        heat_ks_client.disable_stack_user('atestuser')

    def test_enable_stack_user(self):

        """Test enabling a stack user."""

        self._stubs_v3()

        ctx = utils.dummy_context()
        ctx.trust_id = None

        # mock keystone client update function
        self.mock_ks_v3_client.users = self.m.CreateMockAnything()
        self.mock_ks_v3_client.users.update(user='atestuser', enabled=True
                                            ).AndReturn(None)
        self.m.ReplayAll()
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        heat_ks_client.enable_stack_user('atestuser')

    def test_create_ec2_keypair(self):

        """Test creating ec2 credentials."""

        self._stubs_v3()

        ctx = utils.dummy_context()
        ctx.trust_id = None

        ex_data = {'access': 'dummy_access',
                   'secret': 'dummy_secret'}
        ex_data_json = json.dumps(ex_data)

        # stub UUID.hex to match ex_data
        self.m.StubOutWithMock(uuid, 'uuid4')
        mock_uuid_access = self.m.CreateMockAnything()
        mock_uuid_access.hex = 'dummy_access'
        uuid.uuid4().AndReturn(mock_uuid_access)
        mock_uuid_secret = self.m.CreateMockAnything()
        mock_uuid_secret.hex = 'dummy_secret'
        uuid.uuid4().AndReturn(mock_uuid_secret)

        # mock keystone client credentials functions
        self.mock_ks_v3_client.credentials = self.m.CreateMockAnything()
        mock_credential = self.m.CreateMockAnything()
        mock_credential.id = '123456'
        mock_credential.user_id = 'atestuser'
        mock_credential.blob = ex_data_json
        mock_credential.type = 'ec2'

        # mock keystone client create function
        self.mock_ks_v3_client.credentials.create(
            user='atestuser', type='ec2', data=ex_data_json,
            project=ctx.tenant_id).AndReturn(mock_credential)
        self.m.ReplayAll()
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        ec2_cred = heat_ks_client.create_ec2_keypair(user_id='atestuser')
        self.assertEqual('123456', ec2_cred.id)
        self.assertEqual('dummy_access', ec2_cred.access)
        self.assertEqual('dummy_secret', ec2_cred.secret)

    def test_get_ec2_keypair_id(self):

        """Test getting ec2 credential by id."""

        user_id = 'atestuser'
        self._stubs_v3(user_id=user_id)

        ctx = utils.dummy_context()
        ctx.trust_id = None

        ex_data = {'access': 'access123',
                   'secret': 'secret456'}
        ex_data_json = json.dumps(ex_data)

        # Create a mock credential response
        credential_id = 'acredential123'
        mock_credential = self.m.CreateMockAnything()
        mock_credential.id = credential_id
        mock_credential.user_id = user_id
        mock_credential.blob = ex_data_json
        mock_credential.type = 'ec2'

        # mock keystone client get function
        self.mock_ks_v3_client.credentials = self.m.CreateMockAnything()
        self.mock_ks_v3_client.credentials.get(
            credential_id).AndReturn(mock_credential)
        self.m.ReplayAll()
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        ec2_cred = heat_ks_client.get_ec2_keypair(credential_id=credential_id)
        self.assertEqual(credential_id, ec2_cred.id)
        self.assertEqual('access123', ec2_cred.access)
        self.assertEqual('secret456', ec2_cred.secret)

    def _mock_credential_list(self, user_id):
        """Create a mock credential list response."""
        mock_credential_list = []
        for x in (1, 2, 3):
            mock_credential = self.m.CreateMockAnything()
            mock_credential.id = 'credential_id%s' % x
            mock_credential.user_id = user_id
            mock_credential.blob = json.dumps({'access': 'access%s' % x,
                                               'secret': 'secret%s' % x})
            mock_credential.type = 'ec2'
            mock_credential_list.append(mock_credential)

        # mock keystone client list function
        self.mock_ks_v3_client.credentials = self.m.CreateMockAnything()
        self.mock_ks_v3_client.credentials.list().AndReturn(
            mock_credential_list)

    def test_get_ec2_keypair_access(self):

        """Test getting ec2 credential by access."""

        user_id = 'atestuser'
        self._stubs_v3(user_id=user_id)

        ctx = utils.dummy_context()
        ctx.trust_id = None

        self._mock_credential_list(user_id=user_id)
        self.m.ReplayAll()
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        ec2_cred = heat_ks_client.get_ec2_keypair(access='access2')
        self.assertEqual('credential_id2', ec2_cred.id)
        self.assertEqual('access2', ec2_cred.access)
        self.assertEqual('secret2', ec2_cred.secret)

    def test_get_ec2_keypair_error(self):

        """Test getting ec2 credential error path."""

        ctx = utils.dummy_context()
        ctx.trust_id = None

        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertRaises(ValueError, heat_ks_client.get_ec2_keypair)

    def test_delete_ec2_keypair_id(self):

        """Test deleting ec2 credential by id."""

        user_id = 'atestuser'
        self._stubs_v3(user_id=user_id)

        ctx = utils.dummy_context()
        ctx.trust_id = None

        # mock keystone client credentials functions
        credential_id = 'acredential123'
        self.mock_ks_v3_client.credentials = self.m.CreateMockAnything()

        # mock keystone client delete function
        self.mock_ks_v3_client.credentials = self.m.CreateMockAnything()
        self.mock_ks_v3_client.credentials.delete(credential_id)
        self.m.ReplayAll()
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertIsNone(heat_ks_client.delete_ec2_keypair(
                          credential_id=credential_id))

    def test_delete_ec2_keypair_access(self):

        """Test deleting ec2 credential by access."""

        user_id = 'atestuser'
        self._stubs_v3(user_id=user_id)

        ctx = utils.dummy_context()
        ctx.trust_id = None

        self._mock_credential_list(user_id=user_id)

        # mock keystone client delete function
        self.mock_ks_v3_client.credentials.delete(
            'credential_id2').AndReturn(None)
        self.m.ReplayAll()
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertIsNone(heat_ks_client.delete_ec2_keypair(access='access2'))

    def test_deleting_ec2_keypair_error(self):

        """Test deleting ec2 credential error path."""

        ctx = utils.dummy_context()
        ctx.trust_id = None

        self.m.ReplayAll()
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertRaises(ValueError, heat_ks_client.delete_ec2_keypair)

    def _stub_domain(self, cfg_name='adomain', ret_id=None):
        self._stub_config()
        self._stub_admin_client()
        self.mock_config.stack_user_domain = cfg_name
        self.mock_admin_client.domains = self.m.CreateMockAnything()
        if ret_id:
            dummy = self.m.CreateMockAnything()
            dummy.id = ret_id
            self.mock_admin_client.domains.list(
                name=cfg_name).AndReturn([dummy])
        else:
            self.mock_admin_client.domains.list(name=cfg_name).AndReturn([])

    def test_stack_domain_id_new(self):

        """Test the stack_domain_id property when the domain doesn't exist."""

        self._stub_domain(cfg_name='testname')
        dummy = self.m.CreateMockAnything()
        dummy.id = 'adomain123'
        self.mock_admin_client.domains.create(name='testname').AndReturn(dummy)
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.trust_id = None
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertEqual('adomain123', heat_ks_client.stack_domain_id)

    def test_stack_domain_id_existing(self):

        """Test the stack_domain_id property when the domain exists."""

        self._stub_domain(ret_id='adomain123')
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.trust_id = None
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertEqual('adomain123', heat_ks_client.stack_domain_id)

    def test_create_stack_domain_project(self):

        """Test the create_stack_domain_project function."""

        ctx = utils.dummy_context()
        ctx.trust_id = None
        expected_name = '%s-astack' % ctx.tenant_id

        self._stub_domain(ret_id='adomain123')
        self.mock_admin_client.projects = self.m.CreateMockAnything()
        dummy = self.m.CreateMockAnything()
        dummy.id = 'aproject123'
        self.mock_admin_client.projects.create(
            name=expected_name,
            domain='adomain123',
            description='Heat stack user project').AndReturn(dummy)
        self.m.ReplayAll()

        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        self.assertEqual('aproject123',
                         heat_ks_client.create_stack_domain_project(
                             stack_name='astack'))

    def test_delete_stack_domain_project(self):

        """Test the delete_stack_domain_project function."""

        self._stub_config()
        self._stub_admin_client()
        self.mock_admin_client.projects = self.m.CreateMockAnything()
        self.mock_admin_client.projects.delete(project='aprojectid')
        self.m.ReplayAll()

        ctx = utils.dummy_context()
        ctx.trust_id = None
        heat_ks_client = heat_keystoneclient.KeystoneClient(ctx)
        heat_ks_client.delete_stack_domain_project(project_id='aprojectid')
