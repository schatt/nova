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

import contextlib
import copy

import mock
from oslo_serialization import jsonutils
from oslo_utils import units

from nova.compute import claims
from nova.compute import flavors
from nova.compute import power_state
from nova.compute import resource_tracker
from nova.compute import task_states
from nova.compute import vm_states
from nova import exception as exc
from nova import objects
from nova import test

_VIRT_DRIVER_AVAIL_RESOURCES = {
    'vcpus': 4,
    'memory_mb': 512,
    'local_gb': 6,
    'vcpus_used': 0,
    'memory_mb_used': 0,
    'local_gb_used': 0,
    'hypervisor_type': 'fake',
    'hypervisor_version': 0,
    'hypervisor_hostname': 'fakehost',
    'cpu_info': '',
    'numa_topology': None,
}

_COMPUTE_NODE_FIXTURES = [
    {
        'id': 1,
        # NOTE(jaypipes): Will be removed with the
        #                 detach-compute-node-from-service blueprint
        #                 implementation.
        'service_id': 1,
        'host': 'fake-host',
        'service': None,
        'vcpus': _VIRT_DRIVER_AVAIL_RESOURCES['vcpus'],
        'memory_mb': _VIRT_DRIVER_AVAIL_RESOURCES['memory_mb'],
        'local_gb': _VIRT_DRIVER_AVAIL_RESOURCES['local_gb'],
        'vcpus_used': _VIRT_DRIVER_AVAIL_RESOURCES['vcpus_used'],
        'memory_mb_used': _VIRT_DRIVER_AVAIL_RESOURCES['memory_mb_used'],
        'local_gb_used': _VIRT_DRIVER_AVAIL_RESOURCES['local_gb_used'],
        'hypervisor_type': 'fake',
        'hypervisor_version': 0,
        'hypervisor_hostname': 'fake-host',
        'free_ram_mb': (_VIRT_DRIVER_AVAIL_RESOURCES['memory_mb'] -
                        _VIRT_DRIVER_AVAIL_RESOURCES['memory_mb_used']),
        'free_disk_gb': (_VIRT_DRIVER_AVAIL_RESOURCES['local_gb'] -
                         _VIRT_DRIVER_AVAIL_RESOURCES['local_gb_used']),
        'current_workload': 0,
        'running_vms': 0,
        'cpu_info': '{}',
        'disk_available_least': 0,
        'host_ip': 'fake-ip',
        'supported_instances': None,
        'metrics': None,
        'pci_stats': None,
        'extra_resources': None,
        'stats': '{}',
        'numa_topology': None
    },
]

_SERVICE_FIXTURE = objects.Service(
    id=1,
    host='fake-host',
    binary='nova-compute',
    topic='compute',
    report_count=1,
    disabled=False,
    disabled_reason='')

_INSTANCE_TYPE_FIXTURES = {
    1: {
        'id': 1,
        'flavorid': 'fakeid-1',
        'name': 'fake1.small',
        'memory_mb': 128,
        'vcpus': 1,
        'root_gb': 1,
        'ephemeral_gb': 0,
        'swap': 0,
        'rxtx_factor': 0,
        'vcpu_weight': 1,
        'extra_specs': {},
    },
    2: {
        'id': 2,
        'flavorid': 'fakeid-2',
        'name': 'fake1.medium',
        'memory_mb': 256,
        'vcpus': 2,
        'root_gb': 5,
        'ephemeral_gb': 0,
        'swap': 0,
        'rxtx_factor': 0,
        'vcpu_weight': 1,
        'extra_specs': {},
    },
}


# A collection of system_metadata attributes that would exist in instances
# that have the instance type ID matching the dictionary key.
_INSTANCE_TYPE_SYS_META = {
    1: flavors.save_flavor_info({}, _INSTANCE_TYPE_FIXTURES[1]),
    2: flavors.save_flavor_info({}, _INSTANCE_TYPE_FIXTURES[2]),
}


_MIGRATION_SYS_META = flavors.save_flavor_info(
        {}, _INSTANCE_TYPE_FIXTURES[1], 'old_')
_MIGRATION_SYS_META = flavors.save_flavor_info(
        _MIGRATION_SYS_META, _INSTANCE_TYPE_FIXTURES[2], 'new_')

_2MB = 2 * units.Mi / units.Ki

_INSTANCE_NUMA_TOPOLOGIES = {
    '2mb': objects.InstanceNUMATopology(cells=[
        objects.InstanceNUMACell(
            id=0, cpuset=set([1]), memory=_2MB, pagesize=0),
        objects.InstanceNUMACell(
            id=1, cpuset=set([3]), memory=_2MB, pagesize=0)]),
}

_NUMA_LIMIT_TOPOLOGIES = {
    '2mb': objects.NUMATopologyLimits(id=0,
                                      cpu_allocation_ratio=1.0,
                                      ram_allocation_ratio=1.0),
}

_NUMA_PAGE_TOPOLOGIES = {
    '2kb*8': objects.NUMAPagesTopology(size_kb=2, total=8, used=0)
}

_NUMA_HOST_TOPOLOGIES = {
    '2mb': objects.NUMATopology(cells=[
        objects.NUMACell(id=0, cpuset=set([1, 2]), memory=_2MB,
                         cpu_usage=0, memory_usage=0,
                         mempages=[_NUMA_PAGE_TOPOLOGIES['2kb*8']],
                         siblings=[], pinned_cpus=set([])),
        objects.NUMACell(id=1, cpuset=set([3, 4]), memory=_2MB,
                         cpu_usage=0, memory_usage=0,
                         mempages=[_NUMA_PAGE_TOPOLOGIES['2kb*8']],
                         siblings=[], pinned_cpus=set([]))]),
}


_INSTANCE_FIXTURES = [
    objects.Instance(
        id=1,
        host=None,  # prevent RT trying to lazy-load this
        node=None,
        uuid='c17741a5-6f3d-44a8-ade8-773dc8c29124',
        memory_mb=_INSTANCE_TYPE_FIXTURES[1]['memory_mb'],
        vcpus=_INSTANCE_TYPE_FIXTURES[1]['vcpus'],
        root_gb=_INSTANCE_TYPE_FIXTURES[1]['root_gb'],
        ephemeral_gb=_INSTANCE_TYPE_FIXTURES[1]['ephemeral_gb'],
        numa_topology=_INSTANCE_NUMA_TOPOLOGIES['2mb'],
        instance_type_id=1,
        vm_state=vm_states.ACTIVE,
        power_state=power_state.RUNNING,
        task_state=None,
        os_type='fake-os',  # Used by the stats collector.
        project_id='fake-project',  # Used by the stats collector.
    ),
    objects.Instance(
        id=2,
        host=None,
        node=None,
        uuid='33805b54-dea6-47b8-acb2-22aeb1b57919',
        memory_mb=_INSTANCE_TYPE_FIXTURES[2]['memory_mb'],
        vcpus=_INSTANCE_TYPE_FIXTURES[2]['vcpus'],
        root_gb=_INSTANCE_TYPE_FIXTURES[2]['root_gb'],
        ephemeral_gb=_INSTANCE_TYPE_FIXTURES[2]['ephemeral_gb'],
        numa_topology=None,
        instance_type_id=2,
        vm_state=vm_states.DELETED,
        power_state=power_state.SHUTDOWN,
        task_state=None,
        os_type='fake-os',
        project_id='fake-project-2',
    ),
]

_MIGRATION_FIXTURES = {
    # A migration that has only this compute node as the source host
    'source-only': objects.Migration(
        id=1,
        instance_uuid='f15ecfb0-9bf6-42db-9837-706eb2c4bf08',
        source_compute='fake-host',
        dest_compute='other-host',
        source_node='fake-node',
        dest_node='other-node',
        old_instance_type_id=1,
        new_instance_type_id=2,
        status='migrating'
    ),
    # A migration that has only this compute node as the dest host
    'dest-only': objects.Migration(
        id=2,
        instance_uuid='f6ed631a-8645-4b12-8e1e-2fff55795765',
        source_compute='other-host',
        dest_compute='fake-host',
        source_node='other-node',
        dest_node='fake-node',
        old_instance_type_id=1,
        new_instance_type_id=2,
        status='migrating'
    ),
    # A migration that has this compute node as both the source and dest host
    'source-and-dest': objects.Migration(
        id=3,
        instance_uuid='f4f0bfea-fe7e-4264-b598-01cb13ef1997',
        source_compute='fake-host',
        dest_compute='fake-host',
        source_node='fake-node',
        dest_node='fake-node',
        old_instance_type_id=1,
        new_instance_type_id=2,
        status='migrating'
    ),
}

_MIGRATION_INSTANCE_FIXTURES = {
    # source-only
    'f15ecfb0-9bf6-42db-9837-706eb2c4bf08': objects.Instance(
        id=101,
        host=None,  # prevent RT trying to lazy-load this
        node=None,
        uuid='f15ecfb0-9bf6-42db-9837-706eb2c4bf08',
        memory_mb=_INSTANCE_TYPE_FIXTURES[1]['memory_mb'],
        vcpus=_INSTANCE_TYPE_FIXTURES[1]['vcpus'],
        root_gb=_INSTANCE_TYPE_FIXTURES[1]['root_gb'],
        ephemeral_gb=_INSTANCE_TYPE_FIXTURES[1]['ephemeral_gb'],
        numa_topology=None,
        instance_type_id=1,
        vm_state=vm_states.ACTIVE,
        power_state=power_state.RUNNING,
        task_state=task_states.RESIZE_MIGRATING,
        system_metadata=_MIGRATION_SYS_META,
        os_type='fake-os',
        project_id='fake-project',
    ),
    # dest-only
    'f6ed631a-8645-4b12-8e1e-2fff55795765': objects.Instance(
        id=102,
        host=None,  # prevent RT trying to lazy-load this
        node=None,
        uuid='f6ed631a-8645-4b12-8e1e-2fff55795765',
        memory_mb=_INSTANCE_TYPE_FIXTURES[2]['memory_mb'],
        vcpus=_INSTANCE_TYPE_FIXTURES[2]['vcpus'],
        root_gb=_INSTANCE_TYPE_FIXTURES[2]['root_gb'],
        ephemeral_gb=_INSTANCE_TYPE_FIXTURES[2]['ephemeral_gb'],
        numa_topology=None,
        instance_type_id=2,
        vm_state=vm_states.ACTIVE,
        power_state=power_state.RUNNING,
        task_state=task_states.RESIZE_MIGRATING,
        system_metadata=_MIGRATION_SYS_META,
        os_type='fake-os',
        project_id='fake-project',
    ),
    # source-and-dest
    'f4f0bfea-fe7e-4264-b598-01cb13ef1997': objects.Instance(
        id=3,
        host=None,  # prevent RT trying to lazy-load this
        node=None,
        uuid='f4f0bfea-fe7e-4264-b598-01cb13ef1997',
        memory_mb=_INSTANCE_TYPE_FIXTURES[2]['memory_mb'],
        vcpus=_INSTANCE_TYPE_FIXTURES[2]['vcpus'],
        root_gb=_INSTANCE_TYPE_FIXTURES[2]['root_gb'],
        ephemeral_gb=_INSTANCE_TYPE_FIXTURES[2]['ephemeral_gb'],
        numa_topology=None,
        instance_type_id=2,
        vm_state=vm_states.ACTIVE,
        power_state=power_state.RUNNING,
        task_state=task_states.RESIZE_MIGRATING,
        system_metadata=_MIGRATION_SYS_META,
        os_type='fake-os',
        project_id='fake-project',
    ),
}


def overhead_zero(instance):
    # Emulate that the driver does not adjust the memory
    # of the instance...
    return {
        'memory_mb': 0
    }


def setup_rt(hostname, nodename, virt_resources=_VIRT_DRIVER_AVAIL_RESOURCES,
             estimate_overhead=overhead_zero):
    """Sets up the resource tracker instance with mock fixtures.

    :param virt_resources: Optional override of the resource representation
                           returned by the virt driver's
                           `get_available_resource()` method.
    :param estimate_overhead: Optional override of a function that should
                              return overhead of memory given an instance
                              object. Defaults to returning zero overhead.
    """
    cond_api_mock = mock.MagicMock()
    sched_client_mock = mock.MagicMock()
    notifier_mock = mock.MagicMock()
    vd = mock.MagicMock()
    # Make sure we don't change any global fixtures during tests
    virt_resources = copy.deepcopy(virt_resources)
    vd.get_available_resource.return_value = virt_resources
    vd.estimate_instance_overhead.side_effect = estimate_overhead

    with contextlib.nested(
            mock.patch('nova.conductor.API', return_value=cond_api_mock),
            mock.patch('nova.scheduler.client.SchedulerClient',
                       return_value=sched_client_mock),
            mock.patch('nova.rpc.get_notifier', return_value=notifier_mock)):
        rt = resource_tracker.ResourceTracker(hostname, vd, nodename)
    return (rt, sched_client_mock, vd)


class BaseTestCase(test.NoDBTestCase):

    def setUp(self):
        super(BaseTestCase, self).setUp()
        self.rt = None
        self.flags(my_ip='fake-ip')

    def _setup_rt(self, virt_resources=_VIRT_DRIVER_AVAIL_RESOURCES,
                  estimate_overhead=overhead_zero):
        (self.rt, self.sched_client_mock,
         self.driver_mock) = setup_rt(
                 'fake-host', 'fake-node', virt_resources, estimate_overhead)
        self.cond_api_mock = self.rt.conductor_api


class TestUpdateAvailableResources(BaseTestCase):

    def _update_available_resources(self):
        # We test RT._update separately, since the complexity
        # of the update_available_resource() function is high enough as
        # it is, we just want to focus here on testing the resources
        # parameter that update_available_resource() eventually passes
        # to _update().
        with mock.patch.object(self.rt, '_update') as update_mock:
            self.rt.update_available_resource(mock.sentinel.ctx)
        return update_mock

    @mock.patch('nova.objects.Service.get_by_compute_host')
    @mock.patch('nova.objects.ComputeNode.get_by_host_and_nodename')
    @mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_no_instances_no_migrations_no_reserved(self, get_mock, migr_mock,
                                                    get_cn_mock, service_mock):
        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)
        self._setup_rt()

        get_mock.return_value = []
        migr_mock.return_value = []
        get_cn_mock.return_value = _COMPUTE_NODE_FIXTURES[0]
        service_mock.return_value = _SERVICE_FIXTURE

        update_mock = self._update_available_resources()

        vd = self.driver_mock
        vd.get_available_resource.assert_called_once_with('fake-node')
        get_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                         'fake-node',
                                         expected_attrs=[
                                             'system_metadata',
                                             'numa_topology'])
        get_cn_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                            'fake-node')
        migr_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                          'fake-node')

        expected_resources = {
            # host is added in update_available_resources()
            # before calling _update()
            'host': 'fake-host',
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 6,
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 512,
            'memory_mb_used': 0,
            'pci_device_pools': [],
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 0,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 0
        }
        update_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)

    @mock.patch('nova.objects.Service.get_by_compute_host')
    @mock.patch('nova.objects.ComputeNode.get_by_host_and_nodename')
    @mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_no_instances_no_migrations_reserved_disk_and_ram(
            self, get_mock, migr_mock, get_cn_mock, service_mock):
        self.flags(reserved_host_disk_mb=1024,
                   reserved_host_memory_mb=512)
        self._setup_rt()

        get_mock.return_value = []
        migr_mock.return_value = []
        get_cn_mock.return_value = _COMPUTE_NODE_FIXTURES[0]
        service_mock.return_value = _SERVICE_FIXTURE

        update_mock = self._update_available_resources()

        get_cn_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                            'fake-node')
        expected_resources = {
            # host is added in update_available_resources()
            # before calling _update()
            'host': 'fake-host',
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 5,  # 6GB avail - 1 GB reserved
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 0,  # 512MB avail - 512MB reserved
            'memory_mb_used': 512,  # 0MB used + 512MB reserved
            'pci_device_pools': [],
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 1,  # 0GB used + 1 GB reserved
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 0
        }
        update_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)

    @mock.patch('nova.objects.Service.get_by_compute_host')
    @mock.patch('nova.objects.ComputeNode.get_by_host_and_nodename')
    @mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_some_instances_no_migrations(self, get_mock, migr_mock,
                                          get_cn_mock, service_mock):
        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)
        self._setup_rt()

        get_mock.return_value = _INSTANCE_FIXTURES
        migr_mock.return_value = []
        get_cn_mock.return_value = _COMPUTE_NODE_FIXTURES[0]
        service_mock.return_value = _SERVICE_FIXTURE

        update_mock = self._update_available_resources()

        get_cn_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                            'fake-node')
        expected_resources = {
            # host is added in update_available_resources()
            # before calling _update()
            'host': 'fake-host',
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 5,  # 6 - 1 used
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 384,  # 512 - 128 used
            'memory_mb_used': 128,
            'pci_device_pools': [],
            # NOTE(jaypipes): Due to the design of the ERT, which now is used
            #                 track VCPUs, the actual used VCPUs isn't
            #                 "written" to the resources dictionary that is
            #                 passed to _update() like all the other
            #                 resources are. Instead, _update()
            #                 calls the ERT's write_resources() method, which
            #                 then queries each resource handler plugin for the
            #                 changes in its resource usage and the plugin
            #                 writes changes to the supplied "values" dict. For
            #                 this reason, all other resources except VCPUs
            #                 are accurate here. :(
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 1,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 1  # One active instance
        }
        update_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)

    @mock.patch('nova.objects.Service.get_by_compute_host')
    @mock.patch('nova.objects.ComputeNode.get_by_host_and_nodename')
    @mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_orphaned_instances_no_migrations(self, get_mock, migr_mock,
                                              get_cn_mock, service_mock):
        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)
        self._setup_rt()

        get_mock.return_value = []
        migr_mock.return_value = []
        get_cn_mock.return_value = _COMPUTE_NODE_FIXTURES[0]
        service_mock.return_value = _SERVICE_FIXTURE

        # Orphaned instances are those that the virt driver has on
        # record as consuming resources on the compute node, but the
        # Nova database has no record of the instance being active
        # on the host. For some reason, the resource tracker only
        # considers orphaned instance's memory usage in its calculations
        # of free resources...
        orphaned_usages = {
            '71ed7ef6-9d2e-4c65-9f4e-90bb6b76261d': {
                # Yes, the return result format of get_per_instance_usage
                # is indeed this stupid and redundant. Also note that the
                # libvirt driver just returns an empty dict always for this
                # method and so who the heck knows whether this stuff
                # actually works.
                'uuid': '71ed7ef6-9d2e-4c65-9f4e-90bb6b76261d',
                'memory_mb': 64
            }
        }
        vd = self.driver_mock
        vd.get_per_instance_usage.return_value = orphaned_usages

        update_mock = self._update_available_resources()

        get_cn_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                            'fake-node')
        expected_resources = {
            # host is added in update_available_resources()
            # before calling _update()
            'host': 'fake-host',
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 6,
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 448,  # 512 - 64 orphaned usage
            'memory_mb_used': 64,
            'pci_device_pools': [],
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 0,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            # Yep, for some reason, orphaned instances are not counted
            # as running VMs...
            'running_vms': 0
        }
        update_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)

    @mock.patch('nova.objects.Service.get_by_compute_host')
    @mock.patch('nova.objects.ComputeNode.get_by_host_and_nodename')
    @mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
    @mock.patch('nova.objects.Instance.get_by_uuid')
    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_no_instances_source_migration(self, get_mock, get_inst_mock,
                                           migr_mock, get_cn_mock,
                                           service_mock):
        # We test the behavior of update_available_resource() when
        # there is an active migration that involves this compute node
        # as the source host not the destination host, and the resource
        # tracker does not have any instances assigned to it. This is
        # the case when a migration from this compute host to another
        # has been completed, but the user has not confirmed the resize
        # yet, so the resource tracker must continue to keep the resources
        # for the original instance type available on the source compute
        # node in case of a revert of the resize.
        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)
        self._setup_rt()

        get_mock.return_value = []
        migr_obj = _MIGRATION_FIXTURES['source-only']
        migr_mock.return_value = [migr_obj]
        get_cn_mock.return_value = _COMPUTE_NODE_FIXTURES[0]
        service_mock.return_value = _SERVICE_FIXTURE
        # Migration.instance property is accessed in the migration
        # processing code, and this property calls
        # objects.Instance.get_by_uuid, so we have the migration return
        inst_uuid = migr_obj.instance_uuid
        get_inst_mock.return_value = _MIGRATION_INSTANCE_FIXTURES[inst_uuid]

        update_mock = self._update_available_resources()

        get_cn_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                            'fake-node')
        expected_resources = {
            # host is added in update_available_resources()
            # before calling _update()
            'host': 'fake-host',
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 5,
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 384,  # 512 total - 128 for possible revert of orig
            'memory_mb_used': 128,  # 128 possible revert amount
            'pci_device_pools': [],
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 1,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 0
        }
        update_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)

    @mock.patch('nova.objects.Service.get_by_compute_host')
    @mock.patch('nova.objects.ComputeNode.get_by_host_and_nodename')
    @mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
    @mock.patch('nova.objects.Instance.get_by_uuid')
    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_no_instances_dest_migration(self, get_mock, get_inst_mock,
                                         migr_mock, get_cn_mock, service_mock):
        # We test the behavior of update_available_resource() when
        # there is an active migration that involves this compute node
        # as the destination host not the source host, and the resource
        # tracker does not yet have any instances assigned to it. This is
        # the case when a migration to this compute host from another host
        # is in progress, but the user has not confirmed the resize
        # yet, so the resource tracker must reserve the resources
        # for the possibly-to-be-confirmed instance's instance type
        # node in case of a confirm of the resize.
        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)
        self._setup_rt()

        get_mock.return_value = []
        migr_obj = _MIGRATION_FIXTURES['dest-only']
        migr_mock.return_value = [migr_obj]
        inst_uuid = migr_obj.instance_uuid
        get_inst_mock.return_value = _MIGRATION_INSTANCE_FIXTURES[inst_uuid]
        get_cn_mock.return_value = _COMPUTE_NODE_FIXTURES[0]
        service_mock.return_value = _SERVICE_FIXTURE

        update_mock = self._update_available_resources()

        get_cn_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                            'fake-node')
        expected_resources = {
            # host is added in update_available_resources()
            # before calling _update()
            'host': 'fake-host',
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 1,
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 256,  # 512 total - 256 for possible confirm of new
            'memory_mb_used': 256,  # 256 possible confirmed amount
            'pci_device_pools': [],
            'vcpus_used': 0,  # See NOTE(jaypipes) above about why this is 0
            'hypervisor_type': 'fake',
            'local_gb_used': 5,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 0
        }
        update_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)

    @mock.patch('nova.objects.Service.get_by_compute_host')
    @mock.patch('nova.objects.ComputeNode.get_by_host_and_nodename')
    @mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
    @mock.patch('nova.objects.Instance.get_by_uuid')
    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_some_instances_source_and_dest_migration(self, get_mock,
                                                      get_inst_mock, migr_mock,
                                                      get_cn_mock,
                                                      service_mock):
        # We test the behavior of update_available_resource() when
        # there is an active migration that involves this compute node
        # as the destination host AND the source host, and the resource
        # tracker has a few instances assigned to it, including the
        # instance that is resizing to this same compute node. The tracking
        # of resource amounts takes into account both the old and new
        # resize instance types as taking up space on the node.
        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)
        self._setup_rt()

        migr_obj = _MIGRATION_FIXTURES['source-and-dest']
        migr_mock.return_value = [migr_obj]
        service_mock.return_value = _SERVICE_FIXTURE
        inst_uuid = migr_obj.instance_uuid
        # The resizing instance has already had its instance type
        # changed to the *new* instance type (the bigger one, instance type 2)
        resizing_instance = _MIGRATION_INSTANCE_FIXTURES[inst_uuid]
        all_instances = _INSTANCE_FIXTURES + [resizing_instance]
        get_mock.return_value = all_instances
        get_inst_mock.return_value = resizing_instance
        get_cn_mock.return_value = _COMPUTE_NODE_FIXTURES[0]

        update_mock = self._update_available_resources()

        get_cn_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                            'fake-node')
        expected_resources = {
            # host is added in update_available_resources()
            # before calling _update()
            'host': 'fake-host',
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            # 6 total - 1G existing - 5G new flav - 1G old flav
            'free_disk_gb': -1,
            'hypervisor_version': 0,
            'local_gb': 6,
            # 512 total - 128 existing - 256 new flav - 128 old flav
            'free_ram_mb': 0,
            'memory_mb_used': 512,  # 128 exist + 256 new flav + 128 old flav
            'pci_device_pools': [],
            # See NOTE(jaypipes) above for reason why this isn't accurate until
            # _update() is called.
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 7,  # 1G existing, 5G new flav + 1 old flav
            'memory_mb': 512,
            'current_workload': 1,  # One migrating instance...
            'vcpus': 4,
            'running_vms': 2
        }
        update_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)


class TestInitComputeNode(BaseTestCase):

    @mock.patch('nova.objects.ComputeNode.get_by_host_and_nodename')
    def test_no_op_init_compute_node(self, get_mock):
        self._setup_rt()

        capi = self.cond_api_mock
        service_mock = capi.service_get_by_compute_host
        create_mock = capi.compute_node_create
        resources = copy.deepcopy(_VIRT_DRIVER_AVAIL_RESOURCES)
        compute_node = copy.deepcopy(_COMPUTE_NODE_FIXTURES[0])
        self.rt.compute_node = compute_node

        self.rt._init_compute_node(mock.sentinel.ctx, resources)

        self.assertFalse(service_mock.called)
        self.assertFalse(get_mock.called)
        self.assertFalse(create_mock.called)
        self.assertFalse(self.rt.disabled)

    @mock.patch('nova.objects.Service.get_by_compute_host')
    def test_no_found_service_disabled(self, service_mock):
        self._setup_rt()

        service_mock.side_effect = exc.NotFound
        resources = copy.deepcopy(_VIRT_DRIVER_AVAIL_RESOURCES)

        self.rt._init_compute_node(mock.sentinel.ctx, resources)

        self.assertTrue(self.rt.disabled)
        self.assertIsNone(self.rt.compute_node)

    @mock.patch('nova.objects.Service.get_by_compute_host')
    @mock.patch('nova.objects.ComputeNode.get_by_host_and_nodename')
    def test_compute_node_loaded(self, get_mock, service_mock):
        self._setup_rt()

        def fake_get_node(_ctx, host, node):
            res = copy.deepcopy(_COMPUTE_NODE_FIXTURES[0])
            return res

        capi = self.cond_api_mock
        service_mock.return_value = _SERVICE_FIXTURE
        get_mock.side_effect = fake_get_node
        create_mock = capi.compute_node_create
        resources = copy.deepcopy(_VIRT_DRIVER_AVAIL_RESOURCES)

        self.rt._init_compute_node(mock.sentinel.ctx, resources)

        service_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host')
        get_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                         'fake-node')
        self.assertFalse(create_mock.called)
        self.assertFalse(self.rt.disabled)

    @mock.patch('nova.objects.Service.get_by_compute_host')
    @mock.patch('nova.objects.ComputeNode.get_by_host_and_nodename')
    def test_compute_node_created_on_empty(self, get_mock, service_mock):
        self._setup_rt()

        def fake_create_node(_ctx, resources):
            res = copy.deepcopy(_COMPUTE_NODE_FIXTURES[0])
            res.update(resources)
            return res

        capi = self.cond_api_mock
        create_node_mock = capi.compute_node_create
        create_node_mock.side_effect = fake_create_node
        service_obj = _SERVICE_FIXTURE
        service_mock.return_value = service_obj
        get_mock.side_effect = exc.NotFound

        resources = {
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 6,
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 512,
            'memory_mb_used': 0,
            'pci_device_pools': [],
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 0,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 0,
            'pci_passthrough_devices': '[]'
        }
        # We need to do this because _update() actually modifies
        # the supplied dictionary :(
        expected_resources = copy.deepcopy(resources)
        # NOTE(pmurray): This will go away when the ComputeNode object is used
        expected_resources['stats'] = '{}'
        # NOTE(pmurray): no intial values are calculated before the initial
        # creation. vcpus is derived from ERT resources, so this means its
        # value will be 0
        expected_resources['vcpus'] = 0
        # NOTE(jaypipes): This will go away once
        #                 detach-compute-node-from-service blueprint is done
        expected_resources['service_id'] = 1
        # NOTE(sbauza): ResourceTracker adds host field
        expected_resources['host'] = 'fake-host'
        # pci_passthrough_devices should is not held in compute nodes
        del expected_resources['pci_passthrough_devices']

        self.rt._init_compute_node(mock.sentinel.ctx, resources)

        self.assertFalse(self.rt.disabled)
        service_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host')
        get_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                         'fake-node')
        create_node_mock.assert_called_once_with(mock.sentinel.ctx,
                                                 expected_resources)


class TestUpdateComputeNode(BaseTestCase):

    @mock.patch('nova.objects.Service.get_by_compute_host')
    def test_existing_compute_node_updated_same_resources(self, service_mock):
        self._setup_rt()
        self.rt.compute_node = copy.deepcopy(_COMPUTE_NODE_FIXTURES[0])

        capi = self.cond_api_mock
        create_node_mock = capi.compute_node_create

        # This is the same set of resources as the fixture, deliberately. We
        # are checking below to see that update_resource_stats() is not
        # needlessly called when the resources don't actually change.
        resources = {
            # host is added in update_available_resources()
            # before calling _update()
            'host': 'fake-host',
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 6,
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 512,
            'memory_mb_used': 0,
            'pci_device_pools': [],
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 0,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 0
        }
        orig_resources = copy.deepcopy(resources)
        self.rt._update(mock.sentinel.ctx, resources)

        self.assertFalse(self.rt.disabled)
        self.assertFalse(service_mock.called)
        self.assertFalse(create_node_mock.called)

        # The above call to _update() will populate the
        # RT.old_resources collection with the resources. Here, we check that
        # if we call _update() again with the same resources, that
        # the scheduler client won't be called again to update those
        # (unchanged) resources for the compute node
        self.sched_client_mock.reset_mock()
        urs_mock = self.sched_client_mock.update_resource_stats
        self.rt._update(mock.sentinel.ctx, orig_resources)
        self.assertFalse(urs_mock.called)

    @mock.patch('nova.objects.Service.get_by_compute_host')
    def test_existing_compute_node_updated_new_resources(self, service_mock):
        self._setup_rt()
        self.rt.compute_node = copy.deepcopy(_COMPUTE_NODE_FIXTURES[0])

        capi = self.cond_api_mock
        create_node_mock = capi.compute_node_create

        # Deliberately changing local_gb_used, vcpus_used, and memory_mb_used
        # below to be different from the compute node fixture's base usages.
        # We want to check that the code paths update the stored compute node
        # usage records with what is supplied to _update().
        resources = {
            # host is added in update_available_resources()
            # before calling _update()
            'host': 'fake-host',
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 2,
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 384,
            'memory_mb_used': 128,
            'pci_device_pools': [],
            'vcpus_used': 2,
            'hypervisor_type': 'fake',
            'local_gb_used': 4,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 0
        }
        expected_resources = copy.deepcopy(resources)
        expected_resources['id'] = 1
        expected_resources['stats'] = '{}'

        self.rt.ext_resources_handler.reset_resources(resources,
                                                      self.rt.driver)
        # This emulates the behavior that occurs in the
        # RT.update_available_resource() method, which updates resource
        # information in the ERT differently than all other resources.
        self.rt.ext_resources_handler.update_from_instance(dict(vcpus=2))
        self.rt._update(mock.sentinel.ctx, resources)

        self.assertFalse(self.rt.disabled)
        self.assertFalse(service_mock.called)
        self.assertFalse(create_node_mock.called)
        urs_mock = self.sched_client_mock.update_resource_stats
        urs_mock.assert_called_once_with(mock.sentinel.ctx,
                                         ('fake-host', 'fake-node'),
                                         expected_resources)


class TestInstanceClaim(BaseTestCase):

    def setUp(self):
        super(TestInstanceClaim, self).setUp()

        self._setup_rt()
        self.rt.compute_node = copy.deepcopy(_COMPUTE_NODE_FIXTURES[0])

        # not using mock.sentinel.ctx because instance_claim calls #elevated
        self.ctx = mock.MagicMock()
        self.elevated = mock.MagicMock()
        self.ctx.elevated.return_value = self.elevated

        self.instance = copy.deepcopy(_INSTANCE_FIXTURES[0])

    def assertEqualNUMAHostTopology(self, expected, got):
        attrs = ('cpuset', 'memory', 'id', 'cpu_usage', 'memory_usage')
        if None in (expected, got):
            if expected != got:
                raise AssertionError("Topologies don't match. Expected: "
                                     "%(expected)s, but got: %(got)s" %
                                     {'expected': expected, 'got': got})
            else:
                return

        if len(expected) != len(got):
            raise AssertionError("Topologies don't match due to different "
                                 "number of cells. Expected: "
                                 "%(expected)s, but got: %(got)s" %
                                 {'expected': expected, 'got': got})
        for exp_cell, got_cell in zip(expected.cells, got.cells):
            for attr in attrs:
                if getattr(exp_cell, attr) != getattr(got_cell, attr):
                    raise AssertionError("Topologies don't match. Expected: "
                                         "%(expected)s, but got: %(got)s" %
                                         {'expected': expected, 'got': got})

    def test_claim_disabled(self):
        self.rt.compute_node = None
        self.assertTrue(self.rt.disabled)

        claim = self.rt.instance_claim(mock.sentinel.ctx, self.instance, None)

        self.assertEqual(self.rt.host, self.instance.host)
        self.assertEqual(self.rt.host, self.instance.launched_on)
        self.assertEqual(self.rt.nodename, self.instance.node)
        self.assertIsInstance(claim, claims.NopClaim)

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid')
    @mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
    def test_claim(self, migr_mock, pci_mock):
        self.assertFalse(self.rt.disabled)

        pci_mock.return_value = objects.InstancePCIRequests(requests=[])

        disk_used = self.instance.root_gb + self.instance.ephemeral_gb
        expected = copy.deepcopy(_COMPUTE_NODE_FIXTURES[0])
        expected.update({
            'local_gb_used': disk_used,
            'memory_mb_used': self.instance.memory_mb,
            'free_disk_gb': expected['local_gb'] - disk_used,
            "free_ram_mb": expected['memory_mb'] - self.instance.memory_mb,
            'running_vms': 1,
            # 'vcpus_used': 0,  # vcpus are not claimed
            'pci_device_pools': [],
        })
        with mock.patch.object(self.rt, '_update') as update_mock:
            self.rt.instance_claim(self.ctx, self.instance, None)
            update_mock.assert_called_once_with(self.elevated, expected)

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid')
    @mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
    def test_claim_limits(self, migr_mock, pci_mock):
        self.assertFalse(self.rt.disabled)

        pci_mock.return_value = objects.InstancePCIRequests(requests=[])

        good_limits = {
            'memory_mb': _COMPUTE_NODE_FIXTURES[0]['memory_mb'],
            'disk_gb': _COMPUTE_NODE_FIXTURES[0]['local_gb'],
            'vcpu': _COMPUTE_NODE_FIXTURES[0]['vcpus'],
        }
        for key in good_limits.keys():
            bad_limits = copy.deepcopy(good_limits)
            bad_limits[key] = 0

            self.assertRaises(exc.ComputeResourcesUnavailable,
                    self.rt.instance_claim,
                    self.ctx, self.instance, bad_limits)

    @mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid')
    @mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
    def test_claim_numa(self, migr_mock, pci_mock):
        self.assertFalse(self.rt.disabled)

        pci_mock.return_value = objects.InstancePCIRequests(requests=[])

        self.instance.numa_topology = _INSTANCE_NUMA_TOPOLOGIES['2mb']
        host_topology = _NUMA_HOST_TOPOLOGIES['2mb']
        self.rt.compute_node['numa_topology'] = host_topology._to_json()
        limits = {'numa_topology': _NUMA_LIMIT_TOPOLOGIES['2mb']}

        expected_numa = copy.deepcopy(host_topology)
        for cell in expected_numa.cells:
            cell.memory_usage += _2MB
            cell.cpu_usage += 1
        with mock.patch.object(self.rt, '_update') as update_mock:
            self.rt.instance_claim(self.ctx, self.instance, limits)
            self.assertTrue(update_mock.called)
            updated_compute_node = update_mock.call_args[0][1]
            new_numa = updated_compute_node['numa_topology']
            new_numa = objects.NUMATopology.obj_from_db_obj(new_numa)
            self.assertEqualNUMAHostTopology(expected_numa, new_numa)


@mock.patch('nova.objects.MigrationList.get_in_progress_by_host_and_node')
@mock.patch('nova.objects.Instance.get_by_uuid')
@mock.patch('nova.objects.InstanceList.get_by_host_and_node')
@mock.patch('nova.objects.InstancePCIRequests.get_by_instance_uuid')
class TestResizeClaim(BaseTestCase):
    def setUp(self):
        super(TestResizeClaim, self).setUp()

        self._setup_rt()
        self.rt.compute_node = copy.deepcopy(_COMPUTE_NODE_FIXTURES[0])

        self.instance = copy.deepcopy(_INSTANCE_FIXTURES[0])
        self.instance.system_metadata = _INSTANCE_TYPE_SYS_META[1]
        self.flavor = _INSTANCE_TYPE_FIXTURES[1]
        self.limits = {}

        # not using mock.sentinel.ctx because resize_claim calls #elevated
        self.ctx = mock.MagicMock()
        self.elevated = mock.MagicMock()
        self.ctx.elevated.return_value = self.elevated

        # Initialise extensible resource trackers
        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        with contextlib.nested(
            mock.patch('nova.objects.InstanceList.get_by_host_and_node'),
            mock.patch('nova.objects.MigrationList.'
                       'get_in_progress_by_host_and_node')
        ) as (inst_list_mock, migr_mock):
            inst_list_mock.return_value = objects.InstanceList(objects=[])
            migr_mock.return_value = objects.MigrationList(objects=[])
            self.rt.update_available_resource(self.ctx)

    def register_mocks(self, pci_mock, inst_list_mock, inst_by_uuid,
            migr_mock):
        pci_mock.return_value = objects.InstancePCIRequests(requests=[])
        self.inst_list_mock = inst_list_mock
        self.inst_by_uuid = inst_by_uuid
        self.migr_mock = migr_mock

    def audit(self, rt, instances, migrations, migr_inst):
        self.inst_list_mock.return_value = \
                objects.InstanceList(objects=instances)
        self.migr_mock.return_value = \
                objects.MigrationList(objects=migrations)
        self.inst_by_uuid.return_value = migr_inst
        rt.update_available_resource(self.ctx)

    def assertEqual(self, expected, actual):
        if type(expected) != dict or type(actual) != dict:
            super(TestResizeClaim, self).assertEqual(expected, actual)
            return
        fail = False
        for k, e in expected.items():
            a = actual[k]
            if e != a:
                print("%s: %s != %s" % (k, e, a))
                fail = True
        if fail:
            self.fail()

    def adjust_expected(self, expected, flavor):
        disk_used = flavor['root_gb'] + flavor['ephemeral_gb']
        expected['free_disk_gb'] -= disk_used
        expected['local_gb_used'] += disk_used
        expected['free_ram_mb'] -= flavor['memory_mb']
        expected['memory_mb_used'] += flavor['memory_mb']
        expected['vcpus_used'] += flavor['vcpus']

    @mock.patch('nova.objects.Flavor.get_by_id')
    def test_claim(self, flavor_mock, pci_mock, inst_list_mock, inst_by_uuid,
            migr_mock):
        """Resize self.instance and check that the expected quantities of each
        resource have been consumed.
        """

        self.register_mocks(pci_mock, inst_list_mock, inst_by_uuid, migr_mock)
        self.driver_mock.get_host_ip_addr.return_value = "fake-ip"
        flavor_mock.return_value = objects.Flavor(**self.flavor)

        expected = copy.deepcopy(self.rt.compute_node)
        self.adjust_expected(expected, self.flavor)

        with mock.patch.object(self.rt, '_create_migration') as migr_mock:
            migr_mock.return_value = _MIGRATION_FIXTURES['source-only']
            claim = self.rt.resize_claim(
                self.ctx, self.instance, self.flavor, None)

        self.assertIsInstance(claim, claims.ResizeClaim)
        self.assertEqual(expected, self.rt.compute_node)

    def test_same_host(self, pci_mock, inst_list_mock, inst_by_uuid,
            migr_mock):
        """Resize self.instance to the same host but with a different flavor.
        Then abort the claim. Check that the same amount of resources are
        available afterwards as we started with.
        """

        self.register_mocks(pci_mock, inst_list_mock, inst_by_uuid, migr_mock)
        migr_obj = _MIGRATION_FIXTURES['source-and-dest']
        self.instance = _MIGRATION_INSTANCE_FIXTURES[migr_obj['instance_uuid']]

        self.rt.instance_claim(self.ctx, self.instance, None)
        expected = copy.deepcopy(self.rt.compute_node)

        with mock.patch.object(self.rt, '_create_migration') as migr_mock:
            migr_mock.return_value = migr_obj
            claim = self.rt.resize_claim(self.ctx, self.instance,
                    _INSTANCE_TYPE_FIXTURES[1], None)

        self.audit(self.rt, [self.instance], [migr_obj], self.instance)
        self.assertNotEqual(expected, self.rt.compute_node)

        claim.abort()
        self.assertEqual(expected, self.rt.compute_node)

    def test_revert_reserve_source(
            self, pci_mock, inst_list_mock, inst_by_uuid, migr_mock):
        """Check that the source node of an instance migration reserves
        resources until the migration has completed, even if the migration is
        reverted.
        """

        self.register_mocks(pci_mock, inst_list_mock, inst_by_uuid, migr_mock)

        # Get our migrations, instances and itypes in a row
        src_migr = _MIGRATION_FIXTURES['source-only']
        src_instance = _MIGRATION_INSTANCE_FIXTURES[src_migr['instance_uuid']]
        old_itype = _INSTANCE_TYPE_FIXTURES[src_migr['old_instance_type_id']]
        dst_migr = _MIGRATION_FIXTURES['dest-only']
        dst_instance = _MIGRATION_INSTANCE_FIXTURES[dst_migr['instance_uuid']]
        new_itype = _INSTANCE_TYPE_FIXTURES[dst_migr['new_instance_type_id']]

        # Set up the destination resource tracker
        # update_available_resource to initialise extensible resource trackers
        src_rt = self.rt
        (dst_rt, _, _) = setup_rt("other-host", "other-node")
        dst_rt.compute_node = copy.deepcopy(_COMPUTE_NODE_FIXTURES[0])
        inst_list_mock.return_value = objects.InstanceList(objects=[])
        dst_rt.update_available_resource(self.ctx)

        # Register the instance with dst_rt
        expected = copy.deepcopy(dst_rt.compute_node)
        del expected['stats']
        dst_rt.instance_claim(self.ctx, dst_instance)
        self.adjust_expected(expected, new_itype)
        expected_stats = {'num_task_resize_migrating': 1,
                             'io_workload': 1,
                             'num_instances': 1,
                             'num_proj_fake-project': 1,
                             'num_vm_active': 1,
                             'num_os_type_fake-os': 1}
        expected['current_workload'] = 1
        expected['running_vms'] = 1
        actual_stats = dst_rt.compute_node.pop('stats')
        actual_stats = jsonutils.loads(actual_stats)
        self.assertEqual(expected_stats, actual_stats)
        self.assertEqual(expected, dst_rt.compute_node)

        # Provide the migration via a mock, then audit dst_rt to check that
        # the instance + migration resources are not double-counted
        self.audit(dst_rt, [dst_instance], [dst_migr], dst_instance)
        actual_stats = dst_rt.compute_node.pop('stats')
        actual_stats = jsonutils.loads(actual_stats)
        self.assertEqual(expected_stats, actual_stats)
        self.assertEqual(expected, dst_rt.compute_node)

        # Audit src_rt with src_migr
        expected = copy.deepcopy(src_rt.compute_node)
        self.adjust_expected(expected, old_itype)
        self.audit(src_rt, [], [src_migr], src_instance)
        self.assertEqual(expected, src_rt.compute_node)

        # Flag the instance as reverting and re-audit
        src_instance['vm_state'] = vm_states.RESIZED
        src_instance['task_state'] = task_states.RESIZE_REVERTING
        self.audit(src_rt, [], [src_migr], src_instance)
        self.assertEqual(expected, src_rt.compute_node)

    def test_dupe_filter(self, pci_mock, inst_list_mock, inst_by_uuid,
            migr_mock):
        self.register_mocks(pci_mock, inst_list_mock, inst_by_uuid, migr_mock)

        migr_obj = _MIGRATION_FIXTURES['source-and-dest']
        # This is good enough to prevent a lazy-load; value is unimportant
        migr_obj['updated_at'] = None
        self.instance = _MIGRATION_INSTANCE_FIXTURES[migr_obj['instance_uuid']]
        self.audit(self.rt, [], [migr_obj, migr_obj], self.instance)
        self.assertEqual(1, len(self.rt.tracked_migrations))
