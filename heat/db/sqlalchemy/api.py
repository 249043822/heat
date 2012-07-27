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

'''Implementation of SQLAlchemy backend.'''
from sqlalchemy.orm.session import Session

from heat.common.exception import NotFound
from heat.db.sqlalchemy import models
from heat.db.sqlalchemy.session import get_session
from heat.engine import auth


def model_query(context, *args, **kwargs):
    """
    :param session: if present, the session to use
    """
    session = kwargs.get('session') or get_session()

    query = session.query(*args)

    return query


def raw_template_get(context, template_id):
    result = model_query(context, models.RawTemplate).get(template_id)

    if not result:
        raise NotFound("raw template with id %s not found" % template_id)

    return result


def raw_template_get_all(context):
    results = model_query(context, models.RawTemplate).all()

    if not results:
        raise NotFound('no raw templates were found')

    return results


def raw_template_create(context, values):
    raw_template_ref = models.RawTemplate()
    raw_template_ref.update(values)
    raw_template_ref.save()
    return raw_template_ref


def resource_get(context, resource_id):
    result = model_query(context, models.Resource).get(resource_id)

    if not result:
        raise NotFound("resource with id %s not found" % resource_id)

    return result


def resource_get_by_name_and_stack(context, resource_name, stack_id):
    result = model_query(context, models.Resource).\
                        filter_by(name=resource_name).\
                        filter_by(stack_id=stack_id).first()

    return result


def resource_get_by_physical_resource_id(context, physical_resource_id):
    result = (model_query(context, models.Resource)
              .filter_by(nova_instance=physical_resource_id)
              .first())
    if (result is not None and context is not None and
        result.stack.tenant != context.tenant):
        return None
    return result


def resource_get_all(context):
    results = model_query(context, models.Resource).all()

    if not results:
        raise NotFound('no resources were found')

    return results


def resource_create(context, values):
    resource_ref = models.Resource()
    resource_ref.update(values)
    resource_ref.save()
    return resource_ref


def resource_get_all_by_stack(context, stack_id):
    results = model_query(context, models.Resource).\
                filter_by(stack_id=stack_id).all()

    if not results:
        raise NotFound("no resources for stack_id %s were found" % stack_id)

    return results


def stack_get_by_name(context, stack_name, owner_id=None):
    if owner_id:
        result = model_query(context, models.Stack).\
                            filter_by(owner_id=owner_id).\
                            filter_by(name=stack_name).first()
    else:
        result = model_query(context, models.Stack).\
                            filter_by(name=stack_name).first()
    if (result is not None and context is not None and
        result.tenant != context.tenant):
        return None
    return result


def stack_get(context, stack_id):
    result = model_query(context, models.Stack).get(stack_id)

    if (result is not None and context is not None and
        result.tenant != context.tenant):
        return None

    return result


def stack_get_all(context):
    results = model_query(context, models.Stack).\
                         filter_by(owner_id=None).all()
    return results


def stack_get_by_tenant(context):
    results = model_query(context, models.Stack).\
                         filter_by(owner_id=None).\
                         filter_by(tenant=context.tenant).all()
    return results


def stack_create(context, values):
    stack_ref = models.Stack()
    stack_ref.update(values)
    stack_ref.save()
    return stack_ref


def stack_update(context, stack_id, values):
    stack = stack_get(context, stack_id)

    if not stack:
        raise NotFound('Attempt to update a stack with id: %s %s' %
                        (stack_id, 'that does not exist'))

    old_template_id = stack.raw_template_id

    stack.update(values)
    stack.save()

    # When the raw_template ID changes, we delete the old template
    # after storing the new template ID
    if stack.raw_template_id != old_template_id:
        session = Session.object_session(stack)
        rt = raw_template_get(context, old_template_id)
        session.delete(rt)
        session.flush()


def stack_delete(context, stack_id):
    s = stack_get(context, stack_id)
    if not s:
        raise NotFound('Attempt to delete a stack with id: %s %s' %
                        (stack_id, 'that does not exist'))

    session = Session.object_session(s)

    for e in s.events:
        session.delete(e)

    for r in s.resources:
        session.delete(r)

    rt = s.raw_template
    uc = s.user_creds

    session.delete(s)
    session.delete(rt)
    session.delete(uc)

    session.flush()


def user_creds_create(values):
    user_creds_ref = models.UserCreds()
    user_creds_ref.update(values)
    user_creds_ref.password = auth.encrypt(values['password'])
    user_creds_ref.service_password = auth.encrypt(values['service_password'])
    user_creds_ref.aws_creds = auth.encrypt(values['aws_creds'])
    user_creds_ref.save()
    return user_creds_ref


def user_creds_get(user_creds_id):
    result = model_query(None, models.UserCreds).get(user_creds_id)
    result.password = auth.decrypt(result.password)
    result.service_password = auth.decrypt(result.service_password)
    result.aws_creds = auth.decrypt(result.aws_creds)

    return result


def event_get(context, event_id):
    result = model_query(context, models.Event).get(event_id)

    return result


def event_get_all(context):
    results = model_query(context, models.Event).all()

    return results


def event_get_all_by_tenant(context):
    stacks = model_query(context, models.Stack).\
                          filter_by(tenant=context.tenant).all()
    results = []
    for stack in stacks:
        results.extend(model_query(context, models.Event).
                                   filter_by(stack_id=stack.id).all())

    return results


def event_get_all_by_stack(context, stack_id):
    results = model_query(context, models.Event).\
                        filter_by(stack_id=stack_id).all()

    return results


def event_create(context, values):
    event_ref = models.Event()
    event_ref.update(values)
    event_ref.save()
    return event_ref


def watch_rule_get(context, watch_rule_name):
    result = model_query(context, models.WatchRule).\
                        filter_by(name=watch_rule_name).first()
    return result


def watch_rule_get_all(context):
    results = model_query(context, models.WatchRule).all()
    return results


def watch_rule_create(context, values):
    obj_ref = models.WatchRule()
    obj_ref.update(values)
    obj_ref.save()
    return obj_ref


def watch_rule_delete(context, watch_name):
    wr = model_query(context, models.WatchRule).\
                        filter_by(name=watch_name).first()

    if not wr:
        raise NotFound('Attempt to delete a watch_rule with name: %s %s' %
                        (watch_name, 'that does not exist'))

    session = Session.object_session(wr)

    for d in wr.watch_data:
        session.delete(d)

    session.delete(wr)
    session.flush()


def watch_data_create(context, values):
    obj_ref = models.WatchData()
    obj_ref.update(values)
    obj_ref.save()
    return obj_ref


def watch_data_get_all(context, watch_id):
    # get dataset ordered by creation_at (most recient first)
    results = model_query(context, models.WatchData).\
                          filter_by(watch_rule_id=watch_id).all()
    return results


def watch_data_delete(context, watch_name):
    ds = model_query(context, models.WatchRule).\
                     filter_by(name=watch_name).all()

    if not ds:
        raise NotFound('Attempt to delete watch_data with name: %s %s' %
                        (watch_name, 'that does not exist'))

    session = Session.object_session(ds)
    for d in ds:
        session.delete(d)
    session.flush()
