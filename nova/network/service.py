# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
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

"""
Network Hosts are responsible for allocating ips and setting up network
"""

import logging
import math

import IPy

from nova import db
from nova import exception
from nova import flags
from nova import service
from nova import utils
from nova.network import linux_net


FLAGS = flags.FLAGS
flags.DEFINE_string('network_type',
                    'flat',
                    'Service Class for Networking')
flags.DEFINE_string('flat_network_bridge', 'br100',
                    'Bridge for simple network instances')
flags.DEFINE_list('flat_network_ips',
                  ['192.168.0.2', '192.168.0.3', '192.168.0.4'],
                  'Available ips for simple network')
flags.DEFINE_string('flat_network_network', '192.168.0.0',
                    'Network for simple network')
flags.DEFINE_string('flat_network_netmask', '255.255.255.0',
                    'Netmask for simple network')
flags.DEFINE_string('flat_network_gateway', '192.168.0.1',
                    'Broadcast for simple network')
flags.DEFINE_string('flat_network_broadcast', '192.168.0.255',
                    'Broadcast for simple network')
flags.DEFINE_string('flat_network_dns', '8.8.4.4',
                    'Dns for simple network')

flags.DEFINE_integer('vlan_start', 100, 'First VLAN for private networks')
flags.DEFINE_integer('num_networks', 1000, 'Number of networks to support')
flags.DEFINE_string('vpn_ip', utils.get_my_ip(),
                    'Public IP for the cloudpipe VPN servers')
flags.DEFINE_integer('vpn_start', 1000, 'First Vpn port for private networks')
flags.DEFINE_integer('network_size', 256,
                        'Number of addresses in each private subnet')
flags.DEFINE_string('public_range', '4.4.4.0/24', 'Public IP address block')
flags.DEFINE_string('private_range', '10.0.0.0/8', 'Private IP address block')
flags.DEFINE_integer('cnt_vpn_clients', 5,
                        'Number of addresses reserved for vpn clients')


class AddressAlreadyAllocated(exception.Error):
    pass


class AddressNotAllocated(exception.Error):
    pass


# TODO(vish): some better type of dependency injection?
_driver = linux_net


def type_to_class(network_type):
    """Convert a network_type string into an actual Python class"""
    if not network_type:
        logging.warn("Network type couldn't be determined, using %s" %
                      FLAGS.network_type)
        network_type = FLAGS.network_type
    if network_type == 'flat':
        return FlatNetworkService
    elif network_type == 'vlan':
        return VlanNetworkService
    raise exception.NotFound("Couldn't find %s network type" % network_type)


def setup_compute_network(project_id):
    """Sets up the network on a compute host"""
    network = db.project_get_network(None, project_id)
    srv = type_to_class(network.kind)
    srv.setup_compute_network(network)


def get_host_for_project(project_id):
    """Get host allocated to project from datastore"""
    return db.project_get_network(None, project_id).node_name


class BaseNetworkService(service.Service):
    """Implements common network service functionality

    This class must be subclassed.
    """

    def set_network_host(self, project_id, context=None):
        """Safely sets the host of the projects network"""
        network_ref = db.project_get_network(context, project_id)
        # TODO(vish): can we minimize db access by just getting the
        #             id here instead of the ref?
        network_id = network_ref['id']
        host = db.network_set_host(context,
                                   network_id,
                                   FLAGS.node_name)
        self._on_set_network_host(context, network_id)
        return host

    def allocate_fixed_ip(self, project_id, instance_id, context=None,
                          *args, **kwargs):
        """Gets fixed ip from the pool"""
        network_ref = db.project_get_network(context, project_id)
        address = db.fixed_ip_allocate_address(context, network_ref['id'])
        db.fixed_ip_instance_associate(context,
                                       address,
                                       instance_id)
        return address

    def deallocate_fixed_ip(self, address, context=None):
        """Returns a fixed ip to the pool"""
        db.fixed_ip_deallocate(context, address)
        db.fixed_ip_instance_disassociate(context, address)


    def _on_set_network_host(self, context, network_id):
        """Called when this host becomes the host for a project"""
        pass

    @classmethod
    def setup_compute_network(cls, network):
        """Sets up matching network for compute hosts"""
        raise NotImplementedError()

    def allocate_floating_ip(self, project_id, context=None):
        """Gets an floating ip from the pool"""
        # TODO(vish): add floating ips through manage command
        return db.floating_ip_allocate_address(context,
                                              FLAGS.node_name,
                                              project_id)

    def associate_floating_ip(self, floating_address, fixed_address,
                             context=None):
        """Associates an floating ip to a fixed ip"""
        db.floating_ip_fixed_ip_associate(context,
                                         floating_address,
                                         fixed_address)
        _driver.bind_floating_ip(floating_address)
        _driver.ensure_floating_forward(floating_address, fixed_address)

    def disassociate_floating_ip(self, floating_address, context=None):
        """Disassociates a floating ip"""
        fixed_address = db.floating_ip_disassociate(context,
                                                   floating_address)
        _driver.unbind_floating_ip(floating_address)
        _driver.remove_floating_forward(floating_address, fixed_address)

    def deallocate_floating_ip(self, floating_address, context=None):
        """Returns an floating ip to the pool"""
        db.floating_ip_deallocate(context, floating_address)


class FlatNetworkService(BaseNetworkService):
    """Basic network where no vlans are used"""

    @classmethod
    def setup_compute_network(cls, network):
        """Network is created manually"""
        pass

    def _on_set_network_host(self, context, network_id):
        """Called when this host becomes the host for a project"""
        # NOTE(vish): should there be two types of network objects
        #             in the database?
        net = {}
        net['injected'] = True
        net['kind'] = FLAGS.network_type
        net['network_str']=FLAGS.flat_network_network
        net['netmask']=FLAGS.flat_network_netmask
        net['bridge']=FLAGS.flat_network_bridge
        net['gateway']=FLAGS.flat_network_gateway
        net['broadcast']=FLAGS.flat_network_broadcast
        net['dns']=FLAGS.flat_network_dns
        db.network_update(context, network_id, net)
        # TODO(vish): add public ips from flags to the datastore

class VlanNetworkService(BaseNetworkService):
    """Vlan network with dhcp"""
    def __init__(self, *args, **kwargs):
        super(VlanNetworkService, self).__init__(*args, **kwargs)
        # NOTE(vish): this should probably be removed and added via
        #             admin command or fixtures
        db.network_ensure_indexes(None, FLAGS.num_networks)

    def setup_fixed_ip(self, project_id, instance_id, context=None,
                       *args, **kwargs):
        """Gets a fixed ip from the pool"""
        network_ref = db.project_get_network(context, project_id)
        if db.instance_is_vpn(context, instance_id):
            address = db.network_get_vpn_ip_address(context,
                                                    network_ref['id'])
            logging.debug("Allocating vpn IP %s", address)
            db.fixed_ip_instance_associate(context,
                                           address,
                                           instance_id)
            _driver.ensure_vlan_forward(network_ref['vpn_public_ip_str'],
                                        network_ref['vpn_public_port'],
                                        network_ref['vpn_private_ip_str'])
        else:
            parent = super(VlanNetworkService, self)
            address = parent.allocate_fixed_ip(project_id,
                                               instance_id,
                                               context)
        return address

    def deallocate_fixed_ip(self, address, context=None):
        """Returns an ip to the pool"""
        fixed_ip_ref = db.fixed_ip_get_by_address(context, address)
        if fixed_ip_ref['leased']:
            logging.debug("Deallocating IP %s", address)
            db.fixed_ip_deallocate(context, address)
            # NOTE(vish): we keep instance id until release occurs
        else:
            self.release_fixed_ip(address, context)

    def lease_fixed_ip(self, address, context=None):
        """Called by bridge when ip is leased"""
        logging.debug("Leasing IP %s", address)
        db.fixed_ip_lease(context, address)

    def release_fixed_ip(self, address, context=None):
        """Called by bridge when ip is released"""
        logging.debug("Releasing IP %s", address)
        db.fixed_ip_release(context, address)
        db.fixed_ip_instance_disassociate(context, address)

    def restart_nets(self):
        """Ensure the network for each user is enabled"""
        # FIXME
        pass

    def _on_set_network_host(self, context, network_id):
        """Called when this host becomes the host for a project"""
        network_ref = db.network_get(network_id)
        _driver.ensure_vlan_bridge(network_ref['vlan'],
                                   network_ref['bridge'])


    @classmethod
    def setup_compute_network(cls, network):
        """Sets up matching network for compute hosts"""
        _driver.ensure_vlan_bridge(network.vlan, network.bridge)
