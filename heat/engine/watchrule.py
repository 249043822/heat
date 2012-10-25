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


import datetime
from heat.openstack.common import log as logging
from heat.openstack.common import timeutils
from heat.engine import timestamp
from heat.db import api as db_api
from heat.engine import parser
from heat.common import context as ctxtlib
import eventlet

logger = logging.getLogger('heat.engine.watchrule')
greenpool = eventlet.GreenPool()


class WatchRule(object):
    WATCH_STATES = (ALARM, NORMAL, NODATA
    ) = ('ALARM', 'NORMAL', 'NODATA')

    ACTION_MAP = {ALARM: 'AlarmActions',
                  NORMAL: 'OKActions',
                  NODATA: 'InsufficientDataActions'}

    created_at = timestamp.Timestamp(db_api.watch_rule_get, 'created_at')
    updated_at = timestamp.Timestamp(db_api.watch_rule_get, 'updated_at')

    def __init__(self, context, watch_name, rule, stack_name, state=NORMAL,
                 wid=None, watch_data=[], last_evaluated=timeutils.utcnow()):
        self.context = context
        self.now = timeutils.utcnow()
        self.name = watch_name
        self.state = state
        self.rule = rule
        self.stack_name = stack_name
        self.timeperiod = datetime.timedelta(seconds=int(rule['Period']))
        self.id = wid
        self.watch_data = watch_data
        self.last_evaluated = last_evaluated

    @classmethod
    def load(cls, context, watch_name):
        '''
        Load the watchrule from the DB by name
        '''
        dbwr = None
        try:
            dbwr = db_api.watch_rule_get_by_name(context, watch_name)
        except Exception as ex:
            logger.warn('show_watch (%s) db error %s' %
                        (watch_name, str(ex)))
        if not dbwr:
            raise AttributeError('Unknown watch name %s' % watch_name)
        else:
            return cls(context=context,
                       watch_name=dbwr.name,
                       rule=dbwr.rule,
                       stack_name=dbwr.stack_name,
                       state=dbwr.state,
                       wid=dbwr.id,
                       watch_data=dbwr.watch_data,
                       last_evaluated=dbwr.last_evaluated)

    def store(self):
        '''
        Store the watchrule in the database and return its ID
        If self.id is set, we update the existing rule
        '''

        wr_values = {
            'name': self.name,
            'rule': self.rule,
            'state': self.state,
            'stack_name': self.stack_name
        }

        if not self.id:
            wr = db_api.watch_rule_create(self.context, wr_values)
            self.id = wr.id
        else:
            db_api.watch_rule_update(self.context, self.id, wr_values)

    def do_data_cmp(self, data, threshold):
        op = self.rule['ComparisonOperator']
        if op == 'GreaterThanThreshold':
            return data > threshold
        elif op == 'GreaterThanOrEqualToThreshold':
            return data >= threshold
        elif op == 'LessThanThreshold':
            return data < threshold
        elif op == 'LessThanOrEqualToThreshold':
            return data <= threshold
        else:
            return False

    def do_Maximum(self):
        data = 0
        have_data = False
        for d in self.watch_data:
            if d.created_at < self.now - self.timeperiod:
                continue
            if not have_data:
                data = float(d.data[self.rule['MetricName']]['Value'])
                have_data = True
            if float(d.data[self.rule['MetricName']]['Value']) > data:
                data = float(d.data[self.rule['MetricName']]['Value'])

        if not have_data:
            return self.NODATA

        if self.do_data_cmp(data,
                            float(self.rule['Threshold'])):
            return self.ALARM
        else:
            return self.NORMAL

    def do_Minimum(self):
        data = 0
        have_data = False
        for d in self.watch_data:
            if d.created_at < self.now - self.timeperiod:
                continue
            if not have_data:
                data = float(d.data[self.rule['MetricName']]['Value'])
                have_data = True
            elif float(d.data[self.rule['MetricName']]['Value']) < data:
                data = float(d.data[self.rule['MetricName']]['Value'])

        if not have_data:
            return self.NODATA

        if self.do_data_cmp(data,
                            float(self.rule['Threshold'])):
            return self.ALARM
        else:
            return self.NORMAL

    def do_SampleCount(self):
        '''
        count all samples within the specified period
        '''
        data = 0
        for d in self.watch_data:
            if d.created_at < self.now - self.timeperiod:
                continue
            data = data + 1

        if self.do_data_cmp(data,
                            float(self.rule['Threshold'])):
            return self.ALARM
        else:
            return self.NORMAL

    def do_Average(self):
        data = 0
        samples = 0
        for d in self.watch_data:
            if d.created_at < self.now - self.timeperiod:
                continue
            samples = samples + 1
            data = data + float(d.data[self.rule['MetricName']]['Value'])

        if samples == 0:
            return self.NODATA

        data = data / samples
        if self.do_data_cmp(data,
                            float(self.rule['Threshold'])):
            return self.ALARM
        else:
            return self.NORMAL

    def do_Sum(self):
        data = 0
        for d in self.watch_data:
            if d.created_at < self.now - self.timeperiod:
                logger.debug('ignoring %s' % str(d.data))
                continue
            data = data + float(d.data[self.rule['MetricName']]['Value'])

        if self.do_data_cmp(data,
                            float(self.rule['Threshold'])):
            return self.ALARM
        else:
            return self.NORMAL

    def get_alarm_state(self):
        fn = getattr(self, 'do_%s' % self.rule['Statistic'])
        return fn()

    def evaluate(self):
        # has enough time progressed to run the rule
        self.now = timeutils.utcnow()
        if self.now < (self.last_evaluated + self.timeperiod):
            return
        self.run_rule()

    def run_rule(self):
        new_state = self.get_alarm_state()

        if new_state != self.state:
            if self.rule_action(new_state):
                self.state = new_state

        self.last_evaluated = self.now
        self.store()

    def rule_action(self, new_state):
        logger.warn('WATCH: stack:%s, watch_name:%s %s',
                    self.stack_name, self.name, new_state)

        actioned = False
        if not self.ACTION_MAP[new_state] in self.rule:
            logger.info('no action for new state %s',
                        new_state)
            actioned = True
        else:
            s = db_api.stack_get_by_name(None, self.stack_name)
            if s and s.status in (parser.Stack.CREATE_COMPLETE,
                                  parser.Stack.UPDATE_COMPLETE):
                user_creds = db_api.user_creds_get(s.user_creds_id)
                ctxt = ctxtlib.RequestContext.from_dict(user_creds)
                stack = parser.Stack.load(ctxt, s.id)
                for a in self.rule[self.ACTION_MAP[new_state]]:
                    greenpool.spawn_n(stack[a].alarm)
                actioned = True
            else:
                logger.warning("Could not process watch state %s for stack" %
                               new_state)
        return actioned

    def create_watch_data(self, data):
        if not self.rule['MetricName'] in data:
            logger.warn('new data has incorrect metric:%s' %
                        (self.rule['MetricName']))
            raise AttributeError('MetricName %s missing' %
                                 self.rule['MetricName'])

        watch_data = {
            'data': data,
            'watch_rule_id': self.id
        }
        wd = db_api.watch_data_create(None, watch_data)
        logger.debug('new watch:%s data:%s' % (self.name, str(wd.data)))
        if self.rule['Statistic'] == 'SampleCount':
            self.run_rule()

    def set_watch_state(self, state):
        '''
        Temporarily set the watch state
        '''

        if state not in self.WATCH_STATES:
            raise AttributeError('Unknown watch state %s' % state)

        if state != self.state:
            if self.rule_action(state):
                logger.debug("Overriding state %s for watch %s with %s" %
                         (self.state, self.name, state))
            else:
                logger.warning("Unable to override state %s for watch %s" %
                         (self.state, self.name))
