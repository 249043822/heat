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

import json
import logging

from heat.engine import resources

logger = logging.getLogger('heat.engine.parser')

class Stack:
    def __init__(self, stack_name, template, parms=None):

        self.t = template
        if self.t.has_key('Parameters'):
            self.parms = self.t['Parameters']
        else:
            self.parms = {}
        if self.t.has_key('Mappings'):
            self.maps = self.t['Mappings']
        else:
            self.maps = {}
        self.res = {}
        self.doc = None
        self.name = stack_name

        self.parms['AWS::Region'] = {"Description" : "AWS Regions", "Type" : "String", "Default" : "ap-southeast-1",
              "AllowedValues" : ["us-east-1","us-west-1","us-west-2","sa-east-1","eu-west-1","ap-southeast-1","ap-northeast-1"],
              "ConstraintDescription" : "must be a valid EC2 instance type." }

        if parms != None:
            self._apply_user_parameters(parms)
        self.creds = parms['KeyStoneCreds']

        self.resources = {}
        for r in self.t['Resources']:
            type = self.t['Resources'][r]['Type']
            if type == 'AWS::EC2::Instance':
                self.resources[r] = resources.Instance(r, self.t['Resources'][r], self)
            elif type == 'AWS::EC2::Volume':
                self.resources[r] = resources.Volume(r, self.t['Resources'][r], self)
            elif type == 'AWS::EC2::VolumeAttachment':
                self.resources[r] = resources.VolumeAttachment(r, self.t['Resources'][r], self)
            elif type == 'AWS::EC2::EIP':
                self.resources[r] = resources.ElasticIp(r, self.t['Resources'][r], self)
            elif type == 'AWS::EC2::EIPAssociation':
                self.resources[r] = resources.ElasticIpAssociation(r, self.t['Resources'][r], self)
            else:
                self.resources[r] = resources.GenericResource(r, self.t['Resources'][r], self)

            self.calulate_dependencies(self.t['Resources'][r], self.resources[r])

    def validate(self):
        '''
            If you are wondering where the actual validation is, me too.
            it is just not obvious how to respond to validation failures.
            http://docs.amazonwebservices.com/AWSCloudFormation/latest/APIReference/API_ValidateTemplate.html
        '''
        response = { 'ValidateTemplateResult': {
                    'Description': 'bla',
                    'Parameters': []
                    }
              }

        for p in self.parms:
            jp = {'member': {}}
            res = jp['member']
            res['NoEcho'] = 'false'
            res['ParameterKey'] = p
            if self.parms[p].has_key('Description'):
                res['Description'] = self.parms[p]['Description']
            else:
                res['Description'] = ''
            if self.parms[p].has_key('Default'):
                res['DefaultValue'] = self.parms[p]['Default']
            else:
                res['DefaultValue'] = ''
            response['ValidateTemplateResult']['Parameters'].append(res)
        return response

    def start(self):
        # start Volumes first.
        for r in self.t['Resources']:
            if self.t['Resources'][r]['Type'] == 'AWS::EC2::Volume':
                self.resources[r].start()

        for r in self.t['Resources']:
            #print 'calling start [stack->%s]' % (self.resources[r].name)
            self.resources[r].start()

    def calulate_dependencies(self, s, r):
        if isinstance(s, dict):
            for i in s:
                if i == 'Fn::GetAtt':
                    #print '%s seems to depend on %s' % (r.name, s[i][0])
                    #r.depends_on.append(s[i][0])
                    pass
                elif i == 'Ref':
                    #print '%s Refences %s' % (r.name, s[i])
                    r.depends_on.append(s[i])
                elif i == 'DependsOn' or i == 'Ref':
                    #print '%s DependsOn on %s' % (r.name, s[i])
                    r.depends_on.append(s[i])
                else:
                    self.calulate_dependencies(s[i], r)
        elif isinstance(s, list):
            for index, item in enumerate(s):
                self.calulate_dependencies(item, r)

    def _apply_user_parameter(self, key, value):
        logger.debug('appling user parameter %s=%s ' % (key, value))

        if not self.parms.has_key(key):
            self.parms[key] = {}
        self.parms[key]['Value'] = value

    def _apply_user_parameters(self, parms):
        for p in parms:
            if 'Parameters.member.' in p and 'ParameterKey' in p:
                s = p.split('.')
                try:
                    key_name = 'Parameters.member.%s.ParameterKey' % s[2]
                    value_name = 'Parameters.member.%s.ParameterValue' % s[2]
                    self._apply_user_parameter(parms[key_name], parms[value_name])
                except:
                    logger.error('Could not apply parameter %s' % p)

    def parameter_get(self, key):
        if self.parms[key] == None:
            logger.warn('Trying to reference parameter: %s, but it is empty' % key)
            return ''
        elif self.parms[key].has_key('Value'):
            return self.parms[key]['Value']
        elif self.parms[key].has_key('Default'):
            return self.parms[key]['Default']
        else:
            logger.warn('Trying to reference parameter: %s, but no Value or Default' % key)
            return ''

    def resolve_static_refs(self, s):
        '''
            looking for { "Ref": "str" }
        '''
        if isinstance(s, dict):
            for i in s:
                if i == 'Ref' and \
                      isinstance(s[i], (basestring, unicode)) and \
                      self.parms.has_key(s[i]):
                    return self.parameter_get(s[i])
                else:
                    s[i] = self.resolve_static_refs(s[i])
        elif isinstance(s, list):
            for index, item in enumerate(s):
                #print 'resolve_static_refs %d %s' % (index, item)
                s[index] = self.resolve_static_refs(item)
        return s

    def resolve_find_in_map(self, s):
        '''
            looking for { "Fn::FindInMap": ["str", "str"] }
        '''
        if isinstance(s, dict):
            for i in s:
                if i == 'Fn::FindInMap':
                    obj = self.maps
                    if isinstance(s[i], list):
                        #print 'map list: %s' % s[i]
                        for index, item in enumerate(s[i]):
                            if isinstance(item, dict):
                                item = self.resolve_find_in_map(item)
                                #print 'map item dict: %s' % (item)
                            else:
                                pass
                                #print 'map item str: %s' % (item)
                            obj = obj[item]
                    else:
                        obj = obj[s[i]]
                    return obj
                else:
                    s[i] = self.resolve_find_in_map(s[i])
        elif isinstance(s, list):
            for index, item in enumerate(s):
                s[index] = self.resolve_find_in_map(item)
        return s

    def resolve_attributes(self, s):
        '''
            looking for something like:
            {"Fn::GetAtt" : ["DBInstance", "Endpoint.Address"]}
        '''
        if isinstance(s, dict):
            for i in s:
                if i == 'Ref' and self.resources.has_key(s[i]):
                    return self.resources[s[i]].FnGetRefId()
                elif i == 'Fn::GetAtt':
                    resource_name = s[i][0]
                    key_name = s[i][1]
                    return self.resources[resource_name].FnGetAtt(key_name)
                else:
                    s[i] = self.resolve_attributes(s[i])
        elif isinstance(s, list):
            for index, item in enumerate(s):
                s[index] = self.resolve_attributes(item)
        return s

    def resolve_joins(self, s):
        '''
            looking for { "Fn::join": [] }
        '''
        if isinstance(s, dict):
            for i in s:
                if i == 'Fn::Join':
                    j = None
                    try:
                        j = s[i][0].join(s[i][1])
                    except:
                        print '*** could not join %s' % s[i]
                    return j
                else:
                    s[i] = self.resolve_joins(s[i])
        elif isinstance(s, list):
            for index, item in enumerate(s):
                s[index] = self.resolve_joins(item)
        return s

    def resolve_base64(self, s):
        '''
            looking for { "Fn::join": [] }
        '''
        if isinstance(s, dict):
            for i in s:
                if i == 'Fn::Base64':
                    return s[i]
                else:
                    s[i] = self.resolve_base64(s[i])
        elif isinstance(s, list):
            for index, item in enumerate(s):
                s[index] = self.resolve_base64(item)
        return s


