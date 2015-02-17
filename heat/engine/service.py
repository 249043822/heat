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

import collections
import functools
import json
import os
import socket
import warnings

import eventlet
from oslo.config import cfg
from oslo import messaging
from oslo.serialization import jsonutils
from oslo.utils import timeutils
from oslo.utils import uuidutils
from oslo_log import log as logging
from osprofiler import profiler
import requests
import six
import webob

from heat.common import context
from heat.common import exception
from heat.common.i18n import _
from heat.common.i18n import _LE
from heat.common.i18n import _LI
from heat.common.i18n import _LW
from heat.common import identifier
from heat.common import messaging as rpc_messaging
from heat.common import service_utils
from heat.db import api as db_api
from heat.engine import api
from heat.engine import attributes
from heat.engine import clients
from heat.engine import environment
from heat.engine import event as evt
from heat.engine import parameter_groups
from heat.engine import properties
from heat.engine import resources
from heat.engine import service_stack_watch
from heat.engine import stack as parser
from heat.engine import stack_lock
from heat.engine import template as templatem
from heat.engine import watchrule
from heat.openstack.common import service
from heat.openstack.common import threadgroup
from heat.rpc import api as rpc_api

cfg.CONF.import_opt('engine_life_check_timeout', 'heat.common.config')
cfg.CONF.import_opt('max_resources_per_stack', 'heat.common.config')
cfg.CONF.import_opt('max_stacks_per_tenant', 'heat.common.config')
cfg.CONF.import_opt('enable_stack_abandon', 'heat.common.config')
cfg.CONF.import_opt('enable_stack_adopt', 'heat.common.config')

LOG = logging.getLogger(__name__)


def request_context(func):
    @functools.wraps(func)
    def wrapped(self, ctx, *args, **kwargs):
        if ctx is not None and not isinstance(ctx, context.RequestContext):
            ctx = context.RequestContext.from_dict(ctx.to_dict())
        try:
            return func(self, ctx, *args, **kwargs)
        except exception.HeatException:
            raise messaging.rpc.dispatcher.ExpectedException()
    return wrapped


class ThreadGroupManager(object):

    def __init__(self):
        super(ThreadGroupManager, self).__init__()
        self.groups = {}
        self.events = collections.defaultdict(list)

        # Create dummy service task, because when there is nothing queued
        # on self.tg the process exits
        self.add_timer(cfg.CONF.periodic_interval, self._service_task)

    def _service_task(self):
        """
        This is a dummy task which gets queued on the service.Service
        threadgroup.  Without this service.Service sees nothing running
        i.e has nothing to wait() on, so the process exits..
        This could also be used to trigger periodic non-stack-specific
        housekeeping tasks
        """
        pass

    def _serialize_profile_info(self):
        prof = profiler.get()
        trace_info = None
        if prof:
            trace_info = {
                "hmac_key": prof.hmac_key,
                "base_id": prof.get_base_id(),
                "parent_id": prof.get_id()
            }
        return trace_info

    def _start_with_trace(self, trace, func, *args, **kwargs):
        if trace:
            profiler.init(**trace)
        return func(*args, **kwargs)

    def start(self, stack_id, func, *args, **kwargs):
        """
        Run the given method in a sub-thread.
        """
        if stack_id not in self.groups:
            self.groups[stack_id] = threadgroup.ThreadGroup()
        return self.groups[stack_id].add_thread(self._start_with_trace,
                                                self._serialize_profile_info(),
                                                func, *args, **kwargs)

    def start_with_lock(self, cnxt, stack, engine_id, func, *args, **kwargs):
        """
        Try to acquire a stack lock and, if successful, run the given
        method in a sub-thread.  Release the lock when the thread
        finishes.

        :param cnxt: RPC context
        :param stack: Stack to be operated on
        :type stack: heat.engine.parser.Stack
        :param engine_id: The UUID of the engine/worker acquiring the lock
        :param func: Callable to be invoked in sub-thread
        :type func: function or instancemethod
        :param args: Args to be passed to func
        :param kwargs: Keyword-args to be passed to func.
        """
        lock = stack_lock.StackLock(cnxt, stack, engine_id)
        with lock.thread_lock(stack.id):
            th = self.start_with_acquired_lock(stack, lock,
                                               func, *args, **kwargs)
            return th

    def start_with_acquired_lock(self, stack, lock, func, *args, **kwargs):
        """
        Run the given method in a sub-thread and release the provided lock
        when the thread finishes.

        :param stack: Stack to be operated on
        :type stack: heat.engine.parser.Stack
        :param lock: The acquired stack lock
        :type lock: heat.engine.stack_lock.StackLock
        :param func: Callable to be invoked in sub-thread
        :type func: function or instancemethod
        :param args: Args to be passed to func
        :param kwargs: Keyword-args to be passed to func

        """
        def release(gt, *args):
            """
            Callback function that will be passed to GreenThread.link().
            """
            lock.release(*args)

        th = self.start(stack.id, func, *args, **kwargs)
        th.link(release, stack.id)
        return th

    def add_timer(self, stack_id, func, *args, **kwargs):
        """
        Define a periodic task, to be run in a separate thread, in the stack
        threadgroups.  Periodicity is cfg.CONF.periodic_interval
        """
        if stack_id not in self.groups:
            self.groups[stack_id] = threadgroup.ThreadGroup()
        self.groups[stack_id].add_timer(cfg.CONF.periodic_interval,
                                        func, *args, **kwargs)

    def add_event(self, stack_id, event):
        self.events[stack_id].append(event)

    def remove_event(self, gt, stack_id, event):
        for e in self.events.pop(stack_id, []):
            if e is not event:
                self.add_event(stack_id, e)

    def stop_timers(self, stack_id):
        if stack_id in self.groups:
            self.groups[stack_id].stop_timers()

    def stop(self, stack_id, graceful=False):
        '''Stop any active threads on a stack.'''
        if stack_id in self.groups:
            self.events.pop(stack_id, None)
            threadgroup = self.groups.pop(stack_id)
            threads = threadgroup.threads[:]

            threadgroup.stop(graceful)
            threadgroup.wait()

            # Wait for link()ed functions (i.e. lock release)
            links_done = dict((th, False) for th in threads)

            def mark_done(gt, th):
                links_done[th] = True

            for th in threads:
                th.link(mark_done, th)
            while not all(links_done.values()):
                eventlet.sleep()

    def send(self, stack_id, message):
        for event in self.events.pop(stack_id, []):
            event.send(message)


@profiler.trace_cls("rpc")
class EngineListener(service.Service):
    '''
    Listen on an AMQP queue named for the engine.  Allows individual
    engines to communicate with each other for multi-engine support.
    '''

    ACTIONS = (STOP_STACK, SEND) = ('stop_stack', 'send')

    def __init__(self, host, engine_id, thread_group_mgr):
        super(EngineListener, self).__init__()
        self.thread_group_mgr = thread_group_mgr
        self.engine_id = engine_id
        self.host = host

    def start(self):
        super(EngineListener, self).start()
        self.target = messaging.Target(
            server=self.host, topic=self.engine_id)
        server = rpc_messaging.get_rpc_server(self.target, self)
        server.start()

    def listening(self, ctxt):
        '''
        Respond affirmatively to confirm that the engine performing the
        action is still alive.
        '''
        return True

    def stop_stack(self, ctxt, stack_identity):
        '''Stop any active threads on a stack.'''
        stack_id = stack_identity['stack_id']
        self.thread_group_mgr.stop(stack_id)

    def send(self, ctxt, stack_identity, message):
        stack_id = stack_identity['stack_id']
        self.thread_group_mgr.send(stack_id, message)


@profiler.trace_cls("rpc")
class EngineService(service.Service):
    """
    Manages the running instances from creation to destruction.
    All the methods in here are called from the RPC backend.  This is
    all done dynamically so if a call is made via RPC that does not
    have a corresponding method here, an exception will be thrown when
    it attempts to call into this class.  Arguments to these methods
    are also dynamically added and will be named as keyword arguments
    by the RPC caller.
    """

    RPC_API_VERSION = '1.5'

    def __init__(self, host, topic, manager=None):
        super(EngineService, self).__init__()
        resources.initialise()
        self.host = host
        self.topic = topic
        self.binary = 'heat-engine'
        self.hostname = socket.gethostname()

        # The following are initialized here, but assigned in start() which
        # happens after the fork when spawning multiple worker processes
        self.stack_watch = None
        self.listener = None
        self.engine_id = None
        self.thread_group_mgr = None
        self.target = None
        self.service_id = None
        self.manage_thread_grp = None
        self._rpc_server = None

        if cfg.CONF.instance_user:
            warnings.warn('The "instance_user" option in heat.conf is '
                          'deprecated and will be removed in the Juno '
                          'release.', DeprecationWarning)

        if cfg.CONF.trusts_delegated_roles:
            warnings.warn('The default value of "trusts_delegated_roles" '
                          'option in heat.conf is changed to [] in Kilo '
                          'and heat will delegate all roles of trustor. '
                          'Please keep the same if you do not want to '
                          'delegate subset roles when upgrading.',
                          Warning)

    def create_periodic_tasks(self):
        LOG.debug("Starting periodic watch tasks pid=%s" % os.getpid())
        # Note with multiple workers, the parent process hasn't called start()
        # so we need to create a ThreadGroupManager here for the periodic tasks
        if self.thread_group_mgr is None:
            self.thread_group_mgr = ThreadGroupManager()
        self.stack_watch = service_stack_watch.StackWatch(
            self.thread_group_mgr)

        # Create a periodic_watcher_task per-stack
        admin_context = context.get_admin_context()
        stacks = db_api.stack_get_all(admin_context, tenant_safe=False)
        for s in stacks:
            self.stack_watch.start_watch_task(s.id, admin_context)

    def start(self):
        self.engine_id = stack_lock.StackLock.generate_engine_id()
        self.thread_group_mgr = ThreadGroupManager()
        self.listener = EngineListener(self.host, self.engine_id,
                                       self.thread_group_mgr)
        LOG.debug("Starting listener for engine %s" % self.engine_id)
        self.listener.start()
        target = messaging.Target(
            version=self.RPC_API_VERSION, server=self.host,
            topic=self.topic)
        self.target = target
        self._rpc_server = rpc_messaging.get_rpc_server(target, self)
        self._rpc_server.start()
        self._client = rpc_messaging.get_rpc_client(
            version=self.RPC_API_VERSION)

        self.manage_thread_grp = threadgroup.ThreadGroup()
        self.manage_thread_grp.add_timer(cfg.CONF.periodic_interval,
                                         self.service_manage_report)

        super(EngineService, self).start()

    def _stop_rpc_server(self):
        # Stop rpc connection at first for preventing new requests
        LOG.debug("Attempting to stop engine service...")
        try:
            self._rpc_server.stop()
            self._rpc_server.wait()
            LOG.info(_LI("Engine service is stopped successfully"))
        except Exception as e:
            LOG.error(_LE("Failed to stop engine service, %s"), e)

    def stop(self):
        self._stop_rpc_server()

        # Wait for all active threads to be finished
        for stack_id in self.thread_group_mgr.groups.keys():
            # Ignore dummy service task
            if stack_id == cfg.CONF.periodic_interval:
                continue
            LOG.info(_LI("Waiting stack %s processing to be finished"),
                     stack_id)
            # Stop threads gracefully
            self.thread_group_mgr.stop(stack_id, True)
            LOG.info(_LI("Stack %s processing was finished"), stack_id)

        self.manage_thread_grp.stop()
        ctxt = context.get_admin_context()
        db_api.service_delete(ctxt, self.service_id)
        LOG.info(_LI('Service %s is deleted'), self.service_id)

        # Terminate the engine process
        LOG.info(_LI("All threads were gone, terminating engine"))
        super(EngineService, self).stop()

    @request_context
    def identify_stack(self, cnxt, stack_name):
        """
        The identify_stack method returns the full stack identifier for a
        single, live stack given the stack name.

        :param cnxt: RPC context.
        :param stack_name: Name or UUID of the stack to look up.
        """
        if uuidutils.is_uuid_like(stack_name):
            s = db_api.stack_get(cnxt, stack_name, show_deleted=True)
            # may be the name is in uuid format, so if get by id returns None,
            # we should get the info by name again
            if not s:
                s = db_api.stack_get_by_name(cnxt, stack_name)
        else:
            s = db_api.stack_get_by_name(cnxt, stack_name)
        if s:
            stack = parser.Stack.load(cnxt, stack=s)
            return dict(stack.identifier())
        else:
            raise exception.StackNotFound(stack_name=stack_name)

    def _get_stack(self, cnxt, stack_identity, show_deleted=False):
        identity = identifier.HeatIdentifier(**stack_identity)

        s = db_api.stack_get(cnxt, identity.stack_id,
                             show_deleted=show_deleted,
                             eager_load=True)

        if s is None:
            raise exception.StackNotFound(stack_name=identity.stack_name)

        if cnxt.tenant_id not in (identity.tenant, s.stack_user_project_id):
            # The DB API should not allow this, but sanity-check anyway..
            raise exception.InvalidTenant(target=identity.tenant,
                                          actual=cnxt.tenant_id)

        if identity.path or s.name != identity.stack_name:
            raise exception.StackNotFound(stack_name=identity.stack_name)

        return s

    @request_context
    def show_stack(self, cnxt, stack_identity):
        """
        Return detailed information about one or all stacks.

        :param cnxt: RPC context.
        :param stack_identity: Name of the stack you want to show, or None
            to show all
        """
        if stack_identity is not None:
            db_stack = self._get_stack(cnxt, stack_identity, show_deleted=True)
            stacks = [parser.Stack.load(cnxt, stack=db_stack)]
        else:
            stacks = parser.Stack.load_all(cnxt)

        return [api.format_stack(stack) for stack in stacks]

    def get_revision(self, cnxt):
        return cfg.CONF.revision['heat_revision']

    @request_context
    def list_stacks(self, cnxt, limit=None, marker=None, sort_keys=None,
                    sort_dir=None, filters=None, tenant_safe=True,
                    show_deleted=False, show_nested=False):
        """
        The list_stacks method returns attributes of all stacks.  It supports
        pagination (``limit`` and ``marker``), sorting (``sort_keys`` and
        ``sort_dir``) and filtering (``filters``) of the results.

        :param cnxt: RPC context
        :param limit: the number of stacks to list (integer or string)
        :param marker: the ID of the last item in the previous page
        :param sort_keys: an array of fields used to sort the list
        :param sort_dir: the direction of the sort ('asc' or 'desc')
        :param filters: a dict with attribute:value to filter the list
        :param tenant_safe: if true, scope the request by the current tenant
        :param show_deleted: if true, show soft-deleted stacks
        :param show_nested: if true, show nested stacks
        :returns: a list of formatted stacks
        """
        stacks = parser.Stack.load_all(cnxt, limit, marker, sort_keys,
                                       sort_dir, filters, tenant_safe,
                                       show_deleted, resolve_data=False,
                                       show_nested=show_nested)
        return [api.format_stack(stack) for stack in stacks]

    @request_context
    def count_stacks(self, cnxt, filters=None, tenant_safe=True,
                     show_deleted=False, show_nested=False):
        """
        Return the number of stacks that match the given filters
        :param cnxt: RPC context.
        :param filters: a dict of ATTR:VALUE to match against stacks
        :param tenant_safe: if true, scope the request by the current tenant
        :param show_deleted: if true, count will include the deleted stacks
        :param show_nested: if true, count will include nested stacks
        :returns: a integer representing the number of matched stacks
        """
        return db_api.stack_count_all(cnxt, filters=filters,
                                      tenant_safe=tenant_safe,
                                      show_deleted=show_deleted,
                                      show_nested=show_nested)

    def _validate_deferred_auth_context(self, cnxt, stack):
        if cfg.CONF.deferred_auth_method != 'password':
            return

        if not stack.requires_deferred_auth():
            return

        if cnxt.username is None:
            raise exception.MissingCredentialError(required='X-Auth-User')
        if cnxt.password is None:
            raise exception.MissingCredentialError(required='X-Auth-Key')

    def _validate_new_stack(self, cnxt, stack_name, parsed_template):
        try:
            parsed_template.validate()
        except Exception as ex:
            raise exception.StackValidationFailed(message=six.text_type(ex))

        if db_api.stack_get_by_name(cnxt, stack_name):
            raise exception.StackExists(stack_name=stack_name)

        tenant_limit = cfg.CONF.max_stacks_per_tenant
        if db_api.stack_count_all(cnxt) >= tenant_limit:
            message = _("You have reached the maximum stacks per tenant, %d."
                        " Please delete some stacks.") % tenant_limit
            raise exception.RequestLimitExceeded(message=message)

        num_resources = len(parsed_template[parsed_template.RESOURCES])
        if num_resources > cfg.CONF.max_resources_per_stack:
            message = exception.StackResourceLimitExceeded.msg_fmt
            raise exception.RequestLimitExceeded(message=message)

    def _parse_template_and_validate_stack(self, cnxt, stack_name, template,
                                           params, files, args, owner_id=None,
                                           nested_depth=0, user_creds_id=None,
                                           stack_user_project_id=None,
                                           convergence=False):
        # If it is stack-adopt, use parameters from adopt_stack_data
        common_params = api.extract_args(args)
        if (rpc_api.PARAM_ADOPT_STACK_DATA in common_params and
                not cfg.CONF.enable_stack_adopt):
            raise exception.NotSupported(feature='Stack Adopt')

        tmpl = templatem.Template(template, files=files)
        self._validate_new_stack(cnxt, stack_name, tmpl)

        if rpc_api.PARAM_ADOPT_STACK_DATA in common_params:
            # Override the params with values given with -P option
            new_params = common_params[rpc_api.PARAM_ADOPT_STACK_DATA][
                'environment'][rpc_api.STACK_PARAMETERS].copy()
            new_params.update(params.get(rpc_api.STACK_PARAMETERS, {}))
            params[rpc_api.STACK_PARAMETERS] = new_params

        env = environment.Environment(params)
        stack = parser.Stack(cnxt, stack_name, tmpl, env,
                             owner_id=owner_id,
                             nested_depth=nested_depth,
                             user_creds_id=user_creds_id,
                             stack_user_project_id=stack_user_project_id,
                             convergence=convergence,
                             **common_params)

        self._validate_deferred_auth_context(cnxt, stack)
        stack.validate()
        return stack

    @request_context
    def preview_stack(self, cnxt, stack_name, template, params, files, args):
        """
        Simulates a new stack using the provided template.

        Note that at this stage the template has already been fetched from the
        heat-api process if using a template-url.

        :param cnxt: RPC context.
        :param stack_name: Name of the stack you want to create.
        :param template: Template of stack you want to create.
        :param params: Stack Input Params
        :param files: Files referenced from the template
        :param args: Request parameters/args passed from API
        """

        LOG.info(_LI('previewing stack %s'), stack_name)

        conv_eng = cfg.CONF.convergence_engine
        if conv_eng:
            raise exception.NotSupported(feature=_('Convergence engine'))

        stack = self._parse_template_and_validate_stack(cnxt,
                                                        stack_name,
                                                        template,
                                                        params,
                                                        files,
                                                        args,
                                                        convergence=conv_eng)

        return api.format_stack_preview(stack)

    @request_context
    def create_stack(self, cnxt, stack_name, template, params, files, args,
                     owner_id=None, nested_depth=0, user_creds_id=None,
                     stack_user_project_id=None):
        """
        The create_stack method creates a new stack using the template
        provided.
        Note that at this stage the template has already been fetched from the
        heat-api process if using a template-url.

        :param cnxt: RPC context.
        :param stack_name: Name of the stack you want to create.
        :param template: Template of stack you want to create.
        :param params: Stack Input Params
        :param files: Files referenced from the template
        :param args: Request parameters/args passed from API
        :param owner_id: parent stack ID for nested stacks, only expected when
                         called from another heat-engine (not a user option)
        :param nested_depth: the nested depth for nested stacks, only expected
                         when called from another heat-engine
        :param user_creds_id: the parent user_creds record for nested stacks
        :param stack_user_project_id: the parent stack_user_project_id for
                         nested stacks
        """
        LOG.info(_LI('Creating stack %s'), stack_name)

        def _stack_create(stack):

            if not stack.stack_user_project_id:
                try:
                    stack.create_stack_user_project_id()
                except exception.AuthorizationFailure as ex:
                    stack.state_set(stack.action, stack.FAILED,
                                    six.text_type(ex))

            # Create/Adopt a stack, and create the periodic task if successful
            if stack.adopt_stack_data:
                stack.adopt()
            elif stack.status != stack.FAILED:
                stack.create()

            if (stack.action in (stack.CREATE, stack.ADOPT)
                    and stack.status == stack.COMPLETE):
                if self.stack_watch:
                    # Schedule a periodic watcher task for this stack
                    self.stack_watch.start_watch_task(stack.id, cnxt)
            else:
                LOG.info(_LI("Stack create failed, status %s"), stack.status)

        convergence = cfg.CONF.convergence_engine
        if convergence:
            raise exception.NotSupported(feature=_('Convergence engine'))

        stack = self._parse_template_and_validate_stack(
            cnxt, stack_name, template, params, files, args, owner_id,
            nested_depth, user_creds_id, stack_user_project_id, convergence)

        stack.store()

        self.thread_group_mgr.start_with_lock(cnxt, stack, self.engine_id,
                                              _stack_create, stack)

        return dict(stack.identifier())

    @request_context
    def update_stack(self, cnxt, stack_identity, template, params,
                     files, args):
        """
        The update_stack method updates an existing stack based on the
        provided template and parameters.
        Note that at this stage the template has already been fetched from the
        heat-api process if using a template-url.

        :param cnxt: RPC context.
        :param stack_identity: Name of the stack you want to create.
        :param template: Template of stack you want to create.
        :param params: Stack Input Params
        :param files: Files referenced from the template
        :param args: Request parameters/args passed from API
        """
        # Get the database representation of the existing stack
        db_stack = self._get_stack(cnxt, stack_identity)
        LOG.info(_LI('Updating stack %s'), db_stack.name)

        current_stack = parser.Stack.load(cnxt, stack=db_stack)

        if current_stack.action == current_stack.SUSPEND:
            msg = _('Updating a stack when it is suspended')
            raise exception.NotSupported(feature=msg)

        if current_stack.action == current_stack.DELETE:
            msg = _('Updating a stack when it is deleting')
            raise exception.NotSupported(feature=msg)

        # Now parse the template and any parameters for the updated
        # stack definition.
        tmpl = templatem.Template(template, files=files)
        if len(tmpl[tmpl.RESOURCES]) > cfg.CONF.max_resources_per_stack:
            raise exception.RequestLimitExceeded(
                message=exception.StackResourceLimitExceeded.msg_fmt)
        stack_name = current_stack.name
        convergence = current_stack.convergence
        common_params = api.extract_args(args)
        common_params.setdefault(rpc_api.PARAM_TIMEOUT,
                                 current_stack.timeout_mins)
        common_params.setdefault(rpc_api.PARAM_DISABLE_ROLLBACK,
                                 current_stack.disable_rollback)
        env = environment.Environment(params)
        if args.get(rpc_api.PARAM_EXISTING, None):
            env.patch_previous_parameters(
                current_stack.env,
                args.get(rpc_api.PARAM_CLEAR_PARAMETERS, []))
        updated_stack = parser.Stack(cnxt, stack_name, tmpl,
                                     env, convergence=convergence,
                                     **common_params)
        updated_stack.parameters.set_stack_id(current_stack.identifier())

        self._validate_deferred_auth_context(cnxt, updated_stack)
        updated_stack.validate()

        event = eventlet.event.Event()
        th = self.thread_group_mgr.start_with_lock(cnxt, current_stack,
                                                   self.engine_id,
                                                   current_stack.update,
                                                   updated_stack,
                                                   event=event)
        th.link(self.thread_group_mgr.remove_event, current_stack.id, event)
        self.thread_group_mgr.add_event(current_stack.id, event)
        return dict(current_stack.identifier())

    @request_context
    def stack_cancel_update(self, cnxt, stack_identity):
        """Cancel currently running stack update.

        :param cnxt: RPC context.
        :param stack_identity: Name of the stack for which to cancel update.
        """
        # Get the database representation of the existing stack
        db_stack = self._get_stack(cnxt, stack_identity)

        current_stack = parser.Stack.load(cnxt, stack=db_stack)
        if current_stack.state != (current_stack.UPDATE,
                                   current_stack.IN_PROGRESS):
            msg = _("Cancelling update when stack is %s"
                    ) % str(current_stack.state)
            raise exception.NotSupported(feature=msg)
        LOG.info(_LI('Starting cancel of updating stack %s') % db_stack.name)
        # stop the running update and take the lock
        # as we cancel only running update, the acquire_result is
        # always some engine_id, not None
        lock = stack_lock.StackLock(cnxt, current_stack,
                                    self.engine_id)
        engine_id = lock.try_acquire()
        # Current engine has the lock
        if engine_id == self.engine_id:
            self.thread_group_mgr.send(current_stack.id, 'cancel')

        # Another active engine has the lock
        elif stack_lock.StackLock.engine_alive(cnxt, engine_id):
            cancel_result = self._remote_call(
                cnxt, engine_id, self.listener.SEND,
                stack_identity, rpc_api.THREAD_CANCEL)
            if cancel_result is None:
                LOG.debug("Successfully sent %(msg)s message "
                          "to remote task on engine %(eng)s" % {
                              'eng': engine_id, 'msg': 'cancel'})
            else:
                raise exception.EventSendFailed(stack_name=current_stack.name,
                                                engine_id=engine_id)

    @request_context
    def validate_template(self, cnxt, template, params=None):
        """
        The validate_template method uses the stack parser to check
        the validity of a template.

        :param cnxt: RPC context.
        :param template: Template of stack you want to create.
        :param params: Stack Input Params
        """
        LOG.info(_LI('validate_template'))
        if template is None:
            msg = _("No Template provided.")
            return webob.exc.HTTPBadRequest(explanation=msg)

        tmpl = templatem.Template(template)

        # validate overall template
        try:
            tmpl.validate()
        except Exception as ex:
            return {'Error': six.text_type(ex)}

        # validate resource classes
        tmpl_resources = tmpl[tmpl.RESOURCES]

        env = environment.Environment(params)

        for res in tmpl_resources.values():
            ResourceClass = env.get_class(res['Type'])
            if ResourceClass == resources.template_resource.TemplateResource:
                # we can't validate a TemplateResource unless we instantiate
                # it as we need to download the template and convert the
                # parameters into properties_schema.
                continue

            props = properties.Properties(ResourceClass.properties_schema,
                                          res.get('Properties', {}),
                                          context=cnxt)
            deletion_policy = res.get('DeletionPolicy', 'Delete')
            try:
                ResourceClass.validate_deletion_policy(deletion_policy)
                props.validate(with_value=False)
            except Exception as ex:
                return {'Error': six.text_type(ex)}

        # validate parameters
        tmpl_params = tmpl.parameters(None, user_params=env.params)
        tmpl_params.validate(validate_value=False, context=cnxt)
        is_real_param = lambda p: p.name not in tmpl_params.PSEUDO_PARAMETERS
        params = tmpl_params.map(api.format_validate_parameter, is_real_param)
        param_groups = parameter_groups.ParameterGroups(tmpl)

        result = {
            'Description': tmpl.get('Description', ''),
            'Parameters': params,
        }

        if param_groups.parameter_groups:
            result['ParameterGroups'] = param_groups.parameter_groups

        return result

    @request_context
    def authenticated_to_backend(self, cnxt):
        """
        Verify that the credentials in the RPC context are valid for the
        current cloud backend.
        """
        return clients.Clients(cnxt).authenticated()

    @request_context
    def get_template(self, cnxt, stack_identity):
        """
        Get the template.

        :param cnxt: RPC context.
        :param stack_identity: Name of the stack you want to see.
        """
        s = self._get_stack(cnxt, stack_identity, show_deleted=True)
        if s:
            return s.raw_template.template
        return None

    def _remote_call(self, cnxt, lock_engine_id, call, *args, **kwargs):
        timeout = cfg.CONF.engine_life_check_timeout
        self.cctxt = self._client.prepare(
            version='1.0',
            timeout=timeout,
            topic=lock_engine_id)
        try:
            self.cctxt.call(cnxt, call, *args, **kwargs)
        except messaging.MessagingTimeout:
            return False

    @request_context
    def delete_stack(self, cnxt, stack_identity):
        """
        The delete_stack method deletes a given stack.

        :param cnxt: RPC context.
        :param stack_identity: Name of the stack you want to delete.
        """

        st = self._get_stack(cnxt, stack_identity)
        LOG.info(_LI('Deleting stack %s'), st.name)
        stack = parser.Stack.load(cnxt, stack=st)

        lock = stack_lock.StackLock(cnxt, stack, self.engine_id)
        with lock.try_thread_lock(stack.id) as acquire_result:

            # Successfully acquired lock
            if acquire_result is None:
                self.thread_group_mgr.stop_timers(stack.id)
                self.thread_group_mgr.start_with_acquired_lock(stack, lock,
                                                               stack.delete)
                return

        # Current engine has the lock
        if acquire_result == self.engine_id:
            # give threads which are almost complete an opportunity to
            # finish naturally before force stopping them
            eventlet.sleep(0.2)
            self.thread_group_mgr.stop(stack.id)

        # Another active engine has the lock
        elif stack_lock.StackLock.engine_alive(cnxt, acquire_result):
            stop_result = self._remote_call(
                cnxt, acquire_result, self.listener.STOP_STACK,
                stack_identity=stack_identity)
            if stop_result is None:
                LOG.debug("Successfully stopped remote task on engine %s"
                          % acquire_result)
            else:
                raise exception.StopActionFailed(stack_name=stack.name,
                                                 engine_id=acquire_result)

        # There may be additional resources that we don't know about
        # if an update was in-progress when the stack was stopped, so
        # reload the stack from the database.
        st = self._get_stack(cnxt, stack_identity)
        stack = parser.Stack.load(cnxt, stack=st)

        self.thread_group_mgr.start_with_lock(cnxt, stack, self.engine_id,
                                              stack.delete)
        return None

    @request_context
    def abandon_stack(self, cnxt, stack_identity):
        """
        The abandon_stack method abandons a given stack.
        :param cnxt: RPC context.
        :param stack_identity: Name of the stack you want to abandon.
        """
        if not cfg.CONF.enable_stack_abandon:
            raise exception.NotSupported(feature='Stack Abandon')

        st = self._get_stack(cnxt, stack_identity)
        LOG.info(_LI('abandoning stack %s'), st.name)
        stack = parser.Stack.load(cnxt, stack=st)
        lock = stack_lock.StackLock(cnxt, stack, self.engine_id)
        with lock.thread_lock(stack.id):
            # Get stack details before deleting it.
            stack_info = stack.prepare_abandon()
            self.thread_group_mgr.start_with_acquired_lock(stack,
                                                           lock,
                                                           stack.delete,
                                                           abandon=True)
            return stack_info

    def list_resource_types(self, cnxt, support_status=None):
        """
        Get a list of supported resource types.

        :param cnxt: RPC context.
        """
        return resources.global_env().get_types(support_status)

    def resource_schema(self, cnxt, type_name):
        """
        Return the schema of the specified type.

        :param cnxt: RPC context.
        :param type_name: Name of the resource type to obtain the schema of.
        """
        try:
            resource_class = resources.global_env().get_class(type_name)
        except exception.StackValidationFailed:
            raise exception.ResourceTypeNotFound(type_name=type_name)
        except exception.NotFound as ex:
            raise exception.StackValidationFailed(message=ex.message)

        def properties_schema():
            for name, schema_dict in resource_class.properties_schema.items():
                schema = properties.Schema.from_legacy(schema_dict)
                if schema.implemented:
                    yield name, dict(schema)

        def attributes_schema():
            for name, schema_data in resource_class.attributes_schema.items():
                schema = attributes.Schema.from_attribute(schema_data)
                yield name, {schema.DESCRIPTION: schema.description}

        return {
            rpc_api.RES_SCHEMA_RES_TYPE: type_name,
            rpc_api.RES_SCHEMA_PROPERTIES: dict(properties_schema()),
            rpc_api.RES_SCHEMA_ATTRIBUTES: dict(attributes_schema()),
        }

    def generate_template(self, cnxt, type_name):
        """
        Generate a template based on the specified type.

        :param cnxt: RPC context.
        :param type_name: Name of the resource type to generate a template for.
        """
        try:
            return resources.global_env().get_class(
                type_name).resource_to_template(type_name)
        except exception.StackValidationFailed:
            raise exception.ResourceTypeNotFound(type_name=type_name)
        except exception.NotFound as ex:
            raise exception.StackValidationFailed(message=ex.message)

    @request_context
    def list_events(self, cnxt, stack_identity, filters=None, limit=None,
                    marker=None, sort_keys=None, sort_dir=None):
        """
        The list_events method lists all events associated with a given stack.
        It supports pagination (``limit`` and ``marker``),
        sorting (``sort_keys`` and ``sort_dir``) and filtering(filters)
        of the results.

        :param cnxt: RPC context.
        :param stack_identity: Name of the stack you want to get events for
        :param filters: a dict with attribute:value to filter the list
        :param limit: the number of events to list (integer or string)
        :param marker: the ID of the last event in the previous page
        :param sort_keys: an array of fields used to sort the list
        :param sort_dir: the direction of the sort ('asc' or 'desc').
        """

        if stack_identity is not None:
            st = self._get_stack(cnxt, stack_identity, show_deleted=True)

            events = db_api.event_get_all_by_stack(cnxt, st.id, limit=limit,
                                                   marker=marker,
                                                   sort_keys=sort_keys,
                                                   sort_dir=sort_dir,
                                                   filters=filters)
        else:
            events = db_api.event_get_all_by_tenant(cnxt, limit=limit,
                                                    marker=marker,
                                                    sort_keys=sort_keys,
                                                    sort_dir=sort_dir,
                                                    filters=filters)

        stacks = {}

        def get_stack(stack_id):
            if stack_id not in stacks:
                stacks[stack_id] = parser.Stack.load(cnxt, stack_id)
            return stacks[stack_id]

        return [api.format_event(evt.Event.load(cnxt,
                                                e.id, e,
                                                get_stack(e.stack_id)))
                for e in events]

    def _authorize_stack_user(self, cnxt, stack, resource_name):
        '''
        Filter access to describe_stack_resource for stack in-instance users
        - The user must map to a User resource defined in the requested stack
        - The user resource must validate OK against any Policy specified
        '''
        # first check whether access is allowed by context user_id
        if stack.access_allowed(cnxt.user_id, resource_name):
            return True

        # fall back to looking for EC2 credentials in the context
        try:
            ec2_creds = json.loads(cnxt.aws_creds).get('ec2Credentials')
        except (TypeError, AttributeError):
            ec2_creds = None

        if not ec2_creds:
            return False

        access_key = ec2_creds.get('access')
        return stack.access_allowed(access_key, resource_name)

    def _verify_stack_resource(self, stack, resource_name):
        if resource_name not in stack:
            raise exception.ResourceNotFound(resource_name=resource_name,
                                             stack_name=stack.name)

        resource = stack[resource_name]
        if resource.id is None:
            raise exception.ResourceNotAvailable(resource_name=resource_name)

    @request_context
    def describe_stack_resource(self, cnxt, stack_identity, resource_name,
                                with_attr=None):
        s = self._get_stack(cnxt, stack_identity)
        stack = parser.Stack.load(cnxt, stack=s)

        if cfg.CONF.heat_stack_user_role in cnxt.roles:
            if not self._authorize_stack_user(cnxt, stack, resource_name):
                LOG.warn(_LW("Access denied to resource %s"), resource_name)
                raise exception.Forbidden()

        if resource_name not in stack:
            raise exception.ResourceNotFound(resource_name=resource_name,
                                             stack_name=stack.name)

        return api.format_stack_resource(stack[resource_name],
                                         with_attr=with_attr)

    @request_context
    def resource_signal(self, cnxt, stack_identity, resource_name, details,
                        sync_call=False):
        '''
        :param sync_call: indicates whether a synchronized call behavior is
                          expected. This is reserved for CFN WaitCondition
                          implementation.
        '''

        def _resource_signal(rsrc, details):
            stack = rsrc.stack
            LOG.debug("signaling resource %s:%s" % (stack.name, rsrc.name))
            rsrc.signal(details)

            # Refresh the metadata for all other resources, since signals can
            # update metadata which is used by other resources, e.g
            # when signalling a WaitConditionHandle resource, and other
            # resources may refer to WaitCondition Fn::GetAtt Data
            for r in stack.dependencies:
                if r.name != rsrc.name and r.id is not None:
                    r.metadata_update()

        s = self._get_stack(cnxt, stack_identity)

        # This is not "nice" converting to the stored context here,
        # but this happens because the keystone user associated with the
        # signal doesn't have permission to read the secret key of
        # the user associated with the cfn-credentials file
        stack = parser.Stack.load(cnxt, stack=s, use_stored_context=True)
        self._verify_stack_resource(stack, resource_name)

        rsrc = stack[resource_name]
        if callable(rsrc.signal):
            if sync_call:
                _resource_signal(rsrc, details)
                return rsrc.metadata_get()
            else:
                self.thread_group_mgr.start(stack.id, _resource_signal,
                                            rsrc, details)

    @request_context
    def find_physical_resource(self, cnxt, physical_resource_id):
        """
        Return an identifier for the resource with the specified physical
        resource ID.

        :param cnxt: RPC context.
        :param physical_resource_id: The physical resource ID to look up.
        """
        rs = db_api.resource_get_by_physical_resource_id(cnxt,
                                                         physical_resource_id)
        if not rs:
            raise exception.PhysicalResourceNotFound(
                resource_id=physical_resource_id)

        stack = parser.Stack.load(cnxt, stack_id=rs.stack.id)
        resource = stack[rs.name]

        return dict(resource.identifier())

    @request_context
    def describe_stack_resources(self, cnxt, stack_identity, resource_name):
        s = self._get_stack(cnxt, stack_identity)

        stack = parser.Stack.load(cnxt, stack=s)

        return [api.format_stack_resource(resource)
                for name, resource in six.iteritems(stack)
                if resource_name is None or name == resource_name]

    @request_context
    def list_stack_resources(self, cnxt, stack_identity, nested_depth=0):
        s = self._get_stack(cnxt, stack_identity, show_deleted=True)
        stack = parser.Stack.load(cnxt, stack=s)
        depth = min(nested_depth, cfg.CONF.max_nested_stack_depth)

        return [api.format_stack_resource(resource, detail=False)
                for resource in stack.iter_resources(depth)]

    @request_context
    def stack_suspend(self, cnxt, stack_identity):
        '''
        Handle request to perform suspend action on a stack
        '''
        def _stack_suspend(stack):
            LOG.debug("suspending stack %s" % stack.name)
            stack.suspend()

        s = self._get_stack(cnxt, stack_identity)

        stack = parser.Stack.load(cnxt, stack=s)
        self.thread_group_mgr.start_with_lock(cnxt, stack, self.engine_id,
                                              _stack_suspend, stack)

    @request_context
    def stack_resume(self, cnxt, stack_identity):
        '''
        Handle request to perform a resume action on a stack
        '''
        def _stack_resume(stack):
            LOG.debug("resuming stack %s" % stack.name)
            stack.resume()

        s = self._get_stack(cnxt, stack_identity)

        stack = parser.Stack.load(cnxt, stack=s)
        self.thread_group_mgr.start_with_lock(cnxt, stack, self.engine_id,
                                              _stack_resume, stack)

    @request_context
    def stack_snapshot(self, cnxt, stack_identity, name):
        def _stack_snapshot(stack, snapshot):
            LOG.debug("snapshotting stack %s" % stack.name)
            stack.snapshot()
            data = stack.prepare_abandon()
            db_api.snapshot_update(
                cnxt,
                snapshot.id,
                {'data': data, 'status': stack.status,
                 'status_reason': stack.status_reason})

        s = self._get_stack(cnxt, stack_identity)

        stack = parser.Stack.load(cnxt, stack=s)
        if stack.status == stack.IN_PROGRESS:
            LOG.info(_LI('%(stack)s is in state %(action)s_IN_PROGRESS, '
                         'snapshot is not permitted.'), {
                             'stack': six.text_type(stack),
                             'action': stack.action})
            raise exception.ActionInProgress(stack_name=stack.name,
                                             action=stack.action)

        lock = stack_lock.StackLock(cnxt, stack, self.engine_id)

        with lock.thread_lock(stack.id):
            snapshot = db_api.snapshot_create(cnxt, {
                'tenant': cnxt.tenant_id,
                'name': name,
                'stack_id': stack.id,
                'status': 'IN_PROGRESS'})
            self.thread_group_mgr.start_with_acquired_lock(
                stack, lock, _stack_snapshot, stack, snapshot)
            return api.format_snapshot(snapshot)

    @request_context
    def show_snapshot(self, cnxt, stack_identity, snapshot_id):
        snapshot = db_api.snapshot_get(cnxt, snapshot_id)
        return api.format_snapshot(snapshot)

    @request_context
    def delete_snapshot(self, cnxt, stack_identity, snapshot_id):
        def _delete_snapshot(stack, snapshot):
            stack.delete_snapshot(snapshot)
            db_api.snapshot_delete(cnxt, snapshot_id)

        s = self._get_stack(cnxt, stack_identity)
        stack = parser.Stack.load(cnxt, stack=s)
        snapshot = db_api.snapshot_get(cnxt, snapshot_id)
        self.thread_group_mgr.start(
            stack.id, _delete_snapshot, stack, snapshot)

    @request_context
    def stack_check(self, cnxt, stack_identity):
        '''
        Handle request to perform a check action on a stack
        '''
        s = self._get_stack(cnxt, stack_identity)
        stack = parser.Stack.load(cnxt, stack=s)
        LOG.info(_LI("Checking stack %s"), stack.name)

        self.thread_group_mgr.start_with_lock(cnxt, stack, self.engine_id,
                                              stack.check)

    @request_context
    def stack_restore(self, cnxt, stack_identity, snapshot_id):
        def _stack_restore(stack, snapshot):
            LOG.debug("restoring stack %s" % stack.name)
            stack.restore(snapshot)

        s = self._get_stack(cnxt, stack_identity)
        snapshot = db_api.snapshot_get(cnxt, snapshot_id)

        stack = parser.Stack.load(cnxt, stack=s)

        self.thread_group_mgr.start_with_lock(cnxt, stack, self.engine_id,
                                              _stack_restore, stack, snapshot)

    @request_context
    def stack_list_snapshots(self, cnxt, stack_identity):
        s = self._get_stack(cnxt, stack_identity)
        data = db_api.snapshot_get_all(cnxt, s.id)
        return [api.format_snapshot(snapshot) for snapshot in data]

    @request_context
    def metadata_update(self, cnxt, stack_identity,
                        resource_name, metadata):
        """
        Update the metadata for the given resource.
        DEPRECATED: Use resource_signal instead
        """
        warnings.warn('metadata_update is deprecated, '
                      'use resource_signal instead',
                      DeprecationWarning)

        s = self._get_stack(cnxt, stack_identity)

        stack = parser.Stack.load(cnxt, stack=s)
        if resource_name not in stack:
            raise exception.ResourceNotFound(resource_name=resource_name,
                                             stack_name=stack.name)

        resource = stack[resource_name]
        resource.metadata_update(new_metadata=metadata)

        # This is not "nice" converting to the stored context here,
        # but this happens because the keystone user associated with the
        # WaitCondition doesn't have permission to read the secret key of
        # the user associated with the cfn-credentials file
        refresh_stack = parser.Stack.load(cnxt, stack=s,
                                          use_stored_context=True)

        # Refresh the metadata for all other resources, since we expect
        # resource_name to be a WaitCondition resource, and other
        # resources may refer to WaitCondition Fn::GetAtt Data, which
        # is updated here.
        for res in refresh_stack.dependencies:
            if res.name != resource_name and res.id is not None:
                res.metadata_update()

        return resource.metadata_get()

    @request_context
    def create_watch_data(self, cnxt, watch_name, stats_data):
        '''
        This could be used by CloudWatch and WaitConditions
        and treat HA service events like any other CloudWatch.
        '''
        def get_matching_watches():
            if watch_name:
                yield watchrule.WatchRule.load(cnxt, watch_name)
            else:
                for wr in db_api.watch_rule_get_all(cnxt):
                    if watchrule.rule_can_use_sample(wr, stats_data):
                        yield watchrule.WatchRule.load(cnxt, watch=wr)

        rule_run = False
        for rule in get_matching_watches():
            rule.create_watch_data(stats_data)
            rule_run = True

        if not rule_run:
            if watch_name is None:
                watch_name = 'Unknown'
            raise exception.WatchRuleNotFound(watch_name=watch_name)

        return stats_data

    @request_context
    def show_watch(self, cnxt, watch_name):
        """
        The show_watch method returns the attributes of one watch/alarm

        :param cnxt: RPC context.
        :param watch_name: Name of the watch you want to see, or None to see
            all
        """
        if watch_name:
            wrn = [watch_name]
        else:
            try:
                wrn = [w.name for w in db_api.watch_rule_get_all(cnxt)]
            except Exception as ex:
                LOG.warn(_LW('show_watch (all) db error %s'), ex)
                return

        wrs = [watchrule.WatchRule.load(cnxt, w) for w in wrn]
        result = [api.format_watch(w) for w in wrs]
        return result

    @request_context
    def show_watch_metric(self, cnxt, metric_namespace=None, metric_name=None):
        """
        The show_watch method returns the datapoints for a metric

        :param cnxt: RPC context.
        :param metric_namespace: Name of the namespace you want to see, or None
            to see all
        :param metric_name: Name of the metric you want to see, or None to see
            all
        """

        # DB API and schema does not yet allow us to easily query by
        # namespace/metric, but we will want this at some point
        # for now, the API can query all metric data and filter locally
        if metric_namespace is not None or metric_name is not None:
            LOG.error(_LE("Filtering by namespace/metric not yet supported"))
            return

        try:
            wds = db_api.watch_data_get_all(cnxt)
        except Exception as ex:
            LOG.warn(_LW('show_metric (all) db error %s'), ex)
            return

        result = [api.format_watch_data(w) for w in wds]
        return result

    @request_context
    def set_watch_state(self, cnxt, watch_name, state):
        """
        Temporarily set the state of a given watch

        :param cnxt: RPC context.
        :param watch_name: Name of the watch
        :param state: State (must be one defined in WatchRule class
        """
        wr = watchrule.WatchRule.load(cnxt, watch_name)
        if wr.state == rpc_api.WATCH_STATE_CEILOMETER_CONTROLLED:
            return
        actions = wr.set_watch_state(state)
        for action in actions:
            self.thread_group_mgr.start(wr.stack_id, action)

        # Return the watch with the state overridden to indicate success
        # We do not update the timestamps as we are not modifying the DB
        result = api.format_watch(wr)
        result[rpc_api.WATCH_STATE_VALUE] = state
        return result

    @request_context
    def show_software_config(self, cnxt, config_id):
        sc = db_api.software_config_get(cnxt, config_id)
        return api.format_software_config(sc)

    @request_context
    def create_software_config(self, cnxt, group, name, config,
                               inputs, outputs, options):

        sc = db_api.software_config_create(cnxt, {
            'group': group,
            'name': name,
            'config': {
                'inputs': inputs,
                'outputs': outputs,
                'options': options,
                'config': config
            },
            'tenant': cnxt.tenant_id})
        return api.format_software_config(sc)

    @request_context
    def delete_software_config(self, cnxt, config_id):
        db_api.software_config_delete(cnxt, config_id)

    @request_context
    def list_software_deployments(self, cnxt, server_id):
        all_sd = db_api.software_deployment_get_all(cnxt, server_id)
        result = [api.format_software_deployment(sd) for sd in all_sd]
        return result

    @request_context
    def metadata_software_deployments(self, cnxt, server_id):
        if not server_id:
            raise ValueError(_('server_id must be specified'))
        all_sd = db_api.software_deployment_get_all(cnxt, server_id)
        # sort the configs by config name, to give the list of metadata a
        # deterministic and controllable order.
        all_sd_s = sorted(all_sd, key=lambda sd: sd.config.name)
        result = [api.format_software_config(sd.config) for sd in all_sd_s]
        return result

    def _push_metadata_software_deployments(self, cnxt, server_id):
        rs = db_api.resource_get_by_physical_resource_id(cnxt, server_id)
        if not rs:
            return
        deployments = self.metadata_software_deployments(cnxt, server_id)
        md = rs.rsrc_metadata or {}
        md['deployments'] = deployments
        rs.update_and_save({'rsrc_metadata': md})

        metadata_put_url = None
        for rd in rs.data:
            if rd.key == 'metadata_put_url':
                metadata_put_url = rd.value
                break
        if metadata_put_url:
            json_md = jsonutils.dumps(md)
            requests.put(metadata_put_url, json_md)

    @request_context
    def show_software_deployment(self, cnxt, deployment_id):
        sd = db_api.software_deployment_get(cnxt, deployment_id)
        return api.format_software_deployment(sd)

    @request_context
    def create_software_deployment(self, cnxt, server_id, config_id,
                                   input_values, action, status,
                                   status_reason, stack_user_project_id):

        sd = db_api.software_deployment_create(cnxt, {
            'config_id': config_id,
            'server_id': server_id,
            'input_values': input_values,
            'tenant': cnxt.tenant_id,
            'stack_user_project_id': stack_user_project_id,
            'action': action,
            'status': status,
            'status_reason': status_reason})
        self._push_metadata_software_deployments(cnxt, server_id)
        return api.format_software_deployment(sd)

    @request_context
    def signal_software_deployment(self, cnxt, deployment_id, details,
                                   updated_at):

        if not deployment_id:
            raise ValueError(_('deployment_id must be specified'))

        sd = db_api.software_deployment_get(cnxt, deployment_id)
        status = sd.status

        if not status == rpc_api.SOFTWARE_DEPLOYMENT_IN_PROGRESS:
            # output values are only expected when in an IN_PROGRESS state
            return

        details = details or {}

        output_status_code = rpc_api.SOFTWARE_DEPLOYMENT_OUTPUT_STATUS_CODE
        ov = sd.output_values or {}
        status = None
        status_reasons = {}
        status_code = details.get(output_status_code)
        if status_code and str(status_code) != '0':
            status = rpc_api.SOFTWARE_DEPLOYMENT_FAILED
            status_reasons[output_status_code] = _(
                'Deployment exited with non-zero status code: %s'
            ) % details.get(output_status_code)
            event_reason = 'deployment failed (%s)' % status_code
        else:
            event_reason = 'deployment succeeded'

        for output in sd.config.config['outputs'] or []:
            out_key = output['name']
            if out_key in details:
                ov[out_key] = details[out_key]
                if output.get('error_output', False):
                    status = rpc_api.SOFTWARE_DEPLOYMENT_FAILED
                    status_reasons[out_key] = details[out_key]
                    event_reason = 'deployment failed'

        for out_key in rpc_api.SOFTWARE_DEPLOYMENT_OUTPUTS:
            ov[out_key] = details.get(out_key)

        if status == rpc_api.SOFTWARE_DEPLOYMENT_FAILED:
            # build a status reason out of all of the values of outputs
            # flagged as error_output
            status_reasons = [' : '.join((k, six.text_type(status_reasons[k])))
                              for k in status_reasons]
            status_reason = ', '.join(status_reasons)
        else:
            status = rpc_api.SOFTWARE_DEPLOYMENT_COMPLETE
            status_reason = _('Outputs received')

        self.update_software_deployment(
            cnxt, deployment_id=deployment_id,
            output_values=ov, status=status, status_reason=status_reason,
            config_id=None, input_values=None, action=None,
            updated_at=updated_at)
        # Return a string describing the outcome of handling the signal data
        return event_reason

    @request_context
    def update_software_deployment(self, cnxt, deployment_id, config_id,
                                   input_values, output_values, action,
                                   status, status_reason, updated_at):
        update_data = {}
        if config_id:
            update_data['config_id'] = config_id
        if input_values:
            update_data['input_values'] = input_values
        if output_values:
            update_data['output_values'] = output_values
        if action:
            update_data['action'] = action
        if status:
            update_data['status'] = status
        if status_reason:
            update_data['status_reason'] = status_reason
        if updated_at:
            update_data['updated_at'] = timeutils.parse_isotime(updated_at)
        else:
            update_data['updated_at'] = timeutils.utcnow()

        sd = db_api.software_deployment_update(cnxt,
                                               deployment_id, update_data)

        # only push metadata if this update resulted in the config_id
        # changing, since metadata is just a list of configs
        if config_id:
            self._push_metadata_software_deployments(cnxt, sd.server_id)

        return api.format_software_deployment(sd)

    @request_context
    def delete_software_deployment(self, cnxt, deployment_id):
        db_api.software_deployment_delete(cnxt, deployment_id)

    @request_context
    def list_services(self, cnxt):
        result = [service_utils.format_service(srv)
                  for srv in db_api.service_get_all(cnxt)]
        return result

    def service_manage_report(self):
        cnxt = context.get_admin_context()

        if self.service_id is not None:
            # Service is already running
            db_api.service_update(
                cnxt,
                self.service_id,
                dict())
            LOG.info(_LI('Service %s is updated'), self.service_id)
        else:
            service_refs = db_api.service_get_all_by_args(cnxt,
                                                          self.host,
                                                          self.binary,
                                                          self.hostname)
            if len(service_refs) == 1:
                # Service was aborted or stopped
                service_ref = service_refs[0]

                if service_ref['deleted_at'] is None:
                    LOG.info(_LI('Service %s was aborted'), self.service_id)

                service_ref = db_api.service_update(
                    cnxt,
                    service_ref['id'],
                    dict(engine_id=self.engine_id,
                         deleted_at=None,
                         report_interval=cfg.CONF.periodic_interval))
                self.service_id = service_ref['id']
                LOG.info(_LI('Service %s is restarted'), self.service_id)
            elif len(service_refs) == 0:
                # Service is started now
                service_ref = db_api.service_create(
                    cnxt,
                    dict(host=self.host,
                         hostname=self.hostname,
                         binary=self.binary,
                         engine_id=self.engine_id,
                         topic=self.topic,
                         report_interval=cfg.CONF.periodic_interval)
                )
                self.service_id = service_ref['id']
                LOG.info(_LI('Service %s is started'), self.service_id)
