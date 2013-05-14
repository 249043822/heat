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
from heat.openstack.common import log as logging
from heat.openstack.common.importutils import try_import

from heat.common import exception
from heat.engine import clients
from heat.engine import resource

volume_backups = try_import('cinderclient.v1.volume_backups')

logger = logging.getLogger(__name__)


class Volume(resource.Resource):

    properties_schema = {'AvailabilityZone': {'Type': 'String',
                                              'Required': True},
                         'Size': {'Type': 'Number'},
                         'SnapshotId': {'Type': 'String'},
                         'Tags': {'Type': 'List'}}

    _restore_property = 'SnapshotId'

    def _display_name(self):
        return self.physical_resource_name()

    def _display_description(self):
        return self.physical_resource_name()

    def _create_arguments(self):
        return {'size': self.properties['Size'],
                'availability_zone': self.properties['AvailabilityZone']}

    def handle_create(self):
        backup_id = self.properties.get(self._restore_property)
        cinder = self.cinder()
        if backup_id is not None:
            if volume_backups is None:
                raise exception.Error(
                    '%s not supported' % self._restore_property)
            vol_id = cinder.restores.restore(backup_id)['volume_id']

            vol = cinder.volumes.get(vol_id)
            vol.update(
                display_name=self._display_name(),
                display_description=self._display_description())
        else:
            vol = cinder.volumes.create(
                display_name=self._display_name(),
                display_description=self._display_description(),
                **self._create_arguments())

        while vol.status == 'creating':
            eventlet.sleep(1)
            vol.get()
        if vol.status == 'available':
            self.resource_id_set(vol.id)
        else:
            raise exception.Error(vol.status)

    def handle_update(self, json_snippet):
        return self.UPDATE_REPLACE

    if volume_backups is not None:
        def handle_snapshot(self):
            if self.resource_id is not None:
                # We use backups as snapshots are not independent of volumes
                backup = self.cinder().backups.create(self.resource_id)
                while backup.status == 'creating':
                    eventlet.sleep(1)
                    backup.get()
                if backup.status != 'available':
                    raise exception.Error(backup.status)
                self.handle_delete()

    def handle_delete(self):
        if self.resource_id is not None:
            try:
                vol = self.cinder().volumes.get(self.resource_id)

                if vol.status == 'in-use':
                    logger.warn('cant delete volume when in-use')
                    raise exception.Error("Volume in use")

                self.cinder().volumes.delete(self.resource_id)
            except clients.cinder_exceptions.NotFound:
                pass


class VolumeAttachment(resource.Resource):
    properties_schema = {'InstanceId': {'Type': 'String',
                                        'Required': True},
                         'VolumeId': {'Type': 'String',
                                      'Required': True},
                         'Device': {'Type': "String",
                                    'Required': True,
                                    'AllowedPattern': '/dev/vd[b-z]'}}

    def __init__(self, name, json_snippet, stack):
        super(VolumeAttachment, self).__init__(name, json_snippet, stack)

    def handle_create(self):
        server_id = self.properties['InstanceId']
        volume_id = self.properties['VolumeId']
        dev = self.properties['Device']
        inst = self.stack.clients.attach_volume_to_instance(server_id,
                                                            volume_id,
                                                            dev)
        self.resource_id_set(inst)

    def handle_update(self, json_snippet):
        return self.UPDATE_REPLACE

    def handle_delete(self):
        server_id = self.properties['InstanceId']
        volume_id = self.properties['VolumeId']
        self.stack.clients.detach_volume_from_instance(server_id, volume_id)


class CinderVolume(Volume):

    properties_schema = {'availability_zone': {'Type': 'String',
                                               'Required': True},
                         'size': {'Type': 'Number'},
                         'snapshot_id': {'Type': 'String'},
                         'backup_id': {'Type': 'String'},
                         'name': {'Type': 'String'},
                         'description': {'Type': 'String'},
                         'volume_type': {'Type': 'String'},
                         'metadata': {'Type': 'Map'},
                         'imageRef': {'Type': 'String'},
                         'source_volid': {'Type': 'String'}}

    _restore_property = 'backup_id'

    def _display_name(self):
        name = self.properties['name']
        if name:
            return name
        return super(CinderVolume, self)._display_name()

    def _display_description(self):
        return self.properties['description']

    def _create_arguments(self):
        arguments = {
            'size': self.properties['size'],
            'availability_zone': self.properties['availability_zone']
        }
        optionals = ['snapshot_id', 'volume_type', 'imageRef', 'source_volid',
                     'metadata']
        arguments.update((prop, self.properties[prop]) for prop in optionals
                         if self.properties[prop])
        return arguments


def resource_mapping():
    return {
        'AWS::EC2::Volume': Volume,
        'AWS::EC2::VolumeAttachment': VolumeAttachment,
        'OS::Cinder::Volume': CinderVolume,
    }
