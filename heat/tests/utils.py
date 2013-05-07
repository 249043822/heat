# vim: tabstop=4 shiftwidth=4 softtabstop=4

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

import sys
import functools

from testtools import skipIf

from heat.common import context
from heat.engine import parser

from heat.db.sqlalchemy.session import get_engine
from heat.db import migration


class skip_if(object):
    """Decorator that skips a test if condition is true."""
    def __init__(self, condition, msg):
        self.condition = condition
        self.message = msg

    def __call__(self, func):
        def _skipper(*args, **kw):
            """Wrapped skipper function."""
            skipIf(self.condition, self.message)
            func(*args, **kw)
        _skipper.__name__ = func.__name__
        _skipper.__doc__ = func.__doc__
        return _skipper


def stack_delete_after(test_fn):
    """
    Decorator which calls test class self.stack.delete()
    to ensure tests clean up their stacks regardless of test success/failure
    """
    @functools.wraps(test_fn)
    def wrapped_test(test_case, *args, **kwargs):
        def delete_stack():
            stack = getattr(test_case, 'stack', None)
            if stack is not None and stack.id is not None:
                stack.delete()

        try:
            test_fn(test_case, *args, **kwargs)
        except:
            exc_class, exc_val, exc_tb = sys.exc_info()
            try:
                delete_stack()
            finally:
                raise exc_class, exc_val, exc_tb
        else:
            delete_stack()

    return wrapped_test


def setup_dummy_db():
    migration.db_sync()
    engine = get_engine()
    conn = engine.connect()


def parse_stack(t, params={}, stack_name='test_stack', stack_id=None):
    ctx = context.RequestContext.from_dict({'tenant_id': 'test_tenant',
                                            'username': 'test_username',
                                            'password': 'password',
                                            'auth_url':
                                            'http://localhost:5000/v2.0'})
    template = parser.Template(t)
    parameters = parser.Parameters(stack_name, template, params)
    stack = parser.Stack(ctx, stack_name, template, parameters, stack_id)

    return stack
