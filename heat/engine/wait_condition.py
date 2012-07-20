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

import eventlet
import json

from heat.common import exception
from heat.engine import resources

from heat.openstack.common import log as logging

logger = logging.getLogger('heat.engine.wait_condition')


class WaitConditionHandle(resources.Resource):
    '''
    the main point of this class is to :
    have no dependancies (so the instance can reference it)
    generate a unique url (to be returned in the refernce)
    then the cfn-signal will use this url to post to and
    WaitCondition will poll it to see if has been written to.
    '''
    properties_schema = {}

    def __init__(self, name, json_snippet, stack):
        super(WaitConditionHandle, self).__init__(name, json_snippet, stack)

    def handle_create(self):
        self.instance_id = '%s/stacks/%s/resources/%s' % \
                           (resources.Metadata.server(),
                            self.stack.id,
                            self.name)

    def handle_update(self):
        return self.UPDATE_REPLACE

WAIT_STATUSES = (
    WAITING,
    TIMEDOUT,
    SUCCESS,
) = (
    'WAITING',
    'TIMEDOUT',
    'SUCCESS',
)


class WaitCondition(resources.Resource):
    properties_schema = {'Handle': {'Type': 'String',
                                    'Required': True},
                         'Timeout': {'Type': 'Number',
                                    'Required': True,
                                    'MinValue': '1'},
                         'Count': {'Type': 'Number',
                                   'MinValue': '1'}}

    def __init__(self, name, json_snippet, stack):
        super(WaitCondition, self).__init__(name, json_snippet, stack)
        self.resource_id = None

        self.timeout = int(self.t['Properties']['Timeout'])
        self.count = int(self.t['Properties'].get('Count', '1'))

    def _get_handle_resource_id(self):
        if self.resource_id is None:
            self.calculate_properties()
            handle_url = self.properties['Handle']
            self.resource_id = handle_url.split('/')[-1]
        return self.resource_id

    def handle_create(self):
        tmo = None
        try:
            # keep polling our Metadata to see if the cfn-signal has written
            # it yet. The execution here is limited by timeout.
            with eventlet.Timeout(self.timeout) as tmo:
                handle = self.stack[self._get_handle_resource_id()]

                status = WAITING
                reason = ''
                sleep_time = 1

                while status == WAITING:
                    meta = handle.metadata
                    status = meta.get('Status', WAITING)
                    reason = meta.get('Reason', 'Reason not provided')
                    logger.debug('got %s' % json.dumps(meta))
                    if status == WAITING:
                        logger.debug('Waiting for the Metadata[Status]')
                        eventlet.sleep(sleep_time)
                        sleep_time = min(sleep_time * 2, self.timeout / 4)

        except eventlet.Timeout as t:
            if t is not tmo:
                # not my timeout
                raise
            else:
                status = TIMEDOUT
                reason = 'Timed out waiting for instance'

        if status != SUCCESS:
            raise exception.Error(reason)

    def handle_update(self):
        return self.UPDATE_REPLACE

    def FnGetAtt(self, key):
        res = None
        if key == 'Data':
            try:
                meta = self.metadata
                if meta and 'Data' in meta:
                    res = meta['Data']
            except Exception as ex:
                pass

        else:
            raise exception.InvalidTemplateAttribute(resource=self.name,
                                                     key=key)

        logger.debug('%s.GetAtt(%s) == %s' % (self.name, key, res))
        return unicode(res)
