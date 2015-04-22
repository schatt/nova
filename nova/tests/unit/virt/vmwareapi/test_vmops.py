# Copyright 2013 OpenStack Foundation
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
import contextlib

import mock
from oslo_utils import units
from oslo_utils import uuidutils
from oslo_vmware import exceptions as vexc
from oslo_vmware.objects import datastore as ds_obj
from oslo_vmware import vim_util as vutil

from nova.compute import power_state
from nova import context
from nova import exception
from nova.network import model as network_model
from nova import objects
from nova import test
from nova.tests.unit import fake_instance
import nova.tests.unit.image.fake
from nova.tests.unit.virt.vmwareapi import fake as vmwareapi_fake
from nova.tests.unit.virt.vmwareapi import stubs
from nova.virt import hardware
from nova.virt.vmwareapi import constants
from nova.virt.vmwareapi import driver
from nova.virt.vmwareapi import ds_util
from nova.virt.vmwareapi import images
from nova.virt.vmwareapi import vim_util
from nova.virt.vmwareapi import vm_util
from nova.virt.vmwareapi import vmops


class DsPathMatcher(object):
    def __init__(self, expected_ds_path_str):
        self.expected_ds_path_str = expected_ds_path_str

    def __eq__(self, ds_path_param):
        return str(ds_path_param) == self.expected_ds_path_str


class VMwareVMOpsTestCase(test.NoDBTestCase):
    def setUp(self):
        super(VMwareVMOpsTestCase, self).setUp()
        vmwareapi_fake.reset()
        stubs.set_stubs(self.stubs)
        self.flags(image_cache_subdirectory_name='vmware_base',
                   my_ip='',
                   flat_injected=True,
                   vnc_enabled=True)
        self._context = context.RequestContext('fake_user', 'fake_project')
        self._session = driver.VMwareAPISession()

        self._virtapi = mock.Mock()
        self._image_id = nova.tests.unit.image.fake.get_valid_image_id()
        fake_ds_ref = vmwareapi_fake.ManagedObjectReference('fake-ds')
        self._ds = ds_obj.Datastore(
                ref=fake_ds_ref, name='fake_ds',
                capacity=10 * units.Gi,
                freespace=10 * units.Gi)
        self._dc_info = vmops.DcInfo(
                ref='fake_dc_ref', name='fake_dc',
                vmFolder='fake_vm_folder')
        cluster = vmwareapi_fake.create_cluster('fake_cluster', fake_ds_ref)
        self._instance_values = {
            'name': 'fake_name',
            'uuid': 'fake_uuid',
            'vcpus': 1,
            'memory_mb': 512,
            'image_ref': self._image_id,
            'root_gb': 10,
            'node': '%s(%s)' % (cluster.mo_id, cluster.name),
            'expected_attrs': ['system_metadata'],
        }
        self._instance = fake_instance.fake_instance_obj(
                                 self._context, **self._instance_values)
        self._flavor = objects.Flavor(name='m1.small', memory_mb=512, vcpus=1,
                                      root_gb=10, ephemeral_gb=0, swap=0,
                                      extra_specs={})
        self._instance.flavor = self._flavor

        self._vmops = vmops.VMwareVMOps(self._session, self._virtapi, None,
                                        cluster=cluster.obj)
        self._cluster = cluster

        subnet_4 = network_model.Subnet(cidr='192.168.0.1/24',
                                        dns=[network_model.IP('192.168.0.1')],
                                        gateway=
                                            network_model.IP('192.168.0.1'),
                                        ips=[
                                            network_model.IP('192.168.0.100')],
                                        routes=None)
        subnet_6 = network_model.Subnet(cidr='dead:beef::1/64',
                                        dns=None,
                                        gateway=
                                            network_model.IP('dead:beef::1'),
                                        ips=[network_model.IP(
                                            'dead:beef::dcad:beff:feef:0')],
                                        routes=None)
        network = network_model.Network(id=0,
                                        bridge='fa0',
                                        label='fake',
                                        subnets=[subnet_4, subnet_6],
                                        vlan=None,
                                        bridge_interface=None,
                                        injected=True)
        self._network_values = {
            'id': None,
            'address': 'DE:AD:BE:EF:00:00',
            'network': network,
            'type': None,
            'devname': None,
            'ovs_interfaceid': None,
            'rxtx_cap': 3
        }
        self.network_info = network_model.NetworkInfo([
                network_model.VIF(**self._network_values)
        ])
        pure_IPv6_network = network_model.Network(id=0,
                                        bridge='fa0',
                                        label='fake',
                                        subnets=[subnet_6],
                                        vlan=None,
                                        bridge_interface=None,
                                        injected=True)
        self.pure_IPv6_network_info = network_model.NetworkInfo([
                network_model.VIF(id=None,
                                  address='DE:AD:BE:EF:00:00',
                                  network=pure_IPv6_network,
                                  type=None,
                                  devname=None,
                                  ovs_interfaceid=None,
                                  rxtx_cap=3)
                ])

    def test_get_machine_id_str(self):
        result = vmops.VMwareVMOps._get_machine_id_str(self.network_info)
        self.assertEqual('DE:AD:BE:EF:00:00;192.168.0.100;255.255.255.0;'
                         '192.168.0.1;192.168.0.255;192.168.0.1#', result)
        result = vmops.VMwareVMOps._get_machine_id_str(
                                        self.pure_IPv6_network_info)
        self.assertEqual('DE:AD:BE:EF:00:00;;;;;#', result)

    def _setup_create_folder_mocks(self):
        ops = vmops.VMwareVMOps(mock.Mock(), mock.Mock(), mock.Mock())
        base_name = 'folder'
        ds_name = "datastore"
        ds_ref = mock.Mock()
        ds_ref.value = 1
        dc_ref = mock.Mock()
        ops._datastore_dc_mapping[ds_ref.value] = vmops.DcInfo(
                ref=dc_ref,
                name='fake-name',
                vmFolder='fake-folder')
        path = ds_obj.DatastorePath(ds_name, base_name)
        return ds_name, ds_ref, ops, path, dc_ref

    @mock.patch.object(ds_util, 'mkdir')
    def test_create_folder_if_missing(self, mock_mkdir):
        ds_name, ds_ref, ops, path, dc = self._setup_create_folder_mocks()
        ops._create_folder_if_missing(ds_name, ds_ref, 'folder')
        mock_mkdir.assert_called_with(ops._session, path, dc)

    @mock.patch.object(ds_util, 'mkdir')
    def test_create_folder_if_missing_exception(self, mock_mkdir):
        ds_name, ds_ref, ops, path, dc = self._setup_create_folder_mocks()
        ds_util.mkdir.side_effect = vexc.FileAlreadyExistsException()
        ops._create_folder_if_missing(ds_name, ds_ref, 'folder')
        mock_mkdir.assert_called_with(ops._session, path, dc)

    @mock.patch.object(vutil, 'continue_retrieval', return_value=None)
    def test_get_valid_vms_from_retrieve_result(self, _mock_cont):
        ops = vmops.VMwareVMOps(self._session, mock.Mock(), mock.Mock())
        fake_objects = vmwareapi_fake.FakeRetrieveResult()
        fake_objects.add_object(vmwareapi_fake.VirtualMachine(
            name=uuidutils.generate_uuid()))
        fake_objects.add_object(vmwareapi_fake.VirtualMachine(
            name=uuidutils.generate_uuid()))
        fake_objects.add_object(vmwareapi_fake.VirtualMachine(
            name=uuidutils.generate_uuid()))
        vms = ops._get_valid_vms_from_retrieve_result(fake_objects)
        self.assertEqual(3, len(vms))

    @mock.patch.object(vutil, 'continue_retrieval', return_value=None)
    def test_get_valid_vms_from_retrieve_result_with_invalid(self,
                                                             _mock_cont):
        ops = vmops.VMwareVMOps(self._session, mock.Mock(), mock.Mock())
        fake_objects = vmwareapi_fake.FakeRetrieveResult()
        fake_objects.add_object(vmwareapi_fake.VirtualMachine(
            name=uuidutils.generate_uuid()))
        invalid_vm1 = vmwareapi_fake.VirtualMachine(
            name=uuidutils.generate_uuid())
        invalid_vm1.set('runtime.connectionState', 'orphaned')
        invalid_vm2 = vmwareapi_fake.VirtualMachine(
            name=uuidutils.generate_uuid())
        invalid_vm2.set('runtime.connectionState', 'inaccessible')
        fake_objects.add_object(invalid_vm1)
        fake_objects.add_object(invalid_vm2)
        vms = ops._get_valid_vms_from_retrieve_result(fake_objects)
        self.assertEqual(1, len(vms))

    def test_delete_vm_snapshot(self):
        def fake_call_method(module, method, *args, **kwargs):
            self.assertEqual('RemoveSnapshot_Task', method)
            self.assertEqual('fake_vm_snapshot', args[0])
            self.assertFalse(kwargs['removeChildren'])
            self.assertTrue(kwargs['consolidate'])
            return 'fake_remove_snapshot_task'

        with contextlib.nested(
            mock.patch.object(self._session, '_wait_for_task'),
            mock.patch.object(self._session, '_call_method', fake_call_method)
        ) as (_wait_for_task, _call_method):
            self._vmops._delete_vm_snapshot(self._instance,
                                            "fake_vm_ref", "fake_vm_snapshot")
            _wait_for_task.assert_has_calls([
                   mock.call('fake_remove_snapshot_task')])

    def test_create_vm_snapshot(self):

        method_list = ['CreateSnapshot_Task', 'get_dynamic_property']

        def fake_call_method(module, method, *args, **kwargs):
            expected_method = method_list.pop(0)
            self.assertEqual(expected_method, method)
            if (expected_method == 'CreateSnapshot_Task'):
                self.assertEqual('fake_vm_ref', args[0])
                self.assertFalse(kwargs['memory'])
                self.assertTrue(kwargs['quiesce'])
                return 'fake_snapshot_task'
            elif (expected_method == 'get_dynamic_property'):
                task_info = mock.Mock()
                task_info.result = "fake_snapshot_ref"
                self.assertEqual(('fake_snapshot_task', 'Task', 'info'), args)
                return task_info

        with contextlib.nested(
            mock.patch.object(self._session, '_wait_for_task'),
            mock.patch.object(self._session, '_call_method', fake_call_method)
        ) as (_wait_for_task, _call_method):
            snap = self._vmops._create_vm_snapshot(self._instance,
                                                   "fake_vm_ref")
            self.assertEqual("fake_snapshot_ref", snap)
            _wait_for_task.assert_has_calls([
                   mock.call('fake_snapshot_task')])

    def test_update_instance_progress(self):
        with mock.patch.object(self._instance, 'save') as mock_save:
            self._vmops._update_instance_progress(self._instance._context,
                                                  self._instance, 5, 10)
            mock_save.assert_called_once_with()
        self.assertEqual(50, self._instance.progress)

    @mock.patch.object(vm_util, 'get_vm_ref', return_value='fake_ref')
    def test_get_info(self, mock_get_vm_ref):
        props = ['summary.config.numCpu', 'summary.config.memorySizeMB',
                 'runtime.powerState']
        prop_cpu = vmwareapi_fake.Prop(props[0], 4)
        prop_mem = vmwareapi_fake.Prop(props[1], 128)
        prop_state = vmwareapi_fake.Prop(props[2], 'poweredOn')
        prop_list = [prop_state, prop_mem, prop_cpu]
        obj_content = vmwareapi_fake.ObjectContent(None, prop_list=prop_list)
        result = vmwareapi_fake.FakeRetrieveResult()
        result.add_object(obj_content)

        def mock_call_method(module, method, *args, **kwargs):
            if method == 'continue_retrieval':
                return
            return result

        with mock.patch.object(self._session, '_call_method',
                               mock_call_method):
            info = self._vmops.get_info(self._instance)
            mock_get_vm_ref.assert_called_once_with(self._session,
                self._instance)
            expected = hardware.InstanceInfo(state=power_state.RUNNING,
                                             max_mem_kb=128 * 1024,
                                             mem_kb=128 * 1024,
                                             num_cpu=4)
            self.assertEqual(expected, info)

    @mock.patch.object(vm_util, 'get_vm_ref', return_value='fake_ref')
    def test_get_info_when_ds_unavailable(self, mock_get_vm_ref):
        props = ['summary.config.numCpu', 'summary.config.memorySizeMB',
                 'runtime.powerState']
        prop_state = vmwareapi_fake.Prop(props[2], 'poweredOff')
        # when vm's ds not available, only power state can be received
        prop_list = [prop_state]
        obj_content = vmwareapi_fake.ObjectContent(None, prop_list=prop_list)
        result = vmwareapi_fake.FakeRetrieveResult()
        result.add_object(obj_content)

        def mock_call_method(module, method, *args, **kwargs):
            if method == 'continue_retrieval':
                return
            return result

        with mock.patch.object(self._session, '_call_method',
                               mock_call_method):
            info = self._vmops.get_info(self._instance)
            mock_get_vm_ref.assert_called_once_with(self._session,
                self._instance)
            self.assertEqual(hardware.InstanceInfo(state=power_state.SHUTDOWN),
                             info)

    def _test_get_datacenter_ref_and_name(self, ds_ref_exists=False):
        instance_ds_ref = mock.Mock()
        instance_ds_ref.value = "ds-1"
        _vcvmops = vmops.VMwareVMOps(self._session, None, None)
        if ds_ref_exists:
            ds_ref = mock.Mock()
            ds_ref.value = "ds-1"
        else:
            ds_ref = None
        self._continue_retrieval = True
        self._fake_object1 = vmwareapi_fake.FakeRetrieveResult()
        self._fake_object2 = vmwareapi_fake.FakeRetrieveResult()

        def fake_call_method(module, method, *args, **kwargs):
            self._fake_object1.add_object(vmwareapi_fake.Datacenter(
                ds_ref=ds_ref))
            if not ds_ref:
                # Token is set for the fake_object1, so it will continue to
                # fetch the next object.
                setattr(self._fake_object1, 'token', 'token-0')
                if self._continue_retrieval:
                    if self._continue_retrieval:
                        self._continue_retrieval = False
                        self._fake_object2.add_object(
                            vmwareapi_fake.Datacenter())
                        return self._fake_object2
                    return
            if method == "continue_retrieval":
                return
            return self._fake_object1

        with mock.patch.object(self._session, '_call_method',
                               side_effect=fake_call_method) as fake_call:
            dc_info = _vcvmops.get_datacenter_ref_and_name(instance_ds_ref)

            if ds_ref:
                self.assertEqual(1, len(_vcvmops._datastore_dc_mapping))
                calls = [mock.call(vim_util, "get_objects", "Datacenter",
                                   ["name", "datastore", "vmFolder"]),
                         mock.call(vutil, 'continue_retrieval',
                                   self._fake_object1)]
                fake_call.assert_has_calls(calls)
                self.assertEqual("ha-datacenter", dc_info.name)
            else:
                calls = [mock.call(vim_util, "get_objects", "Datacenter",
                                   ["name", "datastore", "vmFolder"]),
                         mock.call(vutil, 'continue_retrieval',
                                   self._fake_object2)]
                fake_call.assert_has_calls(calls)
                self.assertIsNone(dc_info)

    def test_get_datacenter_ref_and_name(self):
        self._test_get_datacenter_ref_and_name(ds_ref_exists=True)

    def test_get_datacenter_ref_and_name_with_no_datastore(self):
        self._test_get_datacenter_ref_and_name()

    @mock.patch.object(vm_util, 'power_off_instance')
    @mock.patch.object(ds_util, 'disk_copy')
    @mock.patch.object(vm_util, 'get_vm_ref', return_value='fake-ref')
    @mock.patch.object(vm_util, 'get_values_from_object_properties')
    @mock.patch.object(vm_util, 'find_rescue_device')
    @mock.patch.object(vm_util, 'get_vm_boot_spec')
    @mock.patch.object(vm_util, 'reconfigure_vm')
    @mock.patch.object(vm_util, 'power_on_instance')
    @mock.patch.object(ds_util, 'get_datastore_by_ref')
    def test_rescue(self, mock_get_ds_by_ref, mock_power_on, mock_reconfigure,
                    mock_get_boot_spec, mock_find_rescue,
                    mock_get_values, mock_get_vm_ref, mock_disk_copy,
                    mock_power_off):
        _volumeops = mock.Mock()
        self._vmops._volumeops = _volumeops

        ds = ds_obj.Datastore('fake-ref', 'ds1')
        mock_get_ds_by_ref.return_value = ds
        mock_find_rescue.return_value = 'fake-rescue-device'
        mock_get_boot_spec.return_value = 'fake-boot-spec'

        device = vmwareapi_fake.DataObject()
        backing = vmwareapi_fake.DataObject()
        backing.datastore = ds.ref
        device.backing = backing
        vmdk = vm_util.VmdkInfo('[fake] uuid/root.vmdk',
                                'fake-adapter',
                                'fake-disk',
                                'fake-capacity',
                                device)

        with contextlib.nested(
            mock.patch.object(self._vmops, 'get_datacenter_ref_and_name'),
            mock.patch.object(vm_util, 'get_vmdk_info',
                              return_value=vmdk)
        ) as (_get_dc_ref_and_name, fake_vmdk_info):
            dc_info = mock.Mock()
            _get_dc_ref_and_name.return_value = dc_info
            self._vmops.rescue(self._context, self._instance, None, None)

            mock_power_off.assert_called_once_with(self._session,
                                                   self._instance,
                                                   'fake-ref')

            uuid = self._instance.image_ref
            cache_path = ds.build_path('vmware_base', uuid, uuid + '.vmdk')
            rescue_path = ds.build_path('fake_uuid', uuid + '-rescue.vmdk')

            mock_disk_copy.assert_called_once_with(self._session, dc_info.ref,
                             cache_path, rescue_path)
            _volumeops.attach_disk_to_vm.assert_called_once_with('fake-ref',
                             self._instance, mock.ANY, mock.ANY, rescue_path)
            mock_get_boot_spec.assert_called_once_with(mock.ANY,
                                                       'fake-rescue-device')
            mock_reconfigure.assert_called_once_with(self._session,
                                                     'fake-ref',
                                                     'fake-boot-spec')
            mock_power_on.assert_called_once_with(self._session,
                                                  self._instance,
                                                  vm_ref='fake-ref')

    def test_unrescue_power_on(self):
        self._test_unrescue(True)

    def test_unrescue_power_off(self):
        self._test_unrescue(False)

    def _test_unrescue(self, power_on):
        _volumeops = mock.Mock()
        self._vmops._volumeops = _volumeops
        vm_ref = mock.Mock()

        def fake_call_method(module, method, *args, **kwargs):
            expected_args = (vm_ref, 'VirtualMachine',
                             'config.hardware.device')
            self.assertEqual('get_dynamic_property', method)
            self.assertEqual(expected_args, args)

        with contextlib.nested(
                mock.patch.object(vm_util, 'power_on_instance'),
                mock.patch.object(vm_util, 'find_rescue_device'),
                mock.patch.object(vm_util, 'get_vm_ref', return_value=vm_ref),
                mock.patch.object(self._session, '_call_method',
                                  fake_call_method),
                mock.patch.object(vm_util, 'power_off_instance')
        ) as (_power_on_instance, _find_rescue, _get_vm_ref,
              _call_method, _power_off):
            self._vmops.unrescue(self._instance, power_on=power_on)

            if power_on:
                _power_on_instance.assert_called_once_with(self._session,
                        self._instance, vm_ref=vm_ref)
            else:
                self.assertFalse(_power_on_instance.called)
            _get_vm_ref.assert_called_once_with(self._session,
                                                self._instance)
            _power_off.assert_called_once_with(self._session, self._instance,
                                               vm_ref)
            _volumeops.detach_disk_from_vm.assert_called_once_with(
                vm_ref, self._instance, mock.ANY, destroy_disk=True)

    def _test_finish_migration(self, power_on=True, resize_instance=False):
        with contextlib.nested(
                mock.patch.object(self._vmops, '_resize_create_ephemerals'),
                mock.patch.object(self._vmops, "_update_instance_progress"),
                mock.patch.object(vm_util, "power_on_instance"),
                mock.patch.object(vm_util, "get_vm_ref",
                                  return_value='fake-ref')
        ) as (fake_resize_create_ephemerals, fake_update_instance_progress,
              fake_power_on, fake_get_vm_ref):
            self._vmops.finish_migration(context=self._context,
                                         migration=None,
                                         instance=self._instance,
                                         disk_info=None,
                                         network_info=None,
                                         block_device_info=None,
                                         resize_instance=resize_instance,
                                         image_meta=None,
                                         power_on=power_on)
            fake_resize_create_ephemerals.called_once_with('fake-ref',
                                                           self._instance,
                                                           None)
            if power_on:
                fake_power_on.assert_called_once_with(self._session,
                                                      self._instance,
                                                      vm_ref='fake-ref')
            else:
                self.assertFalse(fake_power_on.called)

        calls = [
                mock.call(self._context, self._instance, step=5,
                          total_steps=vmops.RESIZE_TOTAL_STEPS),
                mock.call(self._context, self._instance, step=6,
                          total_steps=vmops.RESIZE_TOTAL_STEPS)]
        fake_update_instance_progress.assert_has_calls(calls)

    def test_finish_migration_power_on(self):
        self._test_finish_migration(power_on=True, resize_instance=False)

    def test_finish_migration_power_off(self):
        self._test_finish_migration(power_on=False, resize_instance=False)

    def test_finish_migration_power_on_resize(self):
        self._test_finish_migration(power_on=True, resize_instance=True)

    @mock.patch.object(vmops.VMwareVMOps, '_get_extra_specs')
    @mock.patch.object(vmops.VMwareVMOps, '_resize_create_ephemerals')
    @mock.patch.object(vmops.VMwareVMOps, '_remove_ephemerals')
    @mock.patch.object(ds_util, 'disk_delete')
    @mock.patch.object(ds_util, 'disk_move')
    @mock.patch.object(ds_util, 'file_exists',
                       return_value=True)
    @mock.patch.object(vmops.VMwareVMOps, '_get_ds_browser',
                       return_value='fake-browser')
    @mock.patch.object(vm_util, 'reconfigure_vm')
    @mock.patch.object(vm_util, 'get_vm_resize_spec',
                       return_value='fake-spec')
    @mock.patch.object(vm_util, 'power_off_instance')
    @mock.patch.object(vm_util, 'get_vm_ref', return_value='fake-ref')
    @mock.patch.object(vm_util, 'power_on_instance')
    def _test_finish_revert_migration(self, fake_power_on,
                                      fake_get_vm_ref, fake_power_off,
                                      fake_resize_spec, fake_reconfigure_vm,
                                      fake_get_browser,
                                      fake_original_exists, fake_disk_move,
                                      fake_disk_delete,
                                      fake_remove_ephemerals,
                                      fake_resize_create_ephemerals,
                                      fake_get_extra_specs,
                                      power_on):
        """Tests the finish_revert_migration method on vmops."""
        datastore = ds_obj.Datastore(ref='fake-ref', name='fake')
        device = vmwareapi_fake.DataObject()
        backing = vmwareapi_fake.DataObject()
        backing.datastore = datastore.ref
        device.backing = backing
        vmdk = vm_util.VmdkInfo('[fake] uuid/root.vmdk',
                                'fake-adapter',
                                'fake-disk',
                                'fake-capacity',
                                device)
        dc_info = vmops.DcInfo(ref='fake_ref', name='fake',
                               vmFolder='fake_folder')
        extra_specs = vm_util.ExtraSpecs()
        fake_get_extra_specs.return_value = extra_specs
        with contextlib.nested(
            mock.patch.object(self._vmops, 'get_datacenter_ref_and_name',
                              return_value=dc_info),
            mock.patch.object(vm_util, 'get_vmdk_info',
                              return_value=vmdk)
        ) as (fake_get_dc_ref_and_name, fake_get_vmdk_info):
            self._vmops._volumeops = mock.Mock()
            mock_attach_disk = self._vmops._volumeops.attach_disk_to_vm
            mock_detach_disk = self._vmops._volumeops.detach_disk_from_vm

            self._vmops.finish_revert_migration(self._context,
                                                instance=self._instance,
                                                network_info=None,
                                                block_device_info=None,
                                                power_on=power_on)
            fake_get_vm_ref.assert_called_once_with(self._session,
                                                    self._instance)
            fake_power_off.assert_called_once_with(self._session,
                                                   self._instance,
                                                   'fake-ref')
            # Validate VM reconfiguration
            fake_resize_spec.assert_called_once_with(
                self._session.vim.client.factory,
                int(self._instance.vcpus),
                int(self._instance.memory_mb),
                extra_specs)
            fake_reconfigure_vm.assert_called_once_with(self._session,
                                                        'fake-ref',
                                                        'fake-spec')
            # Validate disk configuration
            fake_get_vmdk_info.assert_called_once_with(
                self._session, 'fake-ref', uuid=self._instance.uuid)
            fake_get_browser.assert_called_once_with('fake-ref')
            fake_original_exists.assert_called_once_with(
                 self._session, 'fake-browser',
                 ds_obj.DatastorePath(datastore.name, 'uuid'),
                 'original.vmdk')

            mock_detach_disk.assert_called_once_with('fake-ref',
                                                     self._instance,
                                                     device)
            fake_disk_delete.assert_called_once_with(
                self._session, dc_info.ref, '[fake] uuid/root.vmdk')
            fake_disk_move.assert_called_once_with(
                self._session, dc_info.ref,
                '[fake] uuid/original.vmdk',
                '[fake] uuid/root.vmdk')
            mock_attach_disk.assert_called_once_with(
                    'fake-ref', self._instance, 'fake-adapter', 'fake-disk',
                    '[fake] uuid/root.vmdk')
            fake_remove_ephemerals.called_once_with('fake-ref')
            fake_resize_create_ephemerals.called_once_with('fake-ref',
                                                           self._instance,
                                                           None)
        if power_on:
            fake_power_on.assert_called_once_with(self._session,
                                                  self._instance)
        else:
            self.assertFalse(fake_power_on.called)

    def test_finish_revert_migration_power_on(self):
        self._test_finish_revert_migration(power_on=True)

    def test_finish_revert_migration_power_off(self):
        self._test_finish_revert_migration(power_on=False)

    @mock.patch.object(vmops.VMwareVMOps, '_get_extra_specs')
    @mock.patch.object(vm_util, 'reconfigure_vm')
    @mock.patch.object(vm_util, 'get_vm_resize_spec',
                       return_value='fake-spec')
    def test_resize_vm(self, fake_resize_spec, fake_reconfigure,
                       fake_get_extra_specs):
        extra_specs = vm_util.ExtraSpecs()
        fake_get_extra_specs.return_value = extra_specs
        flavor = {'vcpus': 2, 'memory_mb': 1024}
        self._vmops._resize_vm('vm-ref', flavor)
        fake_resize_spec.assert_called_once_with(
            self._session.vim.client.factory, 2, 1024, extra_specs)
        fake_reconfigure.assert_called_once_with(self._session,
                                                 'vm-ref', 'fake-spec')

    @mock.patch.object(vmops.VMwareVMOps, '_extend_virtual_disk')
    @mock.patch.object(ds_util, 'disk_move')
    @mock.patch.object(ds_util, 'disk_copy')
    def test_resize_disk(self, fake_disk_copy, fake_disk_move,
                         fake_extend):
        datastore = ds_obj.Datastore(ref='fake-ref', name='fake')
        device = vmwareapi_fake.DataObject()
        backing = vmwareapi_fake.DataObject()
        backing.datastore = datastore.ref
        device.backing = backing
        vmdk = vm_util.VmdkInfo('[fake] uuid/root.vmdk',
                                'fake-adapter',
                                'fake-disk',
                                self._instance.root_gb * units.Gi,
                                device)
        dc_info = vmops.DcInfo(ref='fake_ref', name='fake',
                               vmFolder='fake_folder')
        with mock.patch.object(self._vmops, 'get_datacenter_ref_and_name',
            return_value=dc_info) as fake_get_dc_ref_and_name:
            self._vmops._volumeops = mock.Mock()
            mock_attach_disk = self._vmops._volumeops.attach_disk_to_vm
            mock_detach_disk = self._vmops._volumeops.detach_disk_from_vm

            flavor = {'root_gb': self._instance.root_gb + 1}
            self._vmops._resize_disk(self._instance, 'fake-ref', vmdk, flavor)
            fake_get_dc_ref_and_name.assert_called_once_with(datastore.ref)
            fake_disk_copy.assert_called_once_with(
                self._session, dc_info.ref, '[fake] uuid/root.vmdk',
                '[fake] uuid/resized.vmdk')
            mock_detach_disk.assert_called_once_with('fake-ref',
                                                     self._instance,
                                                     device)
            fake_extend.assert_called_once_with(
                self._instance, flavor['root_gb'] * units.Mi,
                '[fake] uuid/resized.vmdk', dc_info.ref)
            calls = [
                    mock.call(self._session, dc_info.ref,
                              '[fake] uuid/root.vmdk',
                              '[fake] uuid/original.vmdk'),
                    mock.call(self._session, dc_info.ref,
                              '[fake] uuid/resized.vmdk',
                              '[fake] uuid/root.vmdk')]
            fake_disk_move.assert_has_calls(calls)

            mock_attach_disk.assert_called_once_with(
                    'fake-ref', self._instance, 'fake-adapter', 'fake-disk',
                    '[fake] uuid/root.vmdk')

    @mock.patch.object(ds_util, 'disk_delete')
    @mock.patch.object(ds_util, 'file_exists',
                       return_value=True)
    @mock.patch.object(vmops.VMwareVMOps, '_get_ds_browser',
                       return_value='fake-browser')
    @mock.patch.object(vm_util, 'get_vm_ref', return_value='fake-ref')
    def test_confirm_migration(self, fake_get_vm_ref, fake_get_browser,
                               fake_original_exists,
                               fake_disk_delete):
        """Tests the confirm_migration method on vmops."""
        datastore = ds_obj.Datastore(ref='fake-ref', name='fake')
        device = vmwareapi_fake.DataObject()
        backing = vmwareapi_fake.DataObject()
        backing.datastore = datastore.ref
        device.backing = backing
        vmdk = vm_util.VmdkInfo('[fake] uuid/root.vmdk',
                                'fake-adapter',
                                'fake-disk',
                                'fake-capacity',
                                device)
        dc_info = vmops.DcInfo(ref='fake_ref', name='fake',
                               vmFolder='fake_folder')
        with contextlib.nested(
            mock.patch.object(self._vmops, 'get_datacenter_ref_and_name',
                              return_value=dc_info),
            mock.patch.object(vm_util, 'get_vmdk_info',
                              return_value=vmdk)
        ) as (fake_get_dc_ref_and_name, fake_get_vmdk_info):
            self._vmops.confirm_migration(None,
                                          self._instance,
                                          None)
            fake_get_vm_ref.assert_called_once_with(self._session,
                                                    self._instance)
            fake_get_vmdk_info.assert_called_once_with(
                self._session, 'fake-ref', uuid=self._instance.uuid)
            fake_get_browser.assert_called_once_with('fake-ref')
            fake_original_exists.assert_called_once_with(
                self._session, 'fake-browser',
                ds_obj.DatastorePath(datastore.name, 'uuid'),
                'original.vmdk')
            fake_disk_delete.assert_called_once_with(
                self._session, dc_info.ref, '[fake] uuid/original.vmdk')

    @mock.patch.object(vmops.VMwareVMOps, "_remove_ephemerals")
    @mock.patch.object(vm_util, 'get_vmdk_info')
    @mock.patch.object(vmops.VMwareVMOps, "_resize_disk")
    @mock.patch.object(vmops.VMwareVMOps, "_resize_vm")
    @mock.patch.object(vm_util, 'power_off_instance')
    @mock.patch.object(vmops.VMwareVMOps, "_update_instance_progress")
    @mock.patch.object(vm_util, 'get_vm_ref', return_value='fake-ref')
    def test_migrate_disk_and_power_off(self, fake_get_vm_ref, fake_progress,
                                        fake_power_off, fake_resize_vm,
                                        fake_resize_disk, fake_get_vmdk_info,
                                        fake_remove_ephemerals):
        vmdk = vm_util.VmdkInfo('[fake] uuid/root.vmdk',
                                'fake-adapter',
                                'fake-disk',
                                self._instance.root_gb * units.Gi,
                                'fake-device')
        fake_get_vmdk_info.return_value = vmdk
        flavor = {'root_gb': self._instance.root_gb + 1}
        self._vmops.migrate_disk_and_power_off(self._context,
                                               self._instance,
                                               None,
                                               flavor)

        fake_get_vm_ref.assert_called_once_with(self._session,
                                                self._instance)

        fake_power_off.assert_called_once_with(self._session,
                                               self._instance,
                                               'fake-ref')
        fake_resize_vm.assert_called_once_with('fake-ref', flavor)
        fake_resize_disk.assert_called_once_with(self._instance, 'fake-ref',
                                                 vmdk, flavor)
        calls = [mock.call(self._context, self._instance, step=i,
                           total_steps=vmops.RESIZE_TOTAL_STEPS)
                 for i in range(4)]
        fake_progress.assert_has_calls(calls)

    @mock.patch.object(vmops.VMwareVMOps, '_attach_cdrom_to_vm')
    @mock.patch.object(vmops.VMwareVMOps, '_create_config_drive')
    def test_configure_config_drive(self,
                                    mock_create_config_drive,
                                    mock_attach_cdrom_to_vm):
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        vm_ref = mock.Mock()
        mock_create_config_drive.return_value = "fake_iso_path"
        self._vmops._configure_config_drive(
                self._instance, vm_ref, self._dc_info, self._ds,
                injected_files, admin_password)

        upload_iso_path = self._ds.build_path("fake_iso_path")
        mock_create_config_drive.assert_called_once_with(self._instance,
                injected_files, admin_password, self._ds.name,
                self._dc_info.name, self._instance.uuid, "Fake-CookieJar")
        mock_attach_cdrom_to_vm.assert_called_once_with(
                vm_ref, self._instance, self._ds.ref, str(upload_iso_path))

    @mock.patch.object(vmops.LOG, 'debug')
    @mock.patch.object(vmops.VMwareVMOps, '_fetch_image_if_missing')
    @mock.patch.object(vmops.VMwareVMOps, '_get_vm_config_info')
    @mock.patch.object(vmops.VMwareVMOps, 'build_virtual_machine')
    @mock.patch.object(vmops.lockutils, 'lock')
    def test_spawn_mask_block_device_info_password(self, mock_lock,
        mock_build_virtual_machine, mock_get_vm_config_info,
        mock_fetch_image_if_missing, mock_debug):
        # Very simple test that just ensures block_device_info auth_password
        # is masked when logged; the rest of the test just fails out early.
        data = {'auth_password': 'scrubme'}
        bdm = [{'boot_index': 0, 'disk_bus': constants.DEFAULT_ADAPTER_TYPE,
                'connection_info': {'data': data}}]
        bdi = {'block_device_mapping': bdm}

        self.password_logged = False

        # Tests that the parameters to the to_xml method are sanitized for
        # passwords when logged.
        def fake_debug(*args, **kwargs):
            if 'auth_password' in args[0]:
                self.password_logged = True
                self.assertNotIn('scrubme', args[0])

        mock_debug.side_effect = fake_debug
        self.flags(flat_injected=False, vnc_enabled=False)

        # Call spawn(). We don't care what it does as long as it generates
        # the log message, which we check below.
        with mock.patch.object(self._vmops, '_volumeops') as mock_vo:
            mock_vo.attach_root_volume.side_effect = test.TestingException
            try:
                self._vmops.spawn(
                    self._context, self._instance, {},
                    injected_files=None, admin_password=None,
                    network_info=[], block_device_info=bdi
                )
            except test.TestingException:
                pass

        # Check that the relevant log message was generated, and therefore
        # that we checked it was scrubbed
        self.assertTrue(self.password_logged)

    @mock.patch('nova.virt.vmwareapi.vm_util.power_on_instance')
    @mock.patch.object(vmops.VMwareVMOps, '_use_disk_image_as_linked_clone')
    @mock.patch.object(vmops.VMwareVMOps, '_fetch_image_if_missing')
    @mock.patch(
        'nova.virt.vmwareapi.imagecache.ImageCacheManager.enlist_image')
    @mock.patch.object(vmops.VMwareVMOps, 'build_virtual_machine')
    @mock.patch.object(vmops.VMwareVMOps, '_get_vm_config_info')
    @mock.patch.object(vmops.VMwareVMOps, '_get_extra_specs')
    @mock.patch.object(nova.virt.vmwareapi.images.VMwareImage,
                       'from_image')
    def test_spawn_non_root_block_device(self, from_image,
                                         get_extra_specs,
                                         get_vm_config_info,
                                         build_virtual_machine,
                                         enlist_image, fetch_image,
                                         use_disk_image,
                                         power_on_instance):
        extra_specs = get_extra_specs.return_value

        connection_info1 = {'data': 'fake-data1', 'serial': 'volume-fake-id1'}
        connection_info2 = {'data': 'fake-data2', 'serial': 'volume-fake-id2'}
        bdm = [{'connection_info': connection_info1,
                'disk_bus': constants.ADAPTER_TYPE_IDE,
                'mount_device': '/dev/sdb'},
               {'connection_info': connection_info2,
                'disk_bus': constants.DEFAULT_ADAPTER_TYPE,
                'mount_device': '/dev/sdc'}]
        bdi = {'block_device_mapping': bdm, 'root_device_name': '/dev/sda'}
        self.flags(flat_injected=False, vnc_enabled=False)

        image_size = (self._instance.root_gb) * units.Gi / 2
        image_info = images.VMwareImage(
                image_id=self._image_id,
                file_size=image_size)
        vi = get_vm_config_info.return_value
        from_image.return_value = image_info
        build_virtual_machine.return_value = 'fake-vm-ref'

        with mock.patch.object(self._vmops, '_volumeops') as volumeops:
            self._vmops.spawn(self._context, self._instance, {},
                              injected_files=None, admin_password=None,
                              network_info=[], block_device_info=bdi)

            from_image.assert_called_once_with(self._instance.image_ref, {})
            get_vm_config_info.assert_called_once_with(self._instance,
                image_info, extra_specs.storage_policy)
            build_virtual_machine.assert_called_once_with(self._instance,
                image_info, vi.dc_info, vi.datastore, [],
                extra_specs)
            enlist_image.assert_called_once_with(image_info.image_id,
                                                 vi.datastore, vi.dc_info.ref)
            fetch_image.assert_called_once_with(self._context, vi)
            use_disk_image.assert_called_once_with('fake-vm-ref', vi)
            volumeops.attach_volume.assert_any_call(
                connection_info1, self._instance, constants.ADAPTER_TYPE_IDE)
            volumeops.attach_volume.assert_any_call(
                connection_info2, self._instance,
                constants.DEFAULT_ADAPTER_TYPE)

    @mock.patch('nova.virt.vmwareapi.vm_util.power_on_instance')
    @mock.patch.object(vmops.VMwareVMOps, 'build_virtual_machine')
    @mock.patch.object(vmops.VMwareVMOps, '_get_vm_config_info')
    @mock.patch.object(vmops.VMwareVMOps, '_get_extra_specs')
    @mock.patch.object(nova.virt.vmwareapi.images.VMwareImage,
                       'from_image')
    def test_spawn_with_no_image_and_block_devices(self, from_image,
                                                   get_extra_specs,
                                                   get_vm_config_info,
                                                   build_virtual_machine,
                                                   power_on_instance):
        self._instance.image_ref = None
        self._instance.flavor = self._flavor
        extra_specs = get_extra_specs.return_value

        connection_info1 = {'data': 'fake-data1', 'serial': 'volume-fake-id1'}
        connection_info2 = {'data': 'fake-data2', 'serial': 'volume-fake-id2'}
        connection_info3 = {'data': 'fake-data3', 'serial': 'volume-fake-id3'}
        bdm = [{'boot_index': 0,
                'connection_info': connection_info1,
                'disk_bus': constants.ADAPTER_TYPE_IDE},
               {'boot_index': 1,
                'connection_info': connection_info2,
                'disk_bus': constants.DEFAULT_ADAPTER_TYPE},
               {'boot_index': 2,
                'connection_info': connection_info3,
                'disk_bus': constants.ADAPTER_TYPE_LSILOGICSAS}]
        bdi = {'block_device_mapping': bdm}
        self.flags(flat_injected=False, vnc_enabled=False)

        image_info = mock.sentinel.image_info
        vi = get_vm_config_info.return_value
        from_image.return_value = image_info
        build_virtual_machine.return_value = 'fake-vm-ref'

        with mock.patch.object(self._vmops, '_volumeops') as volumeops:
            self._vmops.spawn(self._context, self._instance, {},
                              injected_files=None, admin_password=None,
                              network_info=[], block_device_info=bdi)

            from_image.assert_called_once_with(self._instance.image_ref, {})
            get_vm_config_info.assert_called_once_with(self._instance,
                image_info, extra_specs.storage_policy)
            build_virtual_machine.assert_called_once_with(self._instance,
                image_info, vi.dc_info, vi.datastore, [],
                extra_specs)
            volumeops.attach_root_volume.assert_called_once_with(
                connection_info1, self._instance, vi.datastore.ref,
                constants.ADAPTER_TYPE_IDE)
            volumeops.attach_volume.assert_any_call(
                connection_info2, self._instance,
                constants.DEFAULT_ADAPTER_TYPE)
            volumeops.attach_volume.assert_any_call(
                connection_info3, self._instance,
                constants.ADAPTER_TYPE_LSILOGICSAS)

    @mock.patch('nova.virt.vmwareapi.vm_util.power_on_instance')
    @mock.patch.object(vmops.VMwareVMOps, 'build_virtual_machine')
    @mock.patch.object(vmops.VMwareVMOps, '_get_vm_config_info')
    @mock.patch.object(vmops.VMwareVMOps, '_get_extra_specs')
    @mock.patch.object(nova.virt.vmwareapi.images.VMwareImage,
                       'from_image')
    def test_spawn_unsupported_hardware(self, from_image,
                                        get_extra_specs,
                                        get_vm_config_info,
                                        build_virtual_machine,
                                        power_on_instance):
        self._instance.image_ref = None
        self._instance.flavor = self._flavor
        extra_specs = get_extra_specs.return_value

        connection_info = {'data': 'fake-data', 'serial': 'volume-fake-id'}
        bdm = [{'boot_index': 0,
                'connection_info': connection_info,
                'disk_bus': 'invalid_adapter_type'}]
        bdi = {'block_device_mapping': bdm}
        self.flags(flat_injected=False, vnc_enabled=False)

        image_info = mock.sentinel.image_info
        vi = get_vm_config_info.return_value
        from_image.return_value = image_info
        build_virtual_machine.return_value = 'fake-vm-ref'

        self.assertRaises(exception.UnsupportedHardware, self._vmops.spawn,
                          self._context, self._instance, {},
                          injected_files=None,
                          admin_password=None, network_info=[],
                          block_device_info=bdi)

        from_image.assert_called_once_with(self._instance.image_ref, {})
        get_vm_config_info.assert_called_once_with(
            self._instance, image_info, extra_specs.storage_policy)
        build_virtual_machine.assert_called_once_with(self._instance,
            image_info, vi.dc_info, vi.datastore, [],
            extra_specs)

    def test_get_ds_browser(self):
        cache = self._vmops._datastore_browser_mapping
        ds_browser = mock.Mock()
        moref = vmwareapi_fake.ManagedObjectReference('datastore-100')
        self.assertIsNone(cache.get(moref.value))
        mock_call_method = mock.Mock(return_value=ds_browser)
        with mock.patch.object(self._session, '_call_method',
                               mock_call_method):
            ret = self._vmops._get_ds_browser(moref)
            mock_call_method.assert_called_once_with(vim_util,
                'get_dynamic_property', moref, 'Datastore', 'browser')
            self.assertIs(ds_browser, ret)
            self.assertIs(ds_browser, cache.get(moref.value))

    @mock.patch.object(
            vmops.VMwareVMOps, '_sized_image_exists', return_value=False)
    @mock.patch.object(vmops.VMwareVMOps, '_extend_virtual_disk')
    @mock.patch.object(vm_util, 'copy_virtual_disk')
    def _test_use_disk_image_as_linked_clone(self,
                                             mock_copy_virtual_disk,
                                             mock_extend_virtual_disk,
                                             mock_sized_image_exists,
                                             flavor_fits_image=False):
        file_size = 10 * units.Gi if flavor_fits_image else 5 * units.Gi
        image_info = images.VMwareImage(
                image_id=self._image_id,
                file_size=file_size,
                linked_clone=False)

        cache_root_folder = self._ds.build_path("vmware_base", self._image_id)
        mock_imagecache = mock.Mock()
        mock_imagecache.get_image_cache_folder.return_value = cache_root_folder
        vi = vmops.VirtualMachineInstanceConfigInfo(
                self._instance, image_info,
                self._ds, self._dc_info, mock_imagecache)

        sized_cached_image_ds_loc = cache_root_folder.join(
                "%s.%s.vmdk" % (self._image_id, vi.root_gb))

        self._vmops._volumeops = mock.Mock()
        mock_attach_disk_to_vm = self._vmops._volumeops.attach_disk_to_vm

        self._vmops._use_disk_image_as_linked_clone("fake_vm_ref", vi)

        mock_copy_virtual_disk.assert_called_once_with(
                self._session, self._dc_info.ref,
                str(vi.cache_image_path),
                str(sized_cached_image_ds_loc))

        if not flavor_fits_image:
            mock_extend_virtual_disk.assert_called_once_with(
                    self._instance, vi.root_gb * units.Mi,
                    str(sized_cached_image_ds_loc),
                    self._dc_info.ref)

        mock_attach_disk_to_vm.assert_called_once_with(
                "fake_vm_ref", self._instance, vi.ii.adapter_type,
                vi.ii.disk_type,
                str(sized_cached_image_ds_loc),
                vi.root_gb * units.Mi, False)

    def test_use_disk_image_as_linked_clone(self):
        self._test_use_disk_image_as_linked_clone()

    def test_use_disk_image_as_linked_clone_flavor_fits_image(self):
        self._test_use_disk_image_as_linked_clone(flavor_fits_image=True)

    @mock.patch.object(vmops.VMwareVMOps, '_extend_virtual_disk')
    @mock.patch.object(vm_util, 'copy_virtual_disk')
    def _test_use_disk_image_as_full_clone(self,
                                          mock_copy_virtual_disk,
                                          mock_extend_virtual_disk,
                                          flavor_fits_image=False):
        file_size = 10 * units.Gi if flavor_fits_image else 5 * units.Gi
        image_info = images.VMwareImage(
                image_id=self._image_id,
                file_size=file_size,
                linked_clone=False)

        cache_root_folder = self._ds.build_path("vmware_base", self._image_id)
        mock_imagecache = mock.Mock()
        mock_imagecache.get_image_cache_folder.return_value = cache_root_folder
        vi = vmops.VirtualMachineInstanceConfigInfo(
                self._instance, image_info,
                self._ds, self._dc_info, mock_imagecache)

        self._vmops._volumeops = mock.Mock()
        mock_attach_disk_to_vm = self._vmops._volumeops.attach_disk_to_vm

        self._vmops._use_disk_image_as_full_clone("fake_vm_ref", vi)

        mock_copy_virtual_disk.assert_called_once_with(
                self._session, self._dc_info.ref,
                str(vi.cache_image_path),
                '[fake_ds] fake_uuid/fake_uuid.vmdk')

        if not flavor_fits_image:
            mock_extend_virtual_disk.assert_called_once_with(
                    self._instance, vi.root_gb * units.Mi,
                    '[fake_ds] fake_uuid/fake_uuid.vmdk', self._dc_info.ref)

        mock_attach_disk_to_vm.assert_called_once_with(
                "fake_vm_ref", self._instance, vi.ii.adapter_type,
                vi.ii.disk_type, '[fake_ds] fake_uuid/fake_uuid.vmdk',
                vi.root_gb * units.Mi, False)

    def test_use_disk_image_as_full_clone(self):
        self._test_use_disk_image_as_full_clone()

    def test_use_disk_image_as_full_clone_image_too_big(self):
        self._test_use_disk_image_as_full_clone(flavor_fits_image=True)

    @mock.patch.object(vmops.VMwareVMOps, '_attach_cdrom_to_vm')
    @mock.patch.object(vm_util, 'create_virtual_disk')
    def _test_use_iso_image(self,
                            mock_create_virtual_disk,
                            mock_attach_cdrom,
                            with_root_disk):
        image_info = images.VMwareImage(
                image_id=self._image_id,
                file_size=10 * units.Mi,
                linked_clone=True)

        cache_root_folder = self._ds.build_path("vmware_base", self._image_id)
        mock_imagecache = mock.Mock()
        mock_imagecache.get_image_cache_folder.return_value = cache_root_folder
        vi = vmops.VirtualMachineInstanceConfigInfo(
                self._instance, image_info,
                self._ds, self._dc_info, mock_imagecache)

        self._vmops._volumeops = mock.Mock()
        mock_attach_disk_to_vm = self._vmops._volumeops.attach_disk_to_vm

        self._vmops._use_iso_image("fake_vm_ref", vi)

        mock_attach_cdrom.assert_called_once_with(
                "fake_vm_ref", self._instance, self._ds.ref,
                str(vi.cache_image_path))

        if with_root_disk:
            mock_create_virtual_disk.assert_called_once_with(
                    self._session, self._dc_info.ref,
                    vi.ii.adapter_type, vi.ii.disk_type,
                    '[fake_ds] fake_uuid/fake_uuid.vmdk',
                    vi.root_gb * units.Mi)
            linked_clone = False
            mock_attach_disk_to_vm.assert_called_once_with(
                    "fake_vm_ref", self._instance,
                    vi.ii.adapter_type, vi.ii.disk_type,
                    '[fake_ds] fake_uuid/fake_uuid.vmdk',
                    vi.root_gb * units.Mi, linked_clone)

    def test_use_iso_image_with_root_disk(self):
        self._test_use_iso_image(with_root_disk=True)

    def test_use_iso_image_without_root_disk(self):
        self._test_use_iso_image(with_root_disk=False)

    def _verify_spawn_method_calls(self, mock_call_method, extras=None):
        # TODO(vui): More explicit assertions of spawn() behavior
        # are waiting on additional refactoring pertaining to image
        # handling/manipulation. Till then, we continue to assert on the
        # sequence of VIM operations invoked.
        expected_methods = ['get_dynamic_property',
                            'SearchDatastore_Task',
                            'CreateVirtualDisk_Task',
                            'DeleteDatastoreFile_Task',
                            'MoveDatastoreFile_Task',
                            'DeleteDatastoreFile_Task',
                            'SearchDatastore_Task',
                            'ExtendVirtualDisk_Task',
        ]
        if extras:
            expected_methods.extend(extras)

        recorded_methods = [c[1][1] for c in mock_call_method.mock_calls]
        self.assertEqual(expected_methods, recorded_methods)

    @mock.patch(
        'nova.virt.vmwareapi.vmops.VMwareVMOps._configure_config_drive')
    @mock.patch('nova.virt.vmwareapi.ds_util.get_datastore')
    @mock.patch(
        'nova.virt.vmwareapi.vmops.VMwareVMOps.get_datacenter_ref_and_name')
    @mock.patch('nova.virt.vmwareapi.vif.get_vif_info',
                return_value=[])
    @mock.patch('nova.utils.is_neutron',
                return_value=False)
    @mock.patch('nova.virt.vmwareapi.vm_util.get_vm_create_spec',
                return_value='fake_create_spec')
    @mock.patch('nova.virt.vmwareapi.vm_util.create_vm',
                return_value='fake_vm_ref')
    @mock.patch('nova.virt.vmwareapi.ds_util.mkdir')
    @mock.patch('nova.virt.vmwareapi.vmops.VMwareVMOps._set_machine_id')
    @mock.patch(
        'nova.virt.vmwareapi.imagecache.ImageCacheManager.enlist_image')
    @mock.patch.object(vmops.VMwareVMOps, '_get_and_set_vnc_config')
    @mock.patch('nova.virt.vmwareapi.vm_util.power_on_instance')
    @mock.patch('nova.virt.vmwareapi.vm_util.copy_virtual_disk')
    # TODO(dims): Need to add tests for create_virtual_disk after the
    #             disk/image code in spawn gets refactored
    def _test_spawn(self,
                   mock_copy_virtual_disk,
                   mock_power_on_instance,
                   mock_get_and_set_vnc_config,
                   mock_enlist_image,
                   mock_set_machine_id,
                   mock_mkdir,
                   mock_create_vm,
                   mock_get_create_spec,
                   mock_is_neutron,
                   mock_get_vif_info,
                   mock_get_datacenter_ref_and_name,
                   mock_get_datastore,
                   mock_configure_config_drive,
                   block_device_info=None,
                   power_on=True,
                   extra_specs=None,
                   config_drive=False):

        image_size = (self._instance.root_gb) * units.Gi / 2
        image = {
            'id': self._image_id,
            'disk_format': 'vmdk',
            'size': image_size,
        }
        image_info = images.VMwareImage(
            image_id=self._image_id,
            file_size=image_size)
        vi = self._vmops._get_vm_config_info(
            self._instance, image_info)

        self._vmops._volumeops = mock.Mock()
        network_info = mock.Mock()
        mock_get_datastore.return_value = self._ds
        mock_get_datacenter_ref_and_name.return_value = self._dc_info
        mock_call_method = mock.Mock(return_value='fake_task')

        if extra_specs is None:
            extra_specs = vm_util.ExtraSpecs()

        with contextlib.nested(
                mock.patch.object(self._session, '_wait_for_task'),
                mock.patch.object(self._session, '_call_method',
                                  mock_call_method),
                mock.patch.object(uuidutils, 'generate_uuid',
                                  return_value='tmp-uuid'),
                mock.patch.object(images, 'fetch_image'),
                mock.patch.object(self._vmops, '_get_extra_specs',
                                  return_value=extra_specs)
        ) as (_wait_for_task, _call_method, _generate_uuid, _fetch_image,
              _get_extra_specs):
            self._vmops.spawn(self._context, self._instance, image,
                              injected_files='fake_files',
                              admin_password='password',
                              network_info=network_info,
                              block_device_info=block_device_info,
                              power_on=power_on)

            mock_is_neutron.assert_called_once_with()

            self.assertEqual(2, mock_mkdir.call_count)

            mock_get_vif_info.assert_called_once_with(
                    self._session, self._cluster.obj, False,
                    constants.DEFAULT_VIF_MODEL, network_info)
            mock_get_create_spec.assert_called_once_with(
                    self._session.vim.client.factory,
                    self._instance,
                    'fake_ds',
                    [],
                    extra_specs,
                    'otherGuest',
                    profile_spec=None)
            mock_create_vm.assert_called_once_with(
                    self._session,
                    self._instance,
                    'fake_vm_folder',
                    'fake_create_spec',
                    self._cluster.resourcePool)
            mock_get_and_set_vnc_config.assert_called_once_with(
                self._session.vim.client.factory,
                self._instance,
                'fake_vm_ref')
            mock_set_machine_id.assert_called_once_with(
                self._session.vim.client.factory,
                self._instance,
                network_info,
                vm_ref='fake_vm_ref')
            if power_on:
                mock_power_on_instance.assert_called_once_with(
                    self._session, self._instance, vm_ref='fake_vm_ref')
            else:
                self.assertFalse(mock_power_on_instance.called)

            if (block_device_info and
                'block_device_mapping' in block_device_info):
                bdms = block_device_info['block_device_mapping']
                for bdm in bdms:
                    mock_attach_root = (
                        self._vmops._volumeops.attach_root_volume)
                    mock_attach = self._vmops._volumeops.attach_volume
                    adapter_type = bdm.get('disk_bus') or vi.ii.adapter_type
                    if bdm.get('boot_index') == 0:
                        mock_attach_root.assert_any_call(
                            bdm['connection_info'], self._instance,
                            self._ds.ref, adapter_type)
                    else:
                        mock_attach.assert_any_call(
                            bdm['connection_info'], self._instance,
                            self._ds.ref, adapter_type)

            mock_enlist_image.assert_called_once_with(
                        self._image_id, self._ds, self._dc_info.ref)

            upload_file_name = 'vmware_temp/tmp-uuid/%s/%s-flat.vmdk' % (
                    self._image_id, self._image_id)
            _fetch_image.assert_called_once_with(
                    self._context,
                    self._instance,
                    self._session._host,
                    self._session._port,
                    self._dc_info.name,
                    self._ds.name,
                    upload_file_name,
                    cookies='Fake-CookieJar')
            self.assertTrue(len(_wait_for_task.mock_calls) > 0)
            extras = None
            if block_device_info and 'ephemerals' in block_device_info:
                extras = ['CreateVirtualDisk_Task']
            self._verify_spawn_method_calls(_call_method, extras)

            dc_ref = 'fake_dc_ref'
            source_file = unicode('[fake_ds] vmware_base/%s/%s.vmdk' %
                          (self._image_id, self._image_id))
            dest_file = unicode('[fake_ds] vmware_base/%s/%s.%d.vmdk' %
                          (self._image_id, self._image_id,
                          self._instance['root_gb']))
            # TODO(dims): add more tests for copy_virtual_disk after
            # the disk/image code in spawn gets refactored
            mock_copy_virtual_disk.assert_called_with(self._session,
                                                      dc_ref,
                                                      source_file,
                                                      dest_file)

            if config_drive:
                mock_configure_config_drive.assert_called_once_with(
                        self._instance, 'fake_vm_ref', self._dc_info,
                        self._ds, 'fake_files', 'password')

    @mock.patch.object(ds_util, 'get_datastore')
    @mock.patch.object(vmops.VMwareVMOps, 'get_datacenter_ref_and_name')
    def _test_get_spawn_vm_config_info(self,
                                       mock_get_datacenter_ref_and_name,
                                       mock_get_datastore,
                                       image_size_bytes=0):
        image_info = images.VMwareImage(
                image_id=self._image_id,
                file_size=image_size_bytes,
                linked_clone=True)

        mock_get_datastore.return_value = self._ds
        mock_get_datacenter_ref_and_name.return_value = self._dc_info

        vi = self._vmops._get_vm_config_info(self._instance, image_info)
        self.assertEqual(image_info, vi.ii)
        self.assertEqual(self._ds, vi.datastore)
        self.assertEqual(self._instance.root_gb, vi.root_gb)
        self.assertEqual(self._instance, vi.instance)
        self.assertEqual(self._instance.uuid, vi.instance.uuid)

        cache_image_path = '[%s] vmware_base/%s/%s.vmdk' % (
            self._ds.name, self._image_id, self._image_id)
        self.assertEqual(cache_image_path, str(vi.cache_image_path))

        cache_image_folder = '[%s] vmware_base/%s' % (
            self._ds.name, self._image_id)
        self.assertEqual(cache_image_folder, str(vi.cache_image_folder))

    def test_get_spawn_vm_config_info(self):
        image_size = (self._instance.root_gb) * units.Gi / 2
        self._test_get_spawn_vm_config_info(image_size_bytes=image_size)

    def test_get_spawn_vm_config_info_image_too_big(self):
        image_size = (self._instance.root_gb + 1) * units.Gi
        self.assertRaises(exception.InstanceUnacceptable,
                          self._test_get_spawn_vm_config_info,
                          image_size_bytes=image_size)

    def test_spawn(self):
        self._test_spawn()

    def test_spawn_config_drive_enabled(self):
        self.flags(force_config_drive=True)
        self._test_spawn(config_drive=True)

    def test_spawn_no_power_on(self):
        self._test_spawn(power_on=False)

    def test_spawn_with_block_device_info(self):
        block_device_info = {
            'block_device_mapping': [{'boot_index': 0,
                                      'connection_info': 'fake',
                                      'mount_device': '/dev/vda'}]
        }
        self._test_spawn(block_device_info=block_device_info)

    def test_spawn_with_block_device_info_with_config_drive(self):
        self.flags(force_config_drive=True)
        block_device_info = {
            'block_device_mapping': [{'boot_index': 0,
                                      'connection_info': 'fake',
                                      'mount_device': '/dev/vda'}]
        }
        self._test_spawn(block_device_info=block_device_info,
                         config_drive=True)

    def test_spawn_with_block_device_info_ephemerals(self):
        ephemerals = [{'device_type': 'disk',
                       'disk_bus': 'virtio',
                       'device_name': '/dev/vdb',
                       'size': 1}]
        block_device_info = {'ephemerals': ephemerals}
        self._test_spawn(block_device_info=block_device_info)

    def _get_fake_vi(self):
        image_info = images.VMwareImage(
                image_id=self._image_id,
                file_size=7,
                linked_clone=False)
        vi = vmops.VirtualMachineInstanceConfigInfo(
                self._instance, image_info,
                self._ds, self._dc_info, mock.Mock())
        return vi

    @mock.patch.object(vm_util, 'create_virtual_disk')
    def test_create_and_attach_ephemeral_disk(self, mock_create):
        vi = self._get_fake_vi()
        self._vmops._volumeops = mock.Mock()
        mock_attach_disk_to_vm = self._vmops._volumeops.attach_disk_to_vm

        path = str(ds_obj.DatastorePath(vi.datastore.name, 'fake_uuid',
                                        'fake-filename'))
        self._vmops._create_and_attach_ephemeral_disk(self._instance,
                                                      'fake-vm-ref',
                                                      vi.dc_info, 1,
                                                      'fake-adapter-type',
                                                      path)
        mock_create.assert_called_once_with(
                self._session, self._dc_info.ref, 'fake-adapter-type',
                'thin', path, 1)
        mock_attach_disk_to_vm.assert_called_once_with(
                'fake-vm-ref', self._instance, 'fake-adapter-type',
                'thin', path, 1, False)

    def test_create_ephemeral_with_bdi(self):
        ephemerals = [{'device_type': 'disk',
                       'disk_bus': 'virtio',
                       'device_name': '/dev/vdb',
                       'size': 1}]
        block_device_info = {'ephemerals': ephemerals}
        vi = self._get_fake_vi()
        with mock.patch.object(
            self._vmops, '_create_and_attach_ephemeral_disk') as mock_caa:
            self._vmops._create_ephemeral(block_device_info,
                                          self._instance,
                                          'fake-vm-ref',
                                          vi.dc_info, vi.datastore,
                                          'fake_uuid',
                                          vi.ii.adapter_type)
            mock_caa.assert_called_once_with(
                self._instance, 'fake-vm-ref',
                vi.dc_info, 1 * units.Mi, 'virtio',
                '[fake_ds] fake_uuid/ephemeral_0.vmdk')

    def _test_create_ephemeral_from_instance(self, bdi):
        vi = self._get_fake_vi()
        with mock.patch.object(
            self._vmops, '_create_and_attach_ephemeral_disk') as mock_caa:
            self._vmops._create_ephemeral(bdi,
                                          self._instance,
                                          'fake-vm-ref',
                                          vi.dc_info, vi.datastore,
                                          'fake_uuid',
                                          vi.ii.adapter_type)
            mock_caa.assert_called_once_with(
                self._instance, 'fake-vm-ref',
                vi.dc_info, 1 * units.Mi, 'lsiLogic',
                '[fake_ds] fake_uuid/ephemeral_0.vmdk')

    def test_create_ephemeral_with_bdi_but_no_ephemerals(self):
        block_device_info = {'ephemerals': []}
        self._instance.ephemeral_gb = 1
        self._test_create_ephemeral_from_instance(block_device_info)

    def test_create_ephemeral_with_no_bdi(self):
        self._instance.ephemeral_gb = 1
        self._test_create_ephemeral_from_instance(None)

    def test_build_virtual_machine(self):
        image_id = nova.tests.unit.image.fake.get_valid_image_id()
        image = images.VMwareImage(image_id=image_id)

        extra_specs = vm_util.ExtraSpecs()

        vm_ref = self._vmops.build_virtual_machine(self._instance,
                                                   image, self._dc_info,
                                                   self._ds,
                                                   self.network_info,
                                                   extra_specs)

        vm = vmwareapi_fake._get_object(vm_ref)

        # Test basic VM parameters
        self.assertEqual(self._instance.uuid, vm.name)
        self.assertEqual(self._instance.uuid,
                         vm.get('summary.config.instanceUuid'))
        self.assertEqual(self._instance_values['vcpus'],
                         vm.get('summary.config.numCpu'))
        self.assertEqual(self._instance_values['memory_mb'],
                         vm.get('summary.config.memorySizeMB'))

        # Test NSX config
        for optval in vm.get('config.extraConfig').OptionValue:
            if optval.key == 'nvp.vm-uuid':
                self.assertEqual(self._instance_values['uuid'], optval.value)
                break
        else:
            self.fail('nvp.vm-uuid not found in extraConfig')

        # Test that the VM is associated with the specified datastore
        datastores = vm.datastore.ManagedObjectReference
        self.assertEqual(1, len(datastores))

        datastore = vmwareapi_fake._get_object(datastores[0])
        self.assertEqual(self._ds.name, datastore.get('summary.name'))

        # Test that the VM's network is configured as specified
        devices = vm.get('config.hardware.device').VirtualDevice
        for device in devices:
            if device.obj_name != 'ns0:VirtualE1000':
                continue
            self.assertEqual(self._network_values['address'],
                             device.macAddress)
            break
        else:
            self.fail('NIC not configured')

    def test_spawn_cpu_limit(self):
        cpu_limits = vm_util.CpuLimits(cpu_limit=7)
        extra_specs = vm_util.ExtraSpecs(cpu_limits=cpu_limits)
        self._test_spawn(extra_specs=extra_specs)

    def test_spawn_cpu_reservation(self):
        cpu_limits = vm_util.CpuLimits(cpu_reservation=7)
        extra_specs = vm_util.ExtraSpecs(cpu_limits=cpu_limits)
        self._test_spawn(extra_specs=extra_specs)

    def test_spawn_cpu_allocations(self):
        cpu_limits = vm_util.CpuLimits(cpu_limit=7,
                                       cpu_reservation=6)
        extra_specs = vm_util.ExtraSpecs(cpu_limits=cpu_limits)
        self._test_spawn(extra_specs=extra_specs)

    def test_spawn_cpu_shares_level(self):
        cpu_limits = vm_util.CpuLimits(cpu_shares_level='high')
        extra_specs = vm_util.ExtraSpecs(cpu_limits=cpu_limits)
        self._test_spawn(extra_specs=extra_specs)

    def test_spawn_cpu_shares_custom(self):
        cpu_limits = vm_util.CpuLimits(cpu_shares_level='custom',
                                       cpu_shares_share=1948)
        extra_specs = vm_util.ExtraSpecs(cpu_limits=cpu_limits)
        self._test_spawn(extra_specs=extra_specs)

    def _validate_extra_specs(self, expected, actual):
        self.assertEqual(expected.cpu_limits.cpu_limit,
                         actual.cpu_limits.cpu_limit)
        self.assertEqual(expected.cpu_limits.cpu_reservation,
                         actual.cpu_limits.cpu_reservation)
        self.assertEqual(expected.cpu_limits.cpu_shares_level,
                         actual.cpu_limits.cpu_shares_level)
        self.assertEqual(expected.cpu_limits.cpu_shares_share,
                         actual.cpu_limits.cpu_shares_share)

    def _validate_flavor_extra_specs(self, flavor_extra_specs, expected):
        # Validate that the extra specs are parsed correctly
        flavor = objects.Flavor(extra_specs=flavor_extra_specs)
        flavor_extra_specs = self._vmops._get_extra_specs(flavor)
        self._validate_extra_specs(expected, flavor_extra_specs)

    def test_extra_specs_cpu_limit(self):
        flavor_extra_specs = {'quota:cpu_limit': 7}
        cpu_limits = vm_util.CpuLimits(cpu_limit=7)
        extra_specs = vm_util.ExtraSpecs(cpu_limits=cpu_limits)
        self._validate_flavor_extra_specs(flavor_extra_specs, extra_specs)

    def test_extra_specs_cpu_reservations(self):
        flavor_extra_specs = {'quota:cpu_reservation': 7}
        cpu_limits = vm_util.CpuLimits(cpu_reservation=7)
        extra_specs = vm_util.ExtraSpecs(cpu_limits=cpu_limits)
        self._validate_flavor_extra_specs(flavor_extra_specs, extra_specs)

    def test_extra_specs_cpu_allocations(self):
        flavor_extra_specs = {'quota:cpu_limit': 7,
                              'quota:cpu_reservation': 6}
        cpu_limits = vm_util.CpuLimits(cpu_limit=7,
                                       cpu_reservation=6)
        extra_specs = vm_util.ExtraSpecs(cpu_limits=cpu_limits)
        self._validate_flavor_extra_specs(flavor_extra_specs, extra_specs)

    def test_extra_specs_cpu_shares_level(self):
        flavor_extra_specs = {'quota:cpu_shares_level': 'high'}
        cpu_limits = vm_util.CpuLimits(cpu_shares_level='high')
        extra_specs = vm_util.ExtraSpecs(cpu_limits=cpu_limits)
        self._validate_flavor_extra_specs(flavor_extra_specs, extra_specs)

    def test_extra_specs_cpu_shares_custom(self):
        flavor_extra_specs = {'quota:cpu_shares_level': 'custom',
                              'quota:cpu_shares_share': 1948}
        cpu_limits = vm_util.CpuLimits(cpu_shares_level='custom',
                                       cpu_shares_share=1948)
        extra_specs = vm_util.ExtraSpecs(cpu_limits=cpu_limits)
        self._validate_flavor_extra_specs(flavor_extra_specs, extra_specs)

    def _make_vm_config_info(self, is_iso=False, is_sparse_disk=False):
        disk_type = (constants.DISK_TYPE_SPARSE if is_sparse_disk
                     else constants.DEFAULT_DISK_TYPE)
        file_type = (constants.DISK_FORMAT_ISO if is_iso
                     else constants.DEFAULT_DISK_FORMAT)

        image_info = images.VMwareImage(
                image_id=self._image_id,
                file_size=10 * units.Mi,
                file_type=file_type,
                disk_type=disk_type,
                linked_clone=True)
        cache_root_folder = self._ds.build_path("vmware_base", self._image_id)
        mock_imagecache = mock.Mock()
        mock_imagecache.get_image_cache_folder.return_value = cache_root_folder
        vi = vmops.VirtualMachineInstanceConfigInfo(
                self._instance, image_info,
                self._ds, self._dc_info, mock_imagecache)
        return vi

    @mock.patch.object(vmops.VMwareVMOps, 'check_cache_folder')
    @mock.patch.object(vmops.VMwareVMOps, '_fetch_image_as_file')
    @mock.patch.object(vmops.VMwareVMOps, '_prepare_iso_image')
    @mock.patch.object(vmops.VMwareVMOps, '_prepare_sparse_image')
    @mock.patch.object(vmops.VMwareVMOps, '_prepare_flat_image')
    @mock.patch.object(vmops.VMwareVMOps, '_cache_iso_image')
    @mock.patch.object(vmops.VMwareVMOps, '_cache_sparse_image')
    @mock.patch.object(vmops.VMwareVMOps, '_cache_flat_image')
    @mock.patch.object(vmops.VMwareVMOps, '_delete_datastore_file')
    def _test_fetch_image_if_missing(self,
                                     mock_delete_datastore_file,
                                     mock_cache_flat_image,
                                     mock_cache_sparse_image,
                                     mock_cache_iso_image,
                                     mock_prepare_flat_image,
                                     mock_prepare_sparse_image,
                                     mock_prepare_iso_image,
                                     mock_fetch_image_as_file,
                                     mock_check_cache_folder,
                                     is_iso=False,
                                     is_sparse_disk=False):

        tmp_dir_path = mock.Mock()
        tmp_image_path = mock.Mock()
        if is_iso:
            mock_prepare = mock_prepare_iso_image
            mock_cache = mock_cache_iso_image
        elif is_sparse_disk:
            mock_prepare = mock_prepare_sparse_image
            mock_cache = mock_cache_sparse_image
        else:
            mock_prepare = mock_prepare_flat_image
            mock_cache = mock_cache_flat_image
        mock_prepare.return_value = tmp_dir_path, tmp_image_path

        vi = self._make_vm_config_info(is_iso, is_sparse_disk)
        self._vmops._fetch_image_if_missing(self._context, vi)

        mock_check_cache_folder.assert_called_once_with(
                self._ds.name, self._ds.ref)
        mock_prepare.assert_called_once_with(vi)
        mock_fetch_image_as_file.assert_called_once_with(
                self._context, vi, tmp_image_path)
        mock_cache.assert_called_once_with(vi, tmp_image_path)
        mock_delete_datastore_file.assert_called_once_with(
                str(tmp_dir_path), self._dc_info.ref)

    def test_fetch_image_if_missing(self):
        self._test_fetch_image_if_missing()

    def test_fetch_image_if_missing_with_sparse(self):
        self._test_fetch_image_if_missing(
                is_sparse_disk=True)

    def test_fetch_image_if_missing_with_iso(self):
        self._test_fetch_image_if_missing(
                is_iso=True)

    @mock.patch.object(images, 'fetch_image')
    def test_fetch_image_as_file(self, mock_fetch_image):
        vi = self._make_vm_config_info()
        image_ds_loc = mock.Mock()
        self._vmops._fetch_image_as_file(self._context, vi, image_ds_loc)
        mock_fetch_image.assert_called_once_with(
                self._context,
                vi.instance,
                self._session._host,
                self._session._port,
                self._dc_info.name,
                self._ds.name,
                image_ds_loc.rel_path,
                cookies='Fake-CookieJar')

    @mock.patch.object(images, 'fetch_image_stream_optimized')
    def test_fetch_image_as_vapp(self, mock_fetch_image):
        vi = self._make_vm_config_info()
        image_ds_loc = mock.Mock()
        image_ds_loc.parent.basename = 'fake-name'
        self._vmops._fetch_image_as_vapp(self._context, vi, image_ds_loc)
        mock_fetch_image.assert_called_once_with(
                self._context,
                vi.instance,
                self._session,
                'fake-name',
                self._ds.name,
                vi.dc_info.vmFolder,
                self._vmops._root_resource_pool)

    @mock.patch.object(uuidutils, 'generate_uuid', return_value='tmp-uuid')
    def test_prepare_iso_image(self, mock_generate_uuid):
        vi = self._make_vm_config_info(is_iso=True)
        tmp_dir_loc, tmp_image_ds_loc = self._vmops._prepare_iso_image(vi)

        expected_tmp_dir_path = '[%s] vmware_temp/tmp-uuid' % (self._ds.name)
        expected_image_path = '[%s] vmware_temp/tmp-uuid/%s/%s.iso' % (
                self._ds.name, self._image_id, self._image_id)

        self.assertEqual(str(tmp_dir_loc), expected_tmp_dir_path)
        self.assertEqual(str(tmp_image_ds_loc), expected_image_path)

    @mock.patch.object(uuidutils, 'generate_uuid', return_value='tmp-uuid')
    def test_prepare_sparse_image(self, mock_generate_uuid):
        vi = self._make_vm_config_info(is_sparse_disk=True)
        tmp_dir_loc, tmp_image_ds_loc = self._vmops._prepare_sparse_image(vi)

        expected_tmp_dir_path = '[%s] vmware_temp/tmp-uuid' % (self._ds.name)
        expected_image_path = '[%s] vmware_temp/tmp-uuid/%s/%s' % (
                self._ds.name, self._image_id, "tmp-sparse.vmdk")

        self.assertEqual(str(tmp_dir_loc), expected_tmp_dir_path)
        self.assertEqual(str(tmp_image_ds_loc), expected_image_path)

    @mock.patch.object(ds_util, 'mkdir')
    @mock.patch.object(vm_util, 'create_virtual_disk')
    @mock.patch.object(vmops.VMwareVMOps, '_delete_datastore_file')
    @mock.patch.object(uuidutils, 'generate_uuid', return_value='tmp-uuid')
    def test_prepare_flat_image(self,
                                mock_generate_uuid,
                                mock_delete_datastore_file,
                                mock_create_virtual_disk,
                                mock_mkdir):
        vi = self._make_vm_config_info()
        tmp_dir_loc, tmp_image_ds_loc = self._vmops._prepare_flat_image(vi)

        expected_tmp_dir_path = '[%s] vmware_temp/tmp-uuid' % (self._ds.name)
        expected_image_path = '[%s] vmware_temp/tmp-uuid/%s/%s-flat.vmdk' % (
                self._ds.name, self._image_id, self._image_id)
        expected_image_path_parent = '[%s] vmware_temp/tmp-uuid/%s' % (
                self._ds.name, self._image_id)
        expected_path_to_create = '[%s] vmware_temp/tmp-uuid/%s/%s.vmdk' % (
                self._ds.name, self._image_id, self._image_id)

        mock_mkdir.assert_called_once_with(
                self._session, DsPathMatcher(expected_image_path_parent),
                self._dc_info.ref)

        self.assertEqual(str(tmp_dir_loc), expected_tmp_dir_path)
        self.assertEqual(str(tmp_image_ds_loc), expected_image_path)

        image_info = vi.ii
        mock_create_virtual_disk.assert_called_once_with(
            self._session, self._dc_info.ref,
            image_info.adapter_type,
            image_info.disk_type,
            DsPathMatcher(expected_path_to_create),
            image_info.file_size_in_kb)
        mock_delete_datastore_file.assert_called_once_with(
                DsPathMatcher(expected_image_path),
                self._dc_info.ref)

    @mock.patch.object(ds_util, 'file_move')
    def test_cache_iso_image(self, mock_file_move):
        vi = self._make_vm_config_info(is_iso=True)
        tmp_image_ds_loc = mock.Mock()

        self._vmops._cache_iso_image(vi, tmp_image_ds_loc)

        mock_file_move.assert_called_once_with(
                self._session, self._dc_info.ref,
                tmp_image_ds_loc.parent,
                DsPathMatcher('[fake_ds] vmware_base/%s' % self._image_id))

    @mock.patch.object(ds_util, 'file_move')
    def test_cache_flat_image(self, mock_file_move):
        vi = self._make_vm_config_info()
        tmp_image_ds_loc = mock.Mock()

        self._vmops._cache_flat_image(vi, tmp_image_ds_loc)

        mock_file_move.assert_called_once_with(
                self._session, self._dc_info.ref,
                tmp_image_ds_loc.parent,
                DsPathMatcher('[fake_ds] vmware_base/%s' % self._image_id))

    @mock.patch.object(ds_util, 'disk_move')
    @mock.patch.object(ds_util, 'mkdir')
    def test_cache_stream_optimized_image(self, mock_mkdir, mock_disk_move):
        vi = self._make_vm_config_info()

        self._vmops._cache_stream_optimized_image(vi, mock.sentinel.tmp_image)

        mock_mkdir.assert_called_once_with(
            self._session,
            DsPathMatcher('[fake_ds] vmware_base/%s' % self._image_id),
            self._dc_info.ref)
        mock_disk_move.assert_called_once_with(
                self._session, self._dc_info.ref,
                mock.sentinel.tmp_image,
                DsPathMatcher('[fake_ds] vmware_base/%s/%s.vmdk' %
                              (self._image_id, self._image_id)))

    @mock.patch.object(ds_util, 'file_move')
    @mock.patch.object(vm_util, 'copy_virtual_disk')
    @mock.patch.object(vmops.VMwareVMOps, '_delete_datastore_file')
    def test_cache_sparse_image(self,
                                mock_delete_datastore_file,
                                mock_copy_virtual_disk,
                                mock_file_move):
        vi = self._make_vm_config_info(is_sparse_disk=True)

        sparse_disk_path = "[%s] vmware_temp/tmp-uuid/%s/tmp-sparse.vmdk" % (
                self._ds.name, self._image_id)
        tmp_image_ds_loc = ds_obj.DatastorePath.parse(sparse_disk_path)

        self._vmops._cache_sparse_image(vi, tmp_image_ds_loc)

        target_disk_path = "[%s] vmware_temp/tmp-uuid/%s/%s.vmdk" % (
                self._ds.name,
                self._image_id, self._image_id)
        mock_copy_virtual_disk.assert_called_once_with(
                self._session, self._dc_info.ref,
                sparse_disk_path,
                DsPathMatcher(target_disk_path))

    def test_get_storage_policy_none(self):
        flavor = objects.Flavor(name='m1.small',
                                memory_mb=6,
                                vcpus=28,
                                root_gb=496,
                                ephemeral_gb=8128,
                                swap=33550336,
                                extra_specs={})
        self.flags(pbm_enabled=True,
                   pbm_default_policy='fake-policy', group='vmware')
        extra_specs = self._vmops._get_extra_specs(flavor)
        self.assertEqual('fake-policy', extra_specs.storage_policy)

    def test_get_storage_policy_extra_specs(self):
        extra_specs = {'vmware:storage_policy': 'flavor-policy'}
        flavor = objects.Flavor(name='m1.small',
                                memory_mb=6,
                                vcpus=28,
                                root_gb=496,
                                ephemeral_gb=8128,
                                swap=33550336,
                                extra_specs=extra_specs)
        self.flags(pbm_enabled=True,
                   pbm_default_policy='default-policy', group='vmware')
        extra_specs = self._vmops._get_extra_specs(flavor)
        self.assertEqual('flavor-policy', extra_specs.storage_policy)

    def test_get_base_folder_not_set(self):
        self.flags(image_cache_subdirectory_name='vmware_base')
        base_folder = self._vmops._get_base_folder()
        self.assertEqual('vmware_base', base_folder)

    def test_get_base_folder_host_ip(self):
        self.flags(my_ip='7.7.7.7',
                   image_cache_subdirectory_name='_base')
        base_folder = self._vmops._get_base_folder()
        self.assertEqual('7.7.7.7_base', base_folder)

    def test_get_base_folder_cache_prefix(self):
        self.flags(cache_prefix='my_prefix', group='vmware')
        self.flags(image_cache_subdirectory_name='_base')
        base_folder = self._vmops._get_base_folder()
        self.assertEqual('my_prefix_base', base_folder)

    def _test_reboot_vm(self, reboot_type="SOFT"):

        expected_methods = ['get_object_properties']
        if reboot_type == "SOFT":
            expected_methods.append('RebootGuest')
        else:
            expected_methods.append('ResetVM_Task')

        query = {}
        query['runtime.powerState'] = "poweredOn"
        query['summary.guest.toolsStatus'] = "toolsOk"
        query['summary.guest.toolsRunningStatus'] = "guestToolsRunning"

        def fake_call_method(module, method, *args, **kwargs):
            expected_method = expected_methods.pop(0)
            self.assertEqual(expected_method, method)
            if (expected_method == 'get_object_properties'):
                return 'fake-props'
            elif (expected_method == 'ResetVM_Task'):
                return 'fake-task'

        with contextlib.nested(
                mock.patch.object(vm_util, "get_vm_ref",
                                  return_value='fake-vm-ref'),
                mock.patch.object(vm_util, "get_values_from_object_properties",
                                  return_value=query),
                mock.patch.object(self._session, "_call_method",
                                  fake_call_method),
                mock.patch.object(self._session, "_wait_for_task")
        ) as (_get_vm_ref, _get_values_from_object_properties,
              fake_call_method, _wait_for_task):
            self._vmops.reboot(self._instance, self.network_info, reboot_type)
            _get_vm_ref.assert_called_once_with(self._session,
                                                self._instance)

            _get_values_from_object_properties.assert_called_once_with(
                                                              self._session,
                                                              'fake-props')
            if reboot_type == "HARD":
                _wait_for_task.assert_has_calls([
                       mock.call('fake-task')])

    def test_reboot_vm_soft(self):
        self._test_reboot_vm()

    def test_reboot_vm_hard(self):
        self._test_reboot_vm(reboot_type="HARD")
