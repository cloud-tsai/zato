# -*- coding: utf-8 -*-

"""
Copyright (C) 2018, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

# stdlib
import logging

# Zato
from zato.admin.web.forms import ChangePasswordForm
from zato.admin.web.forms.security.xpath import CreateForm, EditForm
from zato.admin.web.views import change_password as _change_password, CreateEdit, Delete as _Delete, Index as _Index, method_allowed
from zato.common.odb.model import XPathSecurity

logger = logging.getLogger(__name__)

class Index(_Index):
    method_allowed = 'GET'
    url_name = 'security-xpath'
    template = 'zato/security/xpath.html'
    service_name = 'zato.security.xpath.get-list'
    output_class = XPathSecurity
    paginate = True

    class SimpleIO(_Index.SimpleIO):
        input_required = ('cluster_id',)
        output_required = ('id', 'name', 'is_active', 'username', 'username_expr')
        output_optional = ('password_expr',)
        output_repeated = True

    def handle(self):
        return {
            'create_form': CreateForm(),
            'edit_form': EditForm(prefix='edit'),
            'change_password_form': ChangePasswordForm()
        }

class _CreateEdit(CreateEdit):
    method_allowed = 'POST'

    class SimpleIO(CreateEdit.SimpleIO):
        input_required = ('name', 'is_active', 'username', 'username_expr')
        input_optional = ('password_expr',)
        output_required = ('id', 'name')

    def success_message(self, item):
        return 'Successfully {0} the XPath security definition [{1}]'.format(self.verb, item.name)

class Create(_CreateEdit):
    url_name = 'security-xpath-create'
    service_name = 'zato.security.xpath.create'

class Edit(_CreateEdit):
    url_name = 'security-xpath-edit'
    form_prefix = 'edit-'
    service_name = 'zato.security.xpath.edit'

class Delete(_Delete):
    url_name = 'security-xpath-delete'
    error_message = 'Could not delete the XPath security definition'
    service_name = 'zato.security.xpath.delete'

@method_allowed('POST')
def change_password(req):
    return _change_password(req, 'zato.security.xpath.change-password', success_msg='XPath security definition updated')
