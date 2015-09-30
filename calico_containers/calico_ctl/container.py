"""
Usage:
  calicoctl container <CONTAINER> ip (add|remove) <IP> [--interface=<INTERFACE>]
  calicoctl container <CONTAINER> endpoint show
  calicoctl container <CONTAINER> profile (append|remove|set) [<PROFILES>...]
  calicoctl container add <CONTAINER> <IP> [--interface=<INTERFACE>]
  calicoctl container remove <CONTAINER>

Description:
  Add or remove containers to calico networking, manage their IP addresses and profiles.
  All these commands must be run on the host that contains the container.

Options:
  --interface=<INTERFACE>  The name to give to the interface in the container
                           [default: eth1]
"""
import os
import sys
import uuid

import docker.errors
from requests.exceptions import ConnectionError
from urllib3.exceptions import MaxRetryError
from subprocess import CalledProcessError
from subprocess32 import check_call
from netaddr import IPAddress, IPNetwork
from calico_ctl import endpoint
from pycalico import netns
from pycalico.datastore_datatypes import Endpoint

from connectors import client
from connectors import docker_client
from utils import hostname, DOCKER_ORCHESTRATOR_ID, NAMESPACE_ORCHESTRATOR_ID, \
    escape_etcd
from utils import enforce_root
from utils import print_paragraph
from utils import validate_ip


def validate_arguments(arguments):
    """
    Validate argument values:
        <IP>

    Arguments not validated:
        <CONTAINER>
        <INTERFACE>

    :param arguments: Docopt processed arguments
    """
    # Validate IP
    container_ip_ok = arguments.get("<IP>") is None or \
                        validate_ip(arguments["<IP>"], 4) or \
                        validate_ip(arguments["<IP>"], 6)

    # Print error message and exit if not valid argument
    if not container_ip_ok:
        print "Invalid IP address specified."
        sys.exit(1)

    endpoint.validate_arguments(arguments)

def container(arguments):
    """
    Main dispatcher for container commands. Calls the corresponding helper
    function.

    :param arguments: A dictionary of arguments already processed through
    this file's docstring with docopt
    :return: None
    """
    validate_arguments(arguments)

    try:
        workload_id = None
        if "<CONTAINER>" in arguments:
            container_id = arguments.get("<CONTAINER>")
            if container_id.startswith("/") and os.path.exists(container_id):
                # The ID is a path. Don't do any docker lookups
                workload_id = escape_etcd(container_id)
                orchestrator_id = NAMESPACE_ORCHESTRATOR_ID
            else:
                info = get_container_info_or_exit(container_id)
                workload_id = info["Id"]
                orchestrator_id = DOCKER_ORCHESTRATOR_ID

        if arguments.get("ip"):
            if arguments.get("add"):
                container_ip_add(arguments.get("<CONTAINER>"),
                                 arguments.get("<IP>"),
                                 arguments.get("--interface"))
            elif arguments.get("remove"):
                container_ip_remove(arguments.get("<CONTAINER>"),
                                    arguments.get("<IP>"),
                                    arguments.get("--interface"))
            else:
                if arguments.get("add"):
                    container_add(arguments.get("<CONTAINER>"),
                                  arguments.get("<IP>"),
                                  arguments.get("--interface"))
                if arguments.get("remove"):
                    container_remove(arguments.get("<CONTAINER>"))
        elif arguments.get("endpoint"):
            endpoint.endpoint_show(hostname,
                                   orchestrator_id,
                                   workload_id,
                                   None,
                                   True)
        elif arguments.get("profile"):
            if arguments.get("append"):
                endpoint.endpoint_profile_append(hostname,
                                                 orchestrator_id,
                                                 workload_id,
                                                 None,
                                                 arguments['<PROFILES>'])
            elif arguments.get("remove"):
                endpoint.endpoint_profile_remove(hostname,
                                                 orchestrator_id,
                                                 workload_id,
                                                 None,
                                                 arguments['<PROFILES>'])
            elif arguments.get("set"):
                endpoint.endpoint_profile_set(hostname,
                                              orchestrator_id,
                                              workload_id,
                                              None,
                                              arguments['<PROFILES>'])
        else:
            if arguments.get("add"):
                container_add(arguments.get("<CONTAINER>"),
                              arguments.get("<IP>"),
                              arguments.get("--interface"))
            if arguments.get("remove"):
                container_remove(arguments.get("<CONTAINER>"))
    except ConnectionError as e:
        # We hit a "Permission denied error (13) if the docker daemon
        # does not have sudo permissions
        if permission_denied_error(e):
            print_paragraph("Unable to run command.  Re-run the "
                            "command as root, or configure the docker "
                            "group to run with sudo privileges (see docker "
                            "installation guide for details).")
        else:
            print_paragraph("Unable to run docker commands. Is the docker "
                            "daemon running?")
        sys.exit(1)


def container_add(container_id, ip, interface):
    """
    Add a container (on this host) to Calico networking with the given IP.

    :param container_id: The namespace path or the docker name/ID of the container.
    :param ip: An IPAddress object with the desired IP to assign.
    :param interface: The name of the interface in the container.
    """
    # The netns manipulations must be done as root.
    enforce_root()

    if container_id.startswith("/") and os.path.exists(container_id):
        # The ID is a path. Don't do any docker lookups
        workload_id = escape_etcd(container_id)
        orchestrator_id = NAMESPACE_ORCHESTRATOR_ID
        namespace = netns.Namespace(container_id)
    else:
        info = get_container_info_or_exit(container_id)
        workload_id = info["Id"]
        orchestrator_id = DOCKER_ORCHESTRATOR_ID
        namespace = netns.PidNamespace(info["State"]["Pid"])

        # Check the container is actually running.
        if not info["State"]["Running"]:
            print "%s is not currently running." % container_id
            sys.exit(1)

        # We can't set up Calico if the container shares the host namespace.
        if info["HostConfig"]["NetworkMode"] == "host":
            print "Can't add %s to Calico because it is " \
                  "running NetworkMode = host." % container_id
            sys.exit(1)

    # Check if the container already exists
    try:
        _ = client.get_endpoint(hostname=hostname,
                                orchestrator_id=orchestrator_id,
                                workload_id=workload_id)
    except KeyError:
        # Calico doesn't know about this container.  Continue.
        pass
    else:
        # Calico already set up networking for this container.  Since we got
        # called with an IP address, we shouldn't just silently exit, since
        # that would confuse the user: the container would not be reachable on
        # that IP address.
        print "%s has already been configured with Calico Networking." % \
              container_id
        sys.exit(1)

    # Check the IP is in the allocation pool.  If it isn't, BIRD won't export
    # it.
    ip = IPAddress(ip)
    pool = get_pool_or_exit(ip)

    # The next hop IPs for this host are stored in etcd.
    next_hops = client.get_default_next_hops(hostname)
    try:
        next_hops[ip.version]
    except KeyError:
        print "This node is not configured for IPv%d." % ip.version
        sys.exit(1)

    # Assign the IP
    if not client.assign_address(pool, ip):
        print "IP address is already assigned in pool %s " % pool
        sys.exit(1)

    # Get the next hop for the IP address.
    next_hop = next_hops[ip.version]

    network = IPNetwork(IPAddress(ip))
    ep = Endpoint(hostname=hostname,
                  orchestrator_id=DOCKER_ORCHESTRATOR_ID,
                  workload_id=workload_id,
                  endpoint_id=uuid.uuid1().hex,
                  state="active",
                  mac=None)
    if network.version == 4:
        ep.ipv4_nets.add(network)
        ep.ipv4_gateway = next_hop
    else:
        ep.ipv6_nets.add(network)
        ep.ipv6_gateway = next_hop

    # Create the veth, move into the container namespace, add the IP and
    # set up the default routes.
    #netns.create_veth(ep.name, ep.temp_interface_name)
    IP_CMD_TIMEOUT = 5
    try:
        print "------------------------"
        check_call(['ip', 'link',
                'add', ep.name,
                'type', 'veth',
                'peer', 'name', ep.temp_interface_name,
                'mtu', '9001'],
                   timeout=IP_CMD_TIMEOUT)
        print "+++++++++++++++++++++++++++"
    # Set the host end of the veth to 'up' so felix notices it.
        check_call(['ip', 'link', 'set', ep.name, 'up'],
                   timeout=IP_CMD_TIMEOUT)
        check_call(['ip', 'link', 'set', ep.name, 'mtu', '9001'],
                   timeout=IP_CMD_TIMEOUT)
    except Exception as e:
        print e
    netns.move_veth_into_ns(namespace, ep.temp_interface_name, interface)
    #print "Endpoint name: %s" % ep.name
    #print "Endpoint temp interface name: %s" % ep.temp_interface_name
    #print "interface is: %s" % interface
    netns.add_ip_to_ns_veth(namespace, ip, interface)
    netns.add_ns_default_route(namespace, next_hop, interface)

    # Grab the MAC assigned to the veth in the namespace.
    ep.mac = netns.get_ns_veth_mac(namespace, interface)

    # Register the endpoint with Felix.
    client.set_endpoint(ep)

    # Let the caller know what endpoint was created.
    return ep


def container_remove(container_id):
    """
    Remove a container (on this host) from Calico networking.

    The container may be left in a state without any working networking.
    If there is a network adaptor in the host namespace used by the container
    then it is removed.

    :param container_id: The namespace path or the ID of the container.
    """
    # The netns manipulations must be done as root.
    enforce_root()

    # Resolve the name to ID.
    if container_id.startswith("/") and os.path.exists(container_id):
        # The ID is a path. Don't do any docker lookups
        workload_id = escape_etcd(container_id)
        orchestrator_id = NAMESPACE_ORCHESTRATOR_ID
    else:
        workload_id = get_workload_id(container_id)
        orchestrator_id = DOCKER_ORCHESTRATOR_ID

    # Find the endpoint ID. We need this to find any ACL rules
    try:
        endpoint = client.get_endpoint(hostname=hostname,
                                       orchestrator_id=orchestrator_id,
                                       workload_id=workload_id)
    except KeyError:
        print "Container %s doesn't contain any endpoints" % container_id
        sys.exit(1)

    # Remove any IP address assignments that this endpoint has
    for net in endpoint.ipv4_nets | endpoint.ipv6_nets:
        assert(net.size == 1)
        ip = net.ip
        pools = client.get_ip_pools(ip.version)
        for pool in pools:
            if ip in pool:
                # Ignore failure to unassign address, since we're not
                # enforcing assignments strictly in datastore.py.
                client.unassign_address(pool, ip)

    try:
        # Remove the interface if it exists
        netns.remove_veth(endpoint.name)
    except CalledProcessError:
        print "Could not remove Calico interface %s" % endpoint.name
        sys.exit(1)

    # Remove the container from the datastore.
    client.remove_workload(hostname, orchestrator_id, workload_id)

    print "Removed Calico interface from %s" % container_id


# TODO: If container created with IPv4 and then add IPv6 address, do we set up the
# default route for IPv6 correctly (code read suggests not).
def container_ip_add(container_id, ip, interface):
    """
    Add an IP address to an existing Calico networked container.

    :param container_id: The namespace path or container_id of the container.
    :param ip: The IP to add
    :param interface: The name of the interface in the container.

    :return: None
    """
    address = IPAddress(ip)

    # The netns manipulations must be done as root.
    enforce_root()

    pool = get_pool_or_exit(address)

    if container_id.startswith("/") and os.path.exists(container_id):
        # The ID is a path. Don't do any docker lookups
        workload_id = escape_etcd(container_id)
        namespace = netns.Namespace(container_id)
        orchestrator_id = NAMESPACE_ORCHESTRATOR_ID
    else:
        info = get_container_info_or_exit(container_id)
        workload_id = info["Id"]
        namespace = netns.PidNamespace(info["State"]["Pid"])
        orchestrator_id = DOCKER_ORCHESTRATOR_ID

        # Check the container is actually running.
        if not info["State"]["Running"]:
            print "%s is not currently running." % container_id
            sys.exit(1)

    # Check that the container is already networked
    try:
        endpoint = client.get_endpoint(hostname=hostname,
                                       orchestrator_id=orchestrator_id,
                                       workload_id=workload_id)
    except KeyError:
        print "Failed to add IP address to container.\n"
        print_container_not_in_calico_msg(container_id)
        sys.exit(1)

    # From here, this method starts having side effects. If something
    # fails then at least try to leave the system in a clean state.
    if not client.assign_address(pool, address):
        print "IP address is already assigned in pool %s " % pool
        sys.exit(1)

    try:
        if address.version == 4:
            endpoint.ipv4_nets.add(IPNetwork(address))
        else:
            endpoint.ipv6_nets.add(IPNetwork(address))
        client.update_endpoint(endpoint)
    except (KeyError, ValueError):
        client.unassign_address(pool, ip)
        print "Error updating datastore. Aborting."
        sys.exit(1)

    try:
        netns.add_ip_to_ns_veth(namespace, address, interface)
    except CalledProcessError:
        print "Error updating networking in container. Aborting."
        if address.version == 4:
            endpoint.ipv4_nets.remove(IPNetwork(address))
        else:
            endpoint.ipv6_nets.remove(IPNetwork(address))
        client.update_endpoint(endpoint)
        client.unassign_address(pool, ip)
        sys.exit(1)

    print "IP %s added to %s" % (ip, container_id)


def container_ip_remove(container_id, ip, interface):
    """
    Add an IP address to an existing Calico networked container.

    :param container_id: The namespace path or container_id of the container.
    :param ip: The IP to add
    :param interface: The name of the interface in the container.

    :return: None
    """
    address = IPAddress(ip)

    # The netns manipulations must be done as root.
    enforce_root()

    pool = get_pool_or_exit(address)
    if container_id.startswith("/") and os.path.exists(container_id):
        # The ID is a path. Don't do any docker lookups
        workload_id = escape_etcd(container_id)
        namespace = netns.Namespace(container_id)
        orchestrator_id = NAMESPACE_ORCHESTRATOR_ID
    else:
        info = get_container_info_or_exit(container_id)
        workload_id = info["Id"]
        namespace = netns.PidNamespace(info["State"]["Pid"])
        orchestrator_id = DOCKER_ORCHESTRATOR_ID

        # Check the container is actually running.
        if not info["State"]["Running"]:
            print "%s is not currently running." % container_id
            sys.exit(1)

    # Check that the container is already networked
    try:
        endpoint = client.get_endpoint(hostname=hostname,
                                       orchestrator_id=orchestrator_id,
                                       workload_id=workload_id)
        if address.version == 4:
            nets = endpoint.ipv4_nets
        else:
            nets = endpoint.ipv6_nets

        if not IPNetwork(address) in nets:
            print "IP address is not assigned to container. Aborting."
            sys.exit(1)

    except KeyError:
        print "Container is unknown to Calico."
        sys.exit(1)

    try:
        nets.remove(IPNetwork(address))
        client.update_endpoint(endpoint)
    except (KeyError, ValueError):
        print "Error updating datastore. Aborting."
        sys.exit(1)

    try:
        netns.remove_ip_from_ns_veth(namespace, address, interface)

    except CalledProcessError:
        print "Error updating networking in container. Aborting."
        sys.exit(1)

    client.unassign_address(pool, address)

    print "IP %s removed from %s" % (ip, container_id)


def get_pool_or_exit(ip):
    """
    Get the first allocation pool that an IP is in.

    :param ip: The IPAddress to find the pool for.
    :return: The pool or sys.exit
    """
    pools = client.get_ip_pools(ip.version)
    pool = None
    for candidate_pool in pools:
        if ip in candidate_pool:
            pool = candidate_pool
            break
    if pool is None:
        print "%s is not in any configured pools" % ip
        sys.exit(1)

    return pool


def print_container_not_in_calico_msg(container_name):
    """
    Display message indicating that the supplied container is not known to
    Calico.
    :param container_name: The container name.
    :return: None.
    """
    print_paragraph("Container %s is unknown to Calico." % container_name)
    print_paragraph("Use `calicoctl container add` to add the container "
                    "to the Calico network.")


def get_workload_id(container_id):
    """
    Get the a workload ID from either a namespace path or a partial Docker
    ID or Docker name.

    :param container_id: The namespace or Docker ID/Name.
    :return: The workload ID as a string.
    """
    if container_id.startswith("/") and os.path.exists(container_id):
        # The ID is a path. Don't do any docker lookups
        workload_id = escape_etcd(container_id)
    else:
        info = get_container_info_or_exit(container_id)
        workload_id = info["Id"]
    return workload_id


def get_container_info_or_exit(container_name):
    """
    Get the full container info array from a partial ID or name.

    :param container_name: The partial ID or name of the container.
    :return: The container info array, or sys.exit if not found.
    """
    try:
        info = docker_client.inspect_container(container_name)
    except docker.errors.APIError as e:
        if e.response.status_code == 404:
            print "Container %s was not found." % container_name
        else:
            print e.message
        sys.exit(1)
    return info


def permission_denied_error(conn_error):
    """
    Determine whether the supplied connection error is from a permission denied
    error.
    :param conn_error: A requests.exceptions.ConnectionError instance
    :return: True if error is from permission denied.
    """
    # Grab the MaxRetryError from the ConnectionError arguments.
    mre = None
    for arg in conn_error.args:
        if isinstance(arg, MaxRetryError):
            mre = arg
            break
    if not mre:
        return None

    # See if permission denied is in the MaxRetryError arguments.
    se = None
    for arg in mre.args:
        if "Permission denied" in str(arg):
            se = arg
            break
    if not se:
        return None

    return True
