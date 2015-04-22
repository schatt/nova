# Copyright 2013 IBM Corp.
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

import copy

import webob

from nova.api.openstack.compute import plugins
from nova.api.openstack.compute.plugins.v3 import extension_info
from nova import exception
from nova import policy
from nova import test
from nova.tests.unit.api.openstack import fakes


FAKE_UPDATED_DATE = extension_info.FAKE_UPDATED_DATE


class fake_extension(object):
    def __init__(self, name, alias, description, version):
        self.name = name
        self.alias = alias
        self.__doc__ = description
        self.version = version


fake_extensions = {
    'ext1-alias': fake_extension('ext1', 'ext1-alias', 'ext1 description', 1),
    'ext2-alias': fake_extension('ext2', 'ext2-alias', 'ext2 description', 2),
    'ext3-alias': fake_extension('ext3', 'ext3-alias', 'ext3 description', 1)
}


simulated_extension_list = {
    'servers': fake_extension('Servers', 'servers', 'Servers.', 1),
    'images': fake_extension('Images', 'images', 'Images.', 2),
    'os-quota-sets': fake_extension('Quotas', 'os-quota-sets',
                                    'Quotas management support', 1),
    'os-cells': fake_extension('Cells', 'os-cells',
                                    'Cells description', 1),
    'os-flavor-access': fake_extension('FlavorAccess', 'os-flavor-access',
                                    'Flavor access support.', 1)
}


def fake_policy_enforce(context, action, target, do_raise=True):
    return True


def fake_policy_enforce_selective(context, action, target, do_raise=True):
    if action == 'os_compute_api:ext1-alias:discoverable':
        raise exception.Forbidden
    else:
        return True


class ExtensionInfoTest(test.NoDBTestCase):

    def setUp(self):
        super(ExtensionInfoTest, self).setUp()
        ext_info = plugins.LoadedExtensionInfo()
        ext_info.extensions = fake_extensions
        self.controller = extension_info.ExtensionInfoController(ext_info)

    def test_extension_info_list(self):
        self.stubs.Set(policy, 'enforce', fake_policy_enforce)
        req = fakes.HTTPRequestV3.blank('/extensions')
        res_dict = self.controller.index(req)
        self.assertEqual(3, len(res_dict['extensions']))
        for e in res_dict['extensions']:
            self.assertIn(e['alias'], fake_extensions)
            self.assertEqual(e['name'], fake_extensions[e['alias']].name)
            self.assertEqual(e['alias'], fake_extensions[e['alias']].alias)
            self.assertEqual(e['description'],
                             fake_extensions[e['alias']].__doc__)
            self.assertEqual(e['updated'], FAKE_UPDATED_DATE)
            self.assertEqual(e['links'], [])
            self.assertEqual(6, len(e))

    def test_extension_info_show(self):
        self.stubs.Set(policy, 'enforce', fake_policy_enforce)
        req = fakes.HTTPRequestV3.blank('/extensions/ext1-alias')
        res_dict = self.controller.show(req, 'ext1-alias')
        self.assertEqual(1, len(res_dict))
        self.assertEqual(res_dict['extension']['name'],
                         fake_extensions['ext1-alias'].name)
        self.assertEqual(res_dict['extension']['alias'],
                         fake_extensions['ext1-alias'].alias)
        self.assertEqual(res_dict['extension']['description'],
                         fake_extensions['ext1-alias'].__doc__)
        self.assertEqual(res_dict['extension']['updated'], FAKE_UPDATED_DATE)
        self.assertEqual(res_dict['extension']['links'], [])
        self.assertEqual(6, len(res_dict['extension']))

    def test_extension_info_list_not_all_discoverable(self):
        self.stubs.Set(policy, 'enforce', fake_policy_enforce_selective)
        req = fakes.HTTPRequestV3.blank('/extensions')
        res_dict = self.controller.index(req)
        self.assertEqual(2, len(res_dict['extensions']))
        for e in res_dict['extensions']:
            self.assertNotEqual('ext1-alias', e['alias'])
            self.assertIn(e['alias'], fake_extensions)
            self.assertEqual(e['name'], fake_extensions[e['alias']].name)
            self.assertEqual(e['alias'], fake_extensions[e['alias']].alias)
            self.assertEqual(e['description'],
                             fake_extensions[e['alias']].__doc__)
            self.assertEqual(e['updated'], FAKE_UPDATED_DATE)
            self.assertEqual(e['links'], [])
            self.assertEqual(6, len(e))


class ExtensionInfoV21Test(test.NoDBTestCase):

    def setUp(self):
        super(ExtensionInfoV21Test, self).setUp()
        ext_info = plugins.LoadedExtensionInfo()
        ext_info.extensions = simulated_extension_list
        self.controller = extension_info.ExtensionInfoController(ext_info)
        self.stubs.Set(policy, 'enforce', fake_policy_enforce)

    def test_extension_info_list(self):
        req = fakes.HTTPRequest.blank('/extensions')
        res_dict = self.controller.index(req)
        self.assertEqual(12, len(res_dict['extensions']))

        expected_output = copy.deepcopy(simulated_extension_list)
        del expected_output['images']
        del expected_output['servers']
        expected_output['os-cell-capacities'] = fake_extension(
            'CellCapacities', 'os-cell-capacities', '', -1)
        expected_output['os-server-sort-keys'] = fake_extension(
            'ServerSortKeys', 'os-server-sort-keys', '', -1)
        expected_output['os-user-quotas'] = fake_extension(
            'UserQuotas', 'os-user-quotas', '', -1)
        expected_output['os-extended-quotas'] = fake_extension(
            'ExtendedQuotas', 'os-extended-quotas', '', -1)
        expected_output['os-create-server-ext'] = fake_extension(
            'Createserverext', 'os-create-server-ext', '', -1)
        expected_output['OS-EXT-IPS'] = fake_extension(
            'ExtendedIps', 'OS-EXT-IPS', '', -1)
        expected_output['OS-EXT-IPS-MAC'] = fake_extension(
            'ExtendedIpsMac', 'OS-EXT-IPS-MAC', '', -1)
        expected_output['os-server-list-multi-status'] = fake_extension(
            'ServerListMultiStatus', 'os-server-list-multi-status', '', -1)
        expected_output['os-server-start-stop'] = fake_extension(
            'ServerStartStop', 'os-server-start-stop', '', -1)

        for e in res_dict['extensions']:
            self.assertIn(e['alias'], expected_output)
            self.assertEqual(e['name'], expected_output[e['alias']].name)
            self.assertEqual(e['alias'], expected_output[e['alias']].alias)
            self.assertEqual(e['description'],
                             expected_output[e['alias']].__doc__)
            self.assertEqual(e['updated'], FAKE_UPDATED_DATE)
            self.assertEqual(e['links'], [])
            self.assertEqual(6, len(e))

    def test_extension_info_show(self):
        req = fakes.HTTPRequest.blank('/extensions/os-cells')
        res_dict = self.controller.show(req, 'os-cells')
        self.assertEqual(1, len(res_dict))
        self.assertEqual(res_dict['extension']['name'],
                         simulated_extension_list['os-cells'].name)
        self.assertEqual(res_dict['extension']['alias'],
                         simulated_extension_list['os-cells'].alias)
        self.assertEqual(res_dict['extension']['description'],
                         simulated_extension_list['os-cells'].__doc__)
        self.assertEqual(res_dict['extension']['updated'], FAKE_UPDATED_DATE)
        self.assertEqual(res_dict['extension']['links'], [])
        self.assertEqual(6, len(res_dict['extension']))

    def test_extension_info_show_servers_not_present(self):
        req = fakes.HTTPRequest.blank('/extensions/servers')
        self.assertRaises(webob.exc.HTTPNotFound, self.controller.show,
                          req, 'servers')
