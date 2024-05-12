# -*- coding: utf-8 -*-

"""
Copyright (C) 2024, Zato Source s.r.o. https://zato.io

Licensed under AGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
import logging

# Django
from django.template.response import TemplateResponse

# Zato
from zato.admin.web.views import BaseCallView, invoke_action_handler, method_allowed

# ################################################################################################################################
# ################################################################################################################################

logger = logging.getLogger(__name__)

# ################################################################################################################################
# ################################################################################################################################

class IDE(BaseCallView):
    method_allowed = 'GET'
    url_name = 'service-ide'
    template = 'zato/service/ide.html'
    service_name = 'zato.service.ide.service-ide'

    def get_input_dict(self):

        # This will point either to a service or to a full file name
        object_type = self.req.zato.args.object_type

        if object_type == 'service':
            current_service_name = self.req.zato.args.name
            fs_location = ''
        else:
            current_service_name = ''
            fs_location = self.req.zato.args.name

        return {
            'cluster_id': self.cluster_id,
            'service_name': current_service_name,
            'fs_location': fs_location,
        }

# ################################################################################################################################

    def build_http_response(self, response):

        return_data = {
            'cluster_id': self.req.zato.cluster_id,
            'cluster_name': self.req.zato.cluster.name,
            'current_object_name': self.req.zato.args.name,
            'current_object_name_url_safe': self.req.zato.args.name.replace('~', '/'),
            'data': response.data,
        }

        return TemplateResponse(self.req, self.template, return_data)

# ################################################################################################################################
# ################################################################################################################################

@method_allowed('POST')
def get_service(req, service_name):
    return invoke_action_handler(req, 'zato.service.ide.get-service', extra={'service_name': service_name})

# ################################################################################################################################
# ################################################################################################################################

@method_allowed('POST')
def get_file(req):
    fs_location = req.GET['fs_location']
    return invoke_action_handler(req, 'zato.service.ide.get-file', extra={'fs_location': fs_location})

# ################################################################################################################################
# ################################################################################################################################

@method_allowed('POST')
def get_file_list(req):
    return invoke_action_handler(req, 'zato.service.ide.get-file-list')

# ################################################################################################################################
# ################################################################################################################################

@method_allowed('POST')
def get_service_list(req):
    return invoke_action_handler(req, 'zato.service.ide.service-ide')

# ################################################################################################################################
# ################################################################################################################################
