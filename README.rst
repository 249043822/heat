====
HEAT
====

This is an OpenStack style project that provides a REST API to orchestrate
multiple cloud applications implementing well-known standards such as AWS
CloudFormation and TOSCA.

Currently the developers are focusing on AWS CloudFormations but are watching
the development of the TOSCA specification.

Why heat? It makes the clouds rise and keeps them there.

Quick Start
-----------

If you'd like to run from the master branch, you can clone the git repo:

    git clone git@github.com:heat-api/heat.git


Install Heat by running::

    sudo python setup.py install

Setup Heat:

    source ~/.openstack/keystonerc
    heat jeos_create F16 x86_64
    nova keypair-add --pub ~/.ssh/id_rsa.pub my_key

try:
shell1:

    heat-api

shell2:

    sudo heat-engine

shell3:
    
    heat create my_stack --template-url=https://raw.github.com/heat-api/heat/master/templates/WordPress_Single_Instance.template

References
----------
* http://docs.amazonwebservices.com/AWSCloudFormation/latest/APIReference/API_CreateStack.html
* http://docs.amazonwebservices.com/AWSCloudFormation/latest/UserGuide/create-stack.html
* http://docs.amazonwebservices.com/AWSCloudFormation/latest/UserGuide/aws-template-resource-type-ref.html
* http://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca

Related projects
----------------
* http://wiki.openstack.org/Donabe
* http://wiki.openstack.org/DatabaseAsAService (could be used to provide AWS::RDS::DBInstance)
* http://wiki.openstack.org/QueueService (could be used to provide AWS::SQS::Queue)

