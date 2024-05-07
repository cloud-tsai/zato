# -*- coding: utf-8 -*-

"""
Copyright (C) 2024, Zato Source s.r.o. https://zato.io

Licensed under AGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
import logging
from collections import namedtuple
from datetime import datetime
from http import HTTPStatus
from json import dumps
from traceback import format_exc

# dateutil
from dateutil.relativedelta import relativedelta

# Django
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.template.response import TemplateResponse

# Zato
from zato.admin.web import from_utc_to_user
from zato.admin.web.forms.service import CreateForm, EditForm
from zato.admin.web.views import CreateEdit, Delete as _Delete, Index as _Index, method_allowed, upload_to_server
from zato.common.api import ZATO_NONE
from zato.common.ext.validate_ import is_boolean
from zato.common.odb.model import Service

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from zato.common.typing_ import any_, anylist

# ################################################################################################################################
# ################################################################################################################################

logger = logging.getLogger(__name__)

# ################################################################################################################################
# ################################################################################################################################

ExposedThrough = namedtuple('ExposedThrough', ['id', 'name', 'url']) # type: ignore
DeploymentInfo = namedtuple('DeploymentInfo', ['server_name', 'details']) # type: ignore

# ################################################################################################################################

def get_public_wsdl_url(cluster:'any_', service_name:'str') -> 'str':
    """ Returns an address under which a service's WSDL is publically available.
    """
    return 'http://{}:{}/zato/wsdl?service={}&cluster_id={}'.format(cluster.lb_host,
        cluster.lb_port, service_name, cluster.id)

# ################################################################################################################################

def _get_channels(client:'any_', cluster:'any_', id:'str', channel_type:'str') -> 'anylist':
    """ Returns a list of channels of a given type for the given service.
    """
    input_dict = {
        'id': id,
        'channel_type': channel_type
    }
    out = []

    for item in client.invoke('zato.service.get-channel-list', input_dict):

        if channel_type in ['plain_http']:
            url = reverse('http-soap')
            url += '?connection=channel&transport={}'.format(channel_type)
            url += '&cluster={}'.format(cluster.id)
        else:
            url = reverse('channel-' + channel_type)
            url += '?cluster={}'.format(cluster.id)

        url += '&highlight={}'.format(item.id)

        channel = ExposedThrough(item.id, item.name, url)
        out.append(channel)

    return out

# ################################################################################################################################

class Index(_Index):
    """ A view for listing the services along with their basic statistics.
    """
    method_allowed = 'GET'
    url_name = 'service'
    template = 'zato/service/index.html'
    service_name = 'zato.service.get-list'
    output_class = Service
    paginate = True

    class SimpleIO(_Index.SimpleIO):
        input_required = 'cluster_id',
        input_optional = 'query',
        output_required = 'id', 'name', 'is_active', 'is_internal', 'impl_name', 'may_be_deleted', 'usage', 'slow_threshold'
        output_optional = 'is_json_schema_enabled', 'needs_json_schema_err_details', 'is_rate_limit_active', \
            'rate_limit_type', 'rate_limit_def', 'rate_limit_check_parent_def'
        output_repeated = True

    def handle(self):
        return {
            'create_form': CreateForm(),
            'edit_form': EditForm(prefix='edit')
        }

# ################################################################################################################################

@method_allowed('POST')
def create(req:'any_') -> 'None':
    pass

# ################################################################################################################################

class Edit(CreateEdit):
    method_allowed = 'POST'
    url_name = 'service-edit'
    form_prefix = 'edit-'
    service_name = 'zato.service.edit'

    class SimpleIO(CreateEdit.SimpleIO):
        input_required = 'id', 'is_active', 'slow_threshold', 'is_json_schema_enabled', 'needs_json_schema_err_details', \
            'is_rate_limit_active', 'rate_limit_type', 'rate_limit_def', 'rate_limit_check_parent_def'
        output_required = 'id', 'name', 'impl_name', 'is_internal', 'usage', 'may_be_deleted'

    def success_message(self, item:'any_') -> 'str':
        return 'Successfully {} service `{}`'.format(self.verb, item.name)

# ################################################################################################################################

@method_allowed('GET')
def overview(req:'HttpRequest', service_name:'str') -> 'TemplateResponse':

    cluster_id = req.GET.get('cluster')
    service = None

    create_form = CreateForm()
    edit_form = EditForm(prefix='edit')

    if cluster_id and req.method == 'GET':

        input_dict = {
            'name': service_name,
            'cluster_id': req.zato.cluster_id # type: ignore
        }

        response = req.zato.client.invoke('zato.service.get-by-name', input_dict) # type: ignore
        if response.has_data:
            service = Service()

            for name in('id', 'name', 'is_active', 'impl_name', 'is_internal',
                  'usage', 'last_duration', 'usage_min', 'usage_max',
                  'usage_mean', 'last_timestamp'):

                value = getattr(response.data, name, None)

                if name in('is_active', 'is_internal'):
                    value = is_boolean(value)

                if name == 'last_timestamp':

                    if value:
                        service.last_timestamp_utc = value # type: ignore
                        service.last_timestamp = from_utc_to_user(value+'+00:00', req.zato.user_profile) # type: ignore

                    continue

                setattr(service, name, value)

            now = datetime.utcnow()
            start = now+relativedelta(minutes=-60)

            response = req.zato.client.invoke( # type: ignore
                'zato.stats.get-by-service',
                {'service_id':service.id, 'start':start, 'stop':now}
            )

            if response.has_data:
                for name in('mean_trend', 'usage_trend', 'min_resp_time', 'max_resp_time', 'mean', 'usage', 'rate'):
                    value = getattr(response.data, name, ZATO_NONE)
                    if not value or value == ZATO_NONE:
                        value = ''

                    setattr(service, 'time_{}_1h'.format(name), value)

            for channel_type in('plain_http', 'amqp', 'jms-wmq', 'zmq'):
                channels = _get_channels(req.zato.client, req.zato.cluster, service.id, channel_type) # type: ignore
                getattr(service, channel_type.replace('jms-', '') + '_channels').extend(channels)

            deployment_service = 'zato.service.get-deployment-info-list'
            deployment_request = {'id': service.id, 'needs_details':True}

            for item in req.zato.client.invoke(deployment_service, deployment_request): # type: ignore
                service.deployment_info.append(DeploymentInfo(item.server_name, item.details))

            response = req.zato.client.invoke('zato.scheduler.job.get-list', {'cluster_id':cluster_id}) # type: ignore
            if response.has_data:
                for item in response.data:
                    if item.service_name == service_name:
                        url = reverse('scheduler')
                        url += '?cluster={}'.format(cluster_id)
                        url += '&highlight={}'.format(item.id)
                        service.scheduler_jobs.append(ExposedThrough(item.id, item.name, url))

    return_data = {
        'zato_clusters':req.zato.clusters, # type: ignore
        'service': service,
        'cluster_id':cluster_id,
        'search_form':req.zato.search_form, # type: ignore
        'create_form':create_form,
        'edit_form':edit_form,
        }

    return TemplateResponse(req, 'zato/service/overview.html', return_data)

# ################################################################################################################################

class Delete(_Delete):
    url_name = 'service-delete'
    error_message = 'Service could not be deleted'
    service_name = 'zato.service.delete'

# ################################################################################################################################

@method_allowed('POST')
def package_upload(req:'HttpRequest', cluster_id:'str') -> 'any_':
    """ Handles a service package file upload.
    """
    return upload_to_server(req, cluster_id, 'zato.service.upload-package', 'Could not upload the service package, e:`{}`')

# ################################################################################################################################

@method_allowed('POST')
def invoke(req:'HttpRequest', name:'str', cluster_id:'str') -> 'HttpResponse':
    """ Executes a service directly, even if it isn't exposed through any channel.
    """

    # Local variables
    status_code = HTTPStatus.BAD_REQUEST
    response_time_human = ''
    content = {
        'content': '',
        'response_time_ms': '1234',
        'response_time_human': '1.12 s',
    }

    try:
        input_dict = {}
        input_dict['payload'] = req.POST.get('data-request', '')
        input_dict['to_json'] = True
        input_dict['needs_response_time'] = True

        response = req.zato.client.invoke(name, **input_dict) # type: ignore

    except Exception as e:
        msg = 'Service could not be invoked; name:`{}`, cluster_id:`{}`, e:`{}`'.format(name, cluster_id, format_exc())
        logger.error(msg)
        data = e.args
        status_code = HTTPStatus.BAD_REQUEST
    else:
        try:
            if response.ok:
                data = response.inner_service_response or '(None)'
                status_code = HTTPStatus.OK
            else:
                data = response.details
        except Exception:
            data = response.details

    content['data'] = data
    content = dumps(content)

    out = HttpResponse()
    out.status_code = status_code
    out.content = content

    print()
    print(111, content)
    print(222, response.inner.headers)
    print()

    return out

# ################################################################################################################################
