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
from heat.engine import client as engine

logger = logging.getLogger('heat.api.v1.stacks')


class StackController(object):

    """
    WSGI controller for stacks resource in heat v1 API

    """

    def __init__(self, options):
        self.options = options
        engine.configure_engine_client(options)

    def list(self, req):
        """
        Returns the following information for all stacks:
        """
        c = engine.get_engine_client(req.context)
        stack_list = c.get_stacks(**req.params)

        res = {'ListStacksResponse': {'ListStacksResult': {'StackSummaries': [] } } }
        summaries = res['ListStacksResponse']['ListStacksResult']['StackSummaries']
        for s in stack_list:
            summaries.append(s)

        return res

    def describe(self, req):
        """
        Returns the following information for all stacks:
        """
        c = engine.get_engine_client(req.context)

        stack_list = c.show_stack(req.params['StackName'])
        res = {'DescribeStacksResult': {'Stacks': [] } }
        stacks = res['DescribeStacksResult']['Stacks']
        for s in stack_list:
            mem = {'member': s}
            stacks.append(mem)

        return res

    def _get_template(self, body):
        if body.has_key('TemplateBody'):
            logger.info('TemplateBody ...')
            return body['TemplateBody']
        elif body.has_key('TemplateUrl'):
            logger.info('TemplateUrl %s' % body['TemplateUrl'])
            url = urlparse.urlparse(body['TemplateUrl'])
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


    def create(self, req, body):
        """
        Returns the following information for all stacks:
        """
        c = engine.get_engine_client(req.context)

        try:
            templ = self._get_template(body)
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
        stack['StackName'] = body['StackName']

        return c.create_stack(stack, **req.params)

    def validate_template(self, req, body):

        client = engine.get_engine_client(req.context)

        try:
            templ = self._get_template(body)
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
        return client.validate_template(stack, **req.params)

    def delete(self, req):
        """
        Returns the following information for all stacks:
        """
        logger.info('in api delete ')
        c = engine.get_engine_client(req.context)
        res = c.delete_stack(req.params['StackName'])
        if res.status == 200:
            return {'DeleteStackResult': ''}
        else:
            return webob.exc.HTTPNotFound()


    def events_list(self, req):
        """
        Returns the following information for all stacks:
        """
        c = engine.get_engine_client(req.context)
        stack_list = c.get_stack_events(**req.params)

        res = {'DescribeStackEventsResult': {'StackEvents': [] } }
        summaries = res['DescribeStackEventsResult']['StackEvents']
        for s in stack_list:
            summaries.append(s)

        return res

def create_resource(options):
    """Stacks resource factory method."""
    deserializer = wsgi.JSONRequestDeserializer()
    serializer = wsgi.JSONResponseSerializer()
    return wsgi.Resource(StackController(options), deserializer, serializer)
