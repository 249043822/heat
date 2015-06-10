# Copyright (c) 2014 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from oslo_log import log as logging
import oslo_messaging
from osprofiler import profiler
import six

from heat.common import context
from heat.common import exception
from heat.common.i18n import _LE
from heat.common.i18n import _LI
from heat.common import messaging as rpc_messaging
from heat.engine import dependencies
from heat.engine import resource
from heat.engine import sync_point
from heat.openstack.common import service
from heat.rpc import worker_client as rpc_client

LOG = logging.getLogger(__name__)


@profiler.trace_cls("rpc")
class WorkerService(service.Service):
    """
    This service is dedicated to handle internal messages to the 'worker'
    (a.k.a. 'converger') actor in convergence. Messages on this bus will
    use the 'cast' rather than 'call' method to anycast the message to
    an engine that will handle it asynchronously. It won't wait for
    or expect replies from these messages.
    """

    RPC_API_VERSION = '1.1'

    def __init__(self,
                 host,
                 topic,
                 engine_id,
                 thread_group_mgr):
        super(WorkerService, self).__init__()
        self.host = host
        self.topic = topic
        self.engine_id = engine_id
        self.thread_group_mgr = thread_group_mgr

        self._rpc_client = None
        self._rpc_server = None

    def start(self):
        target = oslo_messaging.Target(
            version=self.RPC_API_VERSION,
            server=self.host,
            topic=self.topic)
        LOG.info(_LI("Starting WorkerService ..."))

        self._rpc_server = rpc_messaging.get_rpc_server(target, self)
        self._rpc_server.start()

        self._rpc_client = rpc_client.WorkerClient()

        super(WorkerService, self).start()

    def stop(self):
        # Stop rpc connection at first for preventing new requests
        LOG.info(_LI("Stopping WorkerService ..."))
        try:
            self._rpc_server.stop()
            self._rpc_server.wait()
        except Exception as e:
            LOG.error(_LE("WorkerService is failed to stop, %s"), e)

        super(WorkerService, self).stop()

    @context.request_context
    def check_resource(self, cnxt, resource_id, current_traversal, data,
                       is_update):
        '''
        Process a node in the dependency graph.

        The node may be associated with either an update or a cleanup of its
        associated resource.
        '''
        try:
            rsrc, stack = resource.Resource.load(cnxt, resource_id, data)
        except exception.NotFound:
            return
        tmpl = stack.t

        if current_traversal != rsrc.stack.current_traversal:
            LOG.debug('[%s] Traversal cancelled; stopping.', current_traversal)
            return

        current_deps = ([tuple(i), (tuple(j) if j is not None else None)]
                        for i, j in rsrc.stack.current_deps['edges'])
        deps = dependencies.Dependencies(edges=current_deps)
        graph = deps.graph()

        if is_update:
            if (rsrc.replaced_by is not None and
                    rsrc.current_template_id != tmpl.id):
                return

            try:
                check_resource_update(rsrc, tmpl.id, data)
            except resource.UpdateReplace:
                # NOTE(sirushtim): Implemented by spec
                # convergence-resource-replacement.
                rsrc.make_replacement()
                return
            except resource.UpdateInProgress:
                return

            input_data = construct_input_data(rsrc)
        else:
            try:
                check_resource_cleanup(rsrc, tmpl.id, data)
            except resource.UpdateInProgress:
                return

        graph_key = (rsrc.id, is_update)
        if graph_key not in graph and rsrc.replaces is not None:
            # If we are a replacement, impersonate the replaced resource for
            # the purposes of calculating whether subsequent resources are
            # ready, since everybody has to work from the same version of the
            # graph. Our real resource ID is sent in the input_data, so the
            # dependencies will get updated to point to this resource in time
            # for the next traversal.
            graph_key = (rsrc.replaces, is_update)

        try:
            for req, fwd in deps.required_by(graph_key):
                propagate_check_resource(
                    cnxt, self._rpc_client, req, current_traversal,
                    set(graph[(req, fwd)]), graph_key,
                    input_data if fwd else rsrc.id, fwd)

            check_stack_complete(cnxt, rsrc.stack, current_traversal,
                                 rsrc.id, graph, is_update)
        except sync_point.SyncPointNotFound:
            # NOTE(sirushtim): Implemented by spec
            # convergence-concurrent-workflow
            pass


def construct_input_data(rsrc):
    attributes = rsrc.stack.get_dep_attrs(
        six.itervalues(rsrc.stack.resources),
        rsrc.stack.outputs,
        rsrc.name)
    resolved_attributes = {attr: rsrc.FnGetAtt(attr) for attr in attributes}
    input_data = {'id': rsrc.id,
                  'name': rsrc.name,
                  'physical_resource_id': rsrc.resource_id,
                  'attrs': resolved_attributes}
    return input_data


def check_stack_complete(cnxt, stack, current_traversal, sender, graph,
                         is_update):
    '''
    Mark the stack complete if the update is complete.

    Complete is currently in the sense that all desired resources are in
    service, not that superfluous ones have been cleaned up.
    '''
    roots = set(key for (key, fwd), node in graph.items()
                if not any(f for k, f in node.required_by()))

    if sender not in roots:
        return

    def mark_complete(stack_id, data):
        stack.mark_complete(current_traversal)

    sync_point.sync(cnxt, stack.id, current_traversal, is_update,
                    mark_complete, roots, {sender: None})


def propagate_check_resource(cnxt, rpc_client, next_res_id,
                             current_traversal, predecessors, sender,
                             sender_data, is_update):
    '''
    Trigger processing of a node if all of its dependencies are satisfied.
    '''
    def do_check(entity_id, data):
        rpc_client.check_resource(cnxt, entity_id, current_traversal,
                                  data, is_update)

    sync_point.sync(cnxt, next_res_id, current_traversal,
                    is_update, do_check, predecessors,
                    {sender: sender_data})


def check_resource_update(rsrc, template_id, data):
    '''
    Create or update the Resource if appropriate.
    '''
    input_data = {in_data.name: in_data for in_data in data.values()}

    if rsrc.resource_id is None:
        rsrc.create_convergence(template_id, input_data)
    else:
        rsrc.update_convergence(template_id, input_data)


def check_resource_cleanup(rsrc, template_id, data):
    '''
    Delete the Resource if appropriate.
    '''

    if rsrc.current_template_id != template_id:
        rsrc.delete_convergence(template_id, data)
