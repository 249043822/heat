###
### an unparented test -- no encapsulating class, just any fn starting with
### 'test'.
## http://darcs.idyll.org/~t/projects/nose-demo/simple/tests/test_stuff.py.html
###

import io
import sys
import mox
import nose
from nose.plugins.attrib import attr
from nose import with_setup
import unittest
import shutil

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


def setUp_credential_file():
    f = open('/tmp/incredible', 'w')
    f.write('junk, just junk')
    f.close()

def tearDown_credential_file():
    shutil.rmtree('/tmp/incredible', ignore_errors=True)

@with_setup(setUp_credential_file, tearDown_credential_file)
@attr(tag=['unit', 'cfn-hup'])
@attr(speed='fast')
def test_hup_conf1():
    good= """
[main]
stack=stack-test
credential-file=/tmp/incredible
region=unit-test-a
interval=3
"""
    c = HupConfig([io.BytesIO(good)])
    assert(c.stack == 'stack-test')
    assert(c.credential_file == '/tmp/incredible')
    assert(c.region == 'unit-test-a')
    assert(c.interval == 3)


@with_setup(setUp_credential_file, tearDown_credential_file)
@attr(tag=['unit', 'cfn-hup'])
@attr(speed='fast')
def test_hup_default():
    good= """
[main]
stack=stack-testr
credential-file=/tmp/incredible
"""
    c = HupConfig([io.BytesIO(good)])
    assert(c.stack == 'stack-testr')
    assert(c.credential_file == '/tmp/incredible')
    assert(c.region == 'nova')
    assert(c.interval == 10)


@with_setup(setUp_credential_file, tearDown_credential_file)
@attr(tag=['unit', 'cfn-hup'])
@attr(speed='fast')
def test_hup_hook():
    good= """
[main]
stack=stackname_is_fred
credential-file=/tmp/incredible

[bla]
triggers=post.update
path=Resources.webserver
action=systemctl reload httpd.service
runas=root
"""
    c = HupConfig([io.BytesIO(good)])
    assert(c.stack == 'stackname_is_fred')
    assert(c.credential_file == '/tmp/incredible')
    assert(c.region == 'nova')
    assert(c.interval == 10)

    assert(c.hooks['bla'].triggers == 'post.update')
    assert(c.hooks['bla'].path == 'Resources.webserver')
    assert(c.hooks['bla'].action == 'systemctl reload httpd.service')
    assert(c.hooks['bla'].runas == 'root')


def tearDown_metadata_files():
    shutil.rmtree('/tmp/_files_test_', ignore_errors=True)


class PopenMock:
    def communicate(self):
        self.returncode = 0
        return ['', None]


@with_setup(None, tearDown_metadata_files)
@attr(tag=['unit', 'cfn-metadata'])
@attr(speed='fast')
def test_metadata_files():

    j = ''' {
        "AWS::CloudFormation::Init" : {
          "config" : {
            "files" : {
              "/tmp/_files_test_/epel.repo" : {
                "source" : "https://raw.github.com/heat-api/heat/master/README.rst",
                "mode"   : "000644"
              },
              "/tmp/_files_test_/_with/some/dirs/to/make/small.conf" : {
                "content" : "not much really",
                "mode"    : "000777"
              },
              "/tmp/_files_test_/node.json": {
                  "content": {
                      "myapp": {
                          "db": {
                              "database": "RefDBName",
                              "user": "RefDBUser",
                              "host": "Fn::GetAttDBInstance.Endpoint.Address",
                              "password": "RefDBPassword"
                          }
                      },
                      "run_list": ["recipe[wordpress]", "bla"]
                  },
                  "mode": "000600"
              }
            }
          }
        }
    }
'''

    metadata = Metadata('tester',
                        'ronald')
    metadata.retrieve(j)
    metadata.cfn_init()

    # mask out the file type
    mask = int('007777', 8)
    assert(os.stat('/tmp/_files_test_/node.json').st_mode & mask == 0600)
    assert(os.stat('/tmp/_files_test_/epel.repo').st_mode & mask == 0644)
    assert(os.stat('/tmp/_files_test_/_with/some/dirs/to/make/small.conf').st_mode & mask == 0777)

class CommandRunnerTest(unittest.TestCase):
    def setUp(self):
        self.m = mox.Mox()

    def tearDown(self):
        self.m.UnsetStubs()

    @attr(tag=['unit', 'cfn-helper'])
    @attr(speed='fast')
    def test_runas(self):
        import subprocess
        self.m.StubOutWithMock(subprocess, 'Popen')
        subprocess.Popen(['su', 'user', '-c', 'some command'],
                         stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE).AndReturn(PopenMock())

        self.m.ReplayAll()
        CommandRunner('some command').run('user')
        self.m.VerifyAll()

    @attr(tag=['unit', 'cfn-helper'])
    @attr(speed='fast')
    def test_default_runas(self):
        import subprocess
        self.m.StubOutWithMock(subprocess, 'Popen')
        subprocess.Popen(['su', 'root', '-c', 'some command'],
                         stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE).AndReturn(PopenMock())

        self.m.ReplayAll()
        CommandRunner('some command').run()
        self.m.VerifyAll()


if __name__ == '__main__':
    sys.argv.append(__file__)
    nose.main()
