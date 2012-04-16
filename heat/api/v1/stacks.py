# vim: tabstop=4 shiftwidth=4 softtabstop=4

#
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

"""
/stack endpoint for heat v1 API
"""
import httplib
import json
import logging
import os
import socket
import sys
import urlparse
import webob
from webob.exc import (HTTPNotFound,
                       HTTPConflict,
                       HTTPBadRequest)
from heat.common import wsgi
from heat.common import config
from heat import rpc
from heat import context
import heat.rpc.common as rpc_common


logger = logging.getLogger('heat.api.v1.stacks')


class StackController(object):

    """
    WSGI controller for stacks resource in heat v1 API

    """

    def __init__(self, options):
        self.options = options

    def list(self, req):
        """
        Returns the following information for all stacks:
        """
        con = context.get_admin_context()
        stack_list = rpc.call(con, 'engine',
                            {'method': 'list_stacks',
                            'args': {'params': dict(req.params)}})

        res = {'ListStacksResponse': {
                'ListStacksResult': {'StackSummaries': []}}}
        results = res['ListStacksResponse']['ListStacksResult']
        summaries = results['StackSummaries']
        if stack_list != None:
            for s in stack_list['stacks']:
                summaries.append(s)

        return res

    def describe(self, req):
        """
        Returns the following information for all stacks:
        """
        con = context.get_admin_context()

        try:
            stack_list = rpc.call(con, 'engine',
                              {'method': 'show_stack',
                               'args': {'stack_name': req.params['StackName'],
                                'params': dict(req.params)}})

        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

        res = {'DescribeStacksResult': {'Stacks': []}}
        stacks = res['DescribeStacksResult']['Stacks']
        for s in stack_list['stacks']:
            mem = {'member': s}
            stacks.append(mem)

        return res

    def _get_template(self, req):
        if 'TemplateBody' in req.params:
            logger.info('TemplateBody ...')
            return req.params['TemplateBody']
        elif 'TemplateUrl' in req.params:
            logger.info('TemplateUrl %s' % req.params['TemplateUrl'])
            url = urlparse.urlparse(req.params['TemplateUrl'])
            if url.scheme == 'https':
                conn = httplib.HTTPSConnection(url.netloc)
            else:
                conn = httplib.HTTPConnection(url.netloc)
            conn.request("GET", url.path)
            r1 = conn.getresponse()
            logger.info('status %d' % r1.status)
            if r1.status == 200:
                data = r1.read()
                conn.close()
            else:
                data = None
            return data

        return None

    def create(self, req):
        """
        Returns the following information for all stacks:
        """
        con = context.get_admin_context()

        try:
            templ = self._get_template(req)
        except socket.gaierror:
            msg = _('Invalid Template URL')
            return webob.exc.HTTPBadRequest(explanation=msg)
        if templ is None:
            msg = _("TemplateBody or TemplateUrl were not given.")
            return webob.exc.HTTPBadRequest(explanation=msg)

        try:
            stack = json.loads(templ)
        except ValueError:
            msg = _("The Template must be a JSON document.")
            return webob.exc.HTTPBadRequest(explanation=msg)
        stack['StackName'] = req.params['StackName']

        try:
            return rpc.call(con, 'engine',
                            {'method': 'create_stack',
                             'args': {'stack_name': req.params['StackName'],
                                      'template': stack,
                                      'params': dict(req.params)}})
        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

    def validate_template(self, req):

        con = context.get_admin_context()

        try:
            templ = self._get_template(req)
        except socket.gaierror:
            msg = _('Invalid Template URL')
            return webob.exc.HTTPBadRequest(explanation=msg)
        if templ is None:
            msg = _("TemplateBody or TemplateUrl were not given.")
            return webob.exc.HTTPBadRequest(explanation=msg)

        try:
            stack = json.loads(templ)
        except ValueError:
            msg = _("The Template must be a JSON document.")
            return webob.exc.HTTPBadRequest(explanation=msg)

        logger.info('validate_template')
        try:
            return con.validate_template(stack, **req.params)
        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

    def delete(self, req):
        """
        Returns the following information for all stacks:
        """
        con = context.get_admin_context()

        try:
            res = rpc.call(con, 'engine',
                       {'method': 'delete_stack',
                        'args': {'stack_name': req.params['StackName'],
                        'params': dict(req.params)}})
        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

        if res == None:
            return {'DeleteStackResult': ''}
        else:
            return {'DeleteStackResult': res['Error']}

    def events_list(self, req):
        """
        Returns the following information for all stacks:
        """
        con = context.get_admin_context()
        stack_name = req.params.get('StackName', None)
        try:
            event_res = rpc.call(con, 'engine',
                             {'method': 'list_events',
                              'args': {'stack_name': stack_name}})
        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

        events = 'Error' not in event_res and event_res['events'] or []

        return {'DescribeStackEventsResult': {'StackEvents': events}}


def create_resource(options):
    """
    Stacks resource factory method.
    """
    deserializer = wsgi.JSONRequestDeserializer()
    serializer = wsgi.JSONResponseSerializer()
    return wsgi.Resource(StackController(options), deserializer, serializer)
