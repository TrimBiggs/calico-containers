# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from tests.st.test_base import TestBase
from tests.st.utils.docker_host import DockerHost
from tests.st.utils.utils import get_ip
from tests.st.utils.workload import NET_NONE


class TestNoOrchestratorMultiHost(TestBase):
    def test_multi_host(self):
        """
        Test mainline functionality without using an orchestrator plugin on
        multiple hosts.
        """
        with DockerHost('host1') as host1, DockerHost('host2', start_calico=False) as host2:
            # Start calico manually on host2
            host2.start_calico_node_with_docker()

            # TODO ipv6 too
            host1.calicoctl("profile add TEST_GROUP")

            # Use standard docker bridge networking for one and --net=none
            # for the other
            workload1 = host1.create_workload("workload1")
            workload2 = host2.create_workload("workload2", network=NET_NONE)

            # Add the nodes to Calico networking.
            host1.calicoctl("container add %s 192.168.1.1" % workload1)
            host2.calicoctl("container add %s 192.168.1.2" % workload2)

            # Now add the profiles - one using set and one using append
            host1.calicoctl("container %s profile set TEST_GROUP" % workload1)
            host2.calicoctl("container %s profile append TEST_GROUP" % workload2)

            # TODO - assert on output of endpoint show and endpoint profile
            # show commands.

            # Check it works
            workload1.assert_can_ping("192.168.1.2", retries=3)
            workload2.assert_can_ping("192.168.1.1", retries=3)

            # Test the teardown commands
            host1.calicoctl("profile remove TEST_GROUP")
            host1.calicoctl("container remove %s" % workload1)
            host2.calicoctl("container remove %s" % workload2)
            host1.calicoctl("pool remove 192.168.0.0/16")
            host1.calicoctl("node stop")
            host1.calicoctl("node remove")
            host2.calicoctl("node stop")
            host2.calicoctl("node remove")
