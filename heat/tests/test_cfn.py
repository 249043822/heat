###
### an unparented test -- no encapsulating class, just any fn starting with
### 'test'.
## http://darcs.idyll.org/~t/projects/nose-demo/simple/tests/test_stuff.py.html
###

import io
import sys
import nose
from nose.plugins.attrib import attr
from nose import with_setup

from heat.cfntools.cfn_helper import *


@attr(tag=['unit', 'cfn_helper'])
@attr(speed='fast')
def test_boolean():

    assert(to_boolean('true'))
    assert(to_boolean(True))
    assert(to_boolean('TRUE'))
    assert(to_boolean('True'))
    assert(to_boolean('Yes'))
    assert(to_boolean('YES'))
    assert(to_boolean('yes'))
    assert(to_boolean('1'))
    assert(to_boolean(1))

    assert(not to_boolean('tru'))
    assert(not to_boolean(False))
    assert(not to_boolean('False'))
    assert(not to_boolean('FALSE'))
    assert(not to_boolean('No'))
    assert(not to_boolean('NO'))
    assert(not to_boolean('no'))
    assert(not to_boolean('0334'))
    assert(not to_boolean(0))
    assert(not to_boolean(56))


@attr(tag=['unit', 'cfn-hup'])
@attr(speed='fast')
def test_hup_conf1():
    good= """
[main]
stack=stack-test
credential-file=/path/to/creds_file
region=unit-test-a
interval=3
"""
    c = HupConfig([io.BytesIO(good)])
    assert(c.stack == 'stack-test')
    assert(c.credential_file == '/path/to/creds_file')
    assert(c.region == 'unit-test-a')
    assert(c.interval == 3)


@attr(tag=['unit', 'cfn-hup'])
@attr(speed='fast')
def test_hup_default():
    good= """
[main]
stack=stack-testr
credential-file=/path/to/creds_file
"""
    c = HupConfig([io.BytesIO(good)])
    assert(c.stack == 'stack-testr')
    assert(c.credential_file == '/path/to/creds_file')
    assert(c.region == 'nova')
    assert(c.interval == 10)


@attr(tag=['unit', 'cfn-hup'])
@attr(speed='fast')
def test_hup_hook():
    good= """
[main]
stack=stackname_is_fred
credential-file=/path/to/creds_file

[bla]
triggers=post.update
path=Resources.webserver
action=systemctl reload httpd.service
runas=root
"""
    c = HupConfig([io.BytesIO(good)])
    assert(c.stack == 'stackname_is_fred')
    assert(c.credential_file == '/path/to/creds_file')
    assert(c.region == 'nova')
    assert(c.interval == 10)

    assert(c.hooks['bla'].triggers == 'post.update')
    assert(c.hooks['bla'].path == 'Resources.webserver')
    assert(c.hooks['bla'].action == 'systemctl reload httpd.service')
    assert(c.hooks['bla'].runas == 'root')


if __name__ == '__main__':
    sys.argv.append(__file__)
    nose.main()
