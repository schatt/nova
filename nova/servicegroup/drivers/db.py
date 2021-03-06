# Copyright 2012 IBM Corp.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import timeutils
import six

from nova import context
from nova.i18n import _, _LE
from nova import objects
from nova.servicegroup import api
from nova.servicegroup.drivers import base


CONF = cfg.CONF
CONF.import_opt('service_down_time', 'nova.service')

LOG = logging.getLogger(__name__)


class DbDriver(base.Driver):

    def __init__(self, *args, **kwargs):
        self.service_down_time = CONF.service_down_time

    def join(self, member, group, service=None):
        """Add a new member to a service group.

        :param member: the joined member ID/name
        :param group: the group ID/name, of the joined member
        :param service: a `nova.service.Service` object
        """
        LOG.debug('DB_Driver: join new ServiceGroup member %(member)s to '
                  'the %(group)s group, service = %(service)s',
                  {'member': member, 'group': group,
                   'service': service})
        if service is None:
            raise RuntimeError(_('service is a mandatory argument for DB based'
                                 ' ServiceGroup driver'))
        report_interval = service.report_interval
        if report_interval:
            service.tg.add_timer(report_interval, self._report_state,
                                 api.INITIAL_REPORTING_DELAY, service)

    def is_up(self, service_ref):
        """Moved from nova.utils
        Check whether a service is up based on last heartbeat.
        """
        last_heartbeat = service_ref['updated_at'] or service_ref['created_at']
        if isinstance(last_heartbeat, six.string_types):
            # NOTE(russellb) If this service_ref came in over rpc via
            # conductor, then the timestamp will be a string and needs to be
            # converted back to a datetime.
            last_heartbeat = timeutils.parse_strtime(last_heartbeat)
        else:
            # Objects have proper UTC timezones, but the timeutils comparison
            # below does not (and will fail)
            last_heartbeat = last_heartbeat.replace(tzinfo=None)
        # Timestamps in DB are UTC.
        elapsed = timeutils.delta_seconds(last_heartbeat, timeutils.utcnow())
        is_up = abs(elapsed) <= self.service_down_time
        if not is_up:
            LOG.debug('Seems service is down. Last heartbeat was %(lhb)s. '
                      'Elapsed time is %(el)s',
                      {'lhb': str(last_heartbeat), 'el': str(elapsed)})
        return is_up

    def get_all(self, group_id):
        """Returns ALL members of the given group
        """
        LOG.debug('DB_Driver: get_all members of the %s group', group_id)
        rs = []
        ctxt = context.get_admin_context()
        services = objects.ServiceList.get_by_topic(ctxt, group_id)
        for service in services:
            if self.is_up(service):
                rs.append(service.host)
        return rs

    def _report_state(self, service):
        """Update the state of this service in the datastore."""
        try:
            service.service_ref.report_count += 1
            service.service_ref.save()

            # TODO(termie): make this pattern be more elegant.
            if getattr(service, 'model_disconnected', False):
                service.model_disconnected = False
                LOG.error(_LE('Recovered model server connection!'))

        # TODO(vish): this should probably only catch connection errors
        except Exception:
            if not getattr(service, 'model_disconnected', False):
                service.model_disconnected = True
                LOG.exception(_LE('model server went away'))
