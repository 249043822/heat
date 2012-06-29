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
import re
import urlparse
import webob
from webob.exc import (HTTPNotFound,
                       HTTPConflict,
                       HTTPBadRequest)
from heat.common import wsgi
from heat.common import config
from heat.common import context
from heat import utils
from heat import rpc
import heat.rpc.common as rpc_common


logger = logging.getLogger('heat.api.v1.stacks')


class StackController(object):

    """
    WSGI controller for stacks resource in heat v1 API

    """

    def __init__(self, options):
        self.options = options

    # On valid non-error response, we add a host:port:stack prefix
    # This formats the StackId in the response more like the AWS spec
    def _stackid_addprefix(self, resp):
        if 'StackId' in resp:
            hostportprefix = ":".join([socket.gethostname(),
                str(self.options.bind_port), "stack"])
            resp['StackId'] = "/".join([hostportprefix, resp['StackId']])
        return resp

    def list(self, req):
        """
        Returns the following information for all stacks:
        """
        con = req.context
        parms = dict(req.params)

        stack_list = rpc.call(con, 'engine',
                            {'method': 'list_stacks',
                            'args': {'params': parms}})

        res = {'ListStacksResponse': {
                'ListStacksResult': {'StackSummaries': []}}}
        results = res['ListStacksResponse']['ListStacksResult']
        summaries = results['StackSummaries']
        if stack_list is not None:
            for s in stack_list['stacks']:
                summaries.append(self._stackid_addprefix(s))

        return res

    def describe(self, req):
        """
        Returns the following information for all stacks:
        """
        con = req.context
        parms = dict(req.params)

        # If no StackName parameter is passed, we pass None into the engine
        # this returns results for all stacks (visible to this user), which
        # is the behavior described in the AWS DescribeStacks API docs
        stack_name = None
        if 'StackName' in req.params:
            stack_name = req.params['StackName']

        try:
            stack_list = rpc.call(con, 'engine',
                              {'method': 'show_stack',
                               'args': {'stack_name': stack_name,
                                'params': parms}})

        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

        res = {'DescribeStacksResult': {'Stacks': []}}
        stacks = res['DescribeStacksResult']['Stacks']
        for s in stack_list['stacks']:
            # Reformat Parameters dict-of-dict into AWS API format
            # This is a list-of-dict with nasty "ParameterKey" : key
            # "ParameterValue" : value format.
            s['Parameters'] = [{'ParameterKey':k,
                'ParameterValue':v.get('Default')}
                for (k, v) in s['Parameters'].items()]
            stacks.append(self._stackid_addprefix(s))

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
        con = req.context
        parms = dict(req.params)

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

        try:
            res = rpc.call(con, 'engine',
                            {'method': 'create_stack',
                             'args': {'stack_name': req.params['StackName'],
                                      'template': stack,
                                      'params': parms}})
        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

        # Note boto expects CreateStackResult wrapped in CreateStackResponse
        # CreateStackResponse is not mentioned in the aws API docs, so we
        # need to check against a real AWS response to ensure this is correct
        return {'CreateStackResponse':
            {'CreateStackResult': self._stackid_addprefix(res)}}

    def get_template(self, req):

        con = req.context
        parms = dict(req.params)

        logger.info('get_template')
        try:
            templ = rpc.call(con, 'engine',
                             {'method': 'get_template',
                              'args': {'stack_name': req.params['StackName'],
                                       'params': parms}})
        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

        if templ is None:
            return webob.exc.HTTPNotFound('stack not found')

        return {'GetTemplateResult': {'TemplateBody': templ}}

    def estimate_template_cost(self, req):
        return {'EstimateTemplateCostResult': {
                'Url': 'http://en.wikipedia.org/wiki/Gratis'}}

    def validate_template(self, req):

        con = req.context
        parms = dict(req.params)

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
            return rpc.call(con, 'engine',
                            {'method': 'validate_template',
                             'args': {'template': stack,
                                      'params': parms}})
        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

    def delete(self, req):
        """
        Returns the following information for all stacks:
        """
        con = req.context
        parms = dict(req.params)

        try:
            res = rpc.call(con, 'engine',
                       {'method': 'delete_stack',
                        'args': {'stack_name': req.params['StackName'],
                        'params': parms}})

        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

        if res is None:
            return {'DeleteStackResult': ''}
        else:
            return {'DeleteStackResult': res['Error']}

    def events_list(self, req):
        """
        Returns the following information for all stacks:
        """
        con = req.context
        parms = dict(req.params)

        stack_name = req.params.get('StackName', None)
        try:
            event_res = rpc.call(con, 'engine',
                             {'method': 'list_events',
                              'args': {'stack_name': stack_name,
                              'params': parms}})
        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

        events = 'Error' not in event_res and event_res['events'] or []

        return {'DescribeStackEventsResult': {'StackEvents': events}}

    def describe_stack_resource(self, req):
        """
        Return the details of the given resource belonging to the given stack.
        """
        con = req.context
        args = {
            'stack_name': req.params.get('StackName'),
            'resource_name': req.params.get('LogicalResourceId'),
        }

        try:
            resource_details = rpc.call(con, 'engine',
                              {'method': 'describe_stack_resource',
                               'args': args})

        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

        return {
            'DescribeStackResourceResponse': {
                'DescribeStackResourceResult': {
                    'StackResourceDetail': resource_details,
                },
            },
        }

    def describe_stack_resources(self, req):
        """
        Return details of resources specified by the parameters.

        `StackName`: returns all resources belonging to the stack
        `PhysicalResourceId`: returns all resources belonging to the stack this
                              resource is associated with.

        Only one of the parameters may be specified.

        Optional parameter:

        `LogicalResourceId`: filter the resources list by the logical resource
        id.
        """
        con = req.context
        stack_name = req.params.get('StackName')
        physical_resource_id = req.params.get('PhysicalResourceId')
        if stack_name and physical_resource_id:
            msg = 'Use `StackName` or `PhysicalResourceId` but not both'
            return webob.exc.HTTPBadRequest(msg)

        args = {
            'stack_name': stack_name,
            'physical_resource_id': physical_resource_id,
            'logical_resource_id': req.params.get('LogicalResourceId'),
        }

        try:
            resources = rpc.call(con, 'engine',
                              {'method': 'describe_stack_resources',
                               'args': args})

        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

        response = {
            'DescribeStackResourcesResult': {
                'StackResources': resources,
            }
        }
        return response

    def list_stack_resources(self, req):
        """
        Return summary of the resources belonging to the specified stack.

        """
        con = req.context

        try:
            resources = rpc.call(con, 'engine', {
                'method': 'list_stack_resources',
                'args': {'stack_name': req.params.get('StackName')}
            })
        except rpc_common.RemoteError as ex:
            return webob.exc.HTTPBadRequest(str(ex))

        return {
            'ListStackResourcesResponse': {
                'ListStackResourcesResult': {
                    'StackResourceSummaries': resources,
                },
            },
        }


def create_resource(options):
    """
    Stacks resource factory method.
    """
    deserializer = wsgi.JSONRequestDeserializer()
    return wsgi.Resource(StackController(options), deserializer)
