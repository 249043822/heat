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
Reference implementation stacks server WSGI controller
"""
import json
import logging

import webob
from webob.exc import (HTTPNotFound,
                       HTTPConflict,
                       HTTPBadRequest)

from heat.common import exception
from heat.common import wsgi

from heat.engine import capelistener
from heat.engine import json2capexml
from heat.engine import systemctl


logger = logging.getLogger('heat.engine.api.v1.stacks')

stack_db = {}

class Controller(object):
    '''
    bla
    '''

    def __init__(self, conf):
        self.conf = conf
        self.listener = capelistener.CapeEventListener()


    def index(self, req, format='json'):
        logger.info('format is %s' % format)
        res = {'stacks': [] }
        for s in stack_db:
            mem = {}
            mem['StackId'] = stack_db[s]['StackId']
            mem['StackName'] = s
            mem['CreationTime'] = 'now'
            try:
                mem['TemplateDescription'] = stack_db[s]['Description']
                mem['StackStatus'] = stack_db[s]['StackStatus']
            except:
                mem['TemplateDescription'] = 'No description'
                mem['StackStatus'] = 'unknown'
            res['stacks'].append(mem)

        return res

    def show(self, req, id):
        res = {'stacks': [] }
        if stack_db.has_key(id):
            mem = {}
            mem['StackId'] = stack_db[id]['StackId']
            mem['StackName'] = id
            mem['CreationTime'] = 'TODO'
            mem['LastUpdatedTime'] = 'TODO'
            mem['NotificationARNs'] = 'TODO'
            mem['Outputs'] = [{'Description': 'TODO', 'OutputKey': 'TODO', 'OutputValue': 'TODO' }]
            mem['Parameters'] = stack_db[id]['Parameters']
            mem['StackStatusReason'] = 'TODO'
            mem['TimeoutInMinutes'] = 'TODO'
            try:
                mem['TemplateDescription'] = stack_db[id]['Description']
                mem['StackStatus'] = stack_db[id]['StackStatus']
            except:
                mem['TemplateDescription'] = 'No description'
                mem['StackStatus'] = 'unknown'
            res['stacks'].append(mem)
        else:
            return webob.exc.HTTPNotFound('No stack by that name')

        return res

    def create(self, req, body=None):

        if body is None:
            msg = _("TemplateBody or TemplateUrl were not given.")
            return webob.exc.HTTPBadRequest(explanation=msg)

        if stack_db.has_key(body['StackName']):
            msg = _("Stack already exists with that name.")
            return webob.exc.HTTPConflict(msg)

        stack = body
        stack['StackId'] = body['StackName']
        stack['StackStatus'] = 'CREATE_COMPLETE'
        # TODO self._apply_user_parameters(req, stack)
        stack_db[body['StackName']] = stack

        cape_transformer = json2capexml.Json2CapeXml(stack, body['StackName'])
        cape_transformer.convert_and_write()

        systemctl.systemctl('start', 'pcloud-cape-sshd', body['StackName'])

        return {'stack': {'id': body['StackName']}}

    def delete(self, req, id):
        if not stack_db.has_key(id):
            return webob.exc.HTTPNotFound('No stack by that name')

        logger.info('deleting stack %s' % id)
        systemctl.systemctl('stop', 'pcloud-cape-sshd', id)
        del stack_db[id]
        return None

def create_resource(conf):
    """Stacks resource factory method."""
    deserializer = wsgi.JSONRequestDeserializer()
    serializer = wsgi.JSONResponseSerializer()
    return wsgi.Resource(Controller(conf), deserializer, serializer)
