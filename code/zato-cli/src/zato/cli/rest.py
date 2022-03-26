# -*- coding: utf-8 -*-

"""
Copyright (C) 2022, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
import sys
from uuid import uuid4

# Zato
from zato.cli import ServerAwareCommand
from zato.common.api import CONNECTION, ZATO_NONE
from zato.common.util.api import fs_safe_now
from zato.common.util.cli import BasicAuthManager

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from argparse import Namespace
    from zato.common.typing_ import anytuple, stranydict
    Namespace = Namespace

# ################################################################################################################################
# ################################################################################################################################

class Config:
    ServiceName = 'pub.zato.ping'
    MaxBytesRequests = 500   # 0.5k because requests are usually shorter
    MaxBytesResponses = 5000 # 5k because responses are usually longer

# ################################################################################################################################
# ################################################################################################################################

class SecurityAwareCommand(ServerAwareCommand):

    def _extract_credentials(self, name:'str', credentials:'str') -> 'anytuple':

        credentials_lower = credentials.lower()

        if credentials_lower == 'true':
            username = name
            password = 'api.password.' + uuid4().hex

        elif credentials_lower == 'false':
            username, password = None, None

            """
            Request; service:`zato.security.apikey.create`,
                data:`{'cluster_id': '1', 'name': 'My API Key', 'is_active': 'on', 'username': 'X-My-Header',
                'is_rate_limit_active': None,
                'rate_limit_type': 'APPROXIMATE', 'rate_limit_def': None, 'rate_limit_check_parent_def': None}`
            Response; service:`zato.security.apikey.create`,
            data:`{"zato_security_apikey_create_response": {"id": 64, "name": "My API Key"}}`

            Request; service:`zato.security.apikey.change-password`,
                data:`{'id': '64', 'password1': '******', 'password2': '******'}`
            Response; service:`zato.security.apikey.change-password`,
                data:`{"zato_security_apikey_change_password_response": {"id": 64}}`
            """

        else:
            _credentials = credentials.split(',')
            _credentials = [elem.strip() for elem in _credentials]
            username, password = _credentials

        return username, password

# ################################################################################################################################

    def _get_security_id(self, *, name:'str', basic_auth:'str', api_key:'str') -> 'stranydict':

        out = {}
        username = None
        password = None

        if basic_auth:

            username, password = self._extract_credentials(name, basic_auth)
            manager = BasicAuthManager(self, name, True, username, 'API', password)
            response = manager.create()
            out['security_id'] = response['id']

        elif api_key:
            out['security_id'] = ZATO_NONE

        else:
            out['security_id'] = ZATO_NONE

        # Append credentials, even if they are None
        out['username'] = username
        out['password'] = password

        # No matter what we had on input, we can return our output now.
        return out

# ################################################################################################################################
# ################################################################################################################################

class CreateChannel(SecurityAwareCommand):
    """ Creates a new REST channel.
    """
    opts = [
        {'name':'--name', 'help':'Name of the channel to create', 'required':False,},
        {'name':'--is-active', 'help':'Should the channel be active upon creation', 'required':False},
        {'name':'--url-path', 'help':'URL path to assign to the channel', 'required':False},
        {'name':'--service', 'help':'Service reacting to requests sent to the channel',
            'required':False, 'default':Config.ServiceName},
        {'name':'--basic-auth', 'help':'HTTP Basic Auth credentials for the channel', 'required':False},
        {'name':'--api-key', 'help':'API key-based credentials for the channel', 'required':False},
        {'name':'--store-requests', 'help':'How many requests to store in audit log',
            'required':False, 'default':0, 'type': int},
        {'name':'--store-responses', 'help':'How many responses to store in audit log',
            'required':False, 'default':0, 'type': int},
        {'name':'--max-bytes-requests', 'help':'How many bytes of each request to store',
            'required':False, 'default':500, 'type': int},
        {'name':'--max-bytes-responses', 'help':'How many bytes of each response to store',
            'required':False, 'default':500, 'type': int},
        {'name':'--path', 'help':'Path to a Zato server', 'required':True},
    ]

# ################################################################################################################################

    def execute(self, args:'Namespace'):

        name = getattr(args, 'name', None)
        is_active = getattr(args, 'is_active', None)
        url_path = getattr(args, 'url_path', None)
        channel_service = getattr(args, 'service', None) or Config.ServiceName
        basic_auth = getattr(args, 'basic_auth', '')
        api_key = getattr(args, 'api_key', '')
        store_requests = getattr(args, 'store_requests', 0)
        store_responses = getattr(args, 'store_responses', 0)
        max_bytes_requests = getattr(args, 'max_bytes_requests', None) or Config.MaxBytesRequests
        max_bytes_responses = getattr(args, 'max_bytes_requests', None) or Config.MaxBytesResponses

        # For later use
        now = fs_safe_now()

        # Assume that the channel should be active
        is_active = getattr(args, 'is_active', True)
        if is_active is None:
            is_active = True

        # Generate a name if one is not given
        name = name or 'auto.rest.channel.' + now

        # If we have no URL path, base it on the auto-generate name
        if not url_path:
            url_path = '/'+ name

        # Enable the audit log if told to
        is_audit_log_received_active = bool(store_requests)
        is_audit_log_sent_active = bool(store_responses)

        # Obtain the security ID based on input data, creating the definition if necessary.
        sec_name = 'auto.sec.' + now
        security_info = self._get_security_id(name=sec_name, basic_auth=basic_auth, api_key=api_key)
        security_id = security_info.pop('security_id')

        # API service to invoke
        service = 'zato.http-soap.create'

        # API request to send
        request = {
            'name': name,
            'url_path': url_path,
            'service': channel_service,
            'is_active': is_active,
            'connection': CONNECTION.CHANNEL,
            'security_id': security_id,
            'is_audit_log_received_active': is_audit_log_received_active,
            'is_audit_log_sent_active': is_audit_log_sent_active,

            'max_len_messages_received': store_requests,
            'max_len_messages_sent': store_responses,

            'max_bytes_per_message_received': max_bytes_requests,
            'max_bytes_per_message_sent': max_bytes_responses,
        }

        # Invoke the base service that creates a channel ..
        response = self._invoke_service(service, request)

        # .. update the response with the channel security definition's details ..
        response.update(security_info)

        # .. finally, log the response for the caller.
        self._log_response(response, needs_stdout=True)

# ################################################################################################################################
# ################################################################################################################################

class DeleteChannel(SecurityAwareCommand):
    """ Deletes a REST channel.
    """
    opts = [
        {'name':'--id', 'help':'ID of the channel to delete', 'required':False},
        {'name':'--name', 'help':'Name of the channel to delete', 'required':False},
        {'name':'--path', 'help':'Path to a Zato server', 'required':True},
    ]

    def execute(self, args:'Namespace'):

        id = getattr(args, 'id', None)
        name = getattr(args, 'name', None)

        # Make sure we have input data to delete the channel by
        if not (id or name):
            self.logger.warn('Cannot continue. To delete a WebSocket channel, either --id or --name is required on input.')
            sys.exit(self.SYS_ERROR.INVALID_INPUT)

        # API service to invoke
        service = 'zato.channel.web-socket.delete'

        # API request to send
        request = {
            'id': id,
            'name': name,
            'should_raise_if_missing': False
        }

        self._invoke_service_and_log_response(service, request)

# ################################################################################################################################
# ################################################################################################################################

class CreateOutconn(SecurityAwareCommand):
    """ Creates a new outgoing WebSocket connection.
    """
    opts = [
        {'name':'--name', 'help':'Name of the connection to create', 'required':False,},
        {'name':'--address',   'help':'TCP address of a WebSocket server to connect to', 'required':False},
        {'name':'--sub-list',   'help':'A comma-separate list of topics the connection should subscribe to', 'required':False},
        {'name':'--on-connect-service',
            'help':'Service to invoke when the WebSocket connects to a remote server', 'required':False},
        {'name':'--on-message-service',
            'help':'Service to invoke when the WebSocket receives a message from the remote server', 'required':False},
        {'name':'--on-close-service',
            'help':'Service to invoke when the remote server closes its WebSocket connection', 'required':False},
        {'name':'--path', 'help':'Path to a Zato server', 'required':True},
    ]

    def execute(self, args:'Namespace'):

        # This can be specified by users
        name = getattr(args, 'name', None)
        address = getattr(args, 'address', None)
        on_connect_service_name = getattr(args, 'on_connect_service', None)
        on_message_service_name = getattr(args, 'on_message_service', None)
        on_close_service_name = getattr(args, 'on_close_service', None)
        subscription_list = getattr(args, 'sub_list', '')

        # This is fixed
        is_zato = getattr(args, 'is_zato', True)
        is_active = getattr(args, 'is_active', True)
        has_auto_reconnect = getattr(args, 'has_auto_reconnect', True)

        # Generate a name if one is not given
        name = name or 'auto.wsx.outconn.' + fs_safe_now()

        # If we have no address to connect to, use the on employed for testing
        if not address:
            address = 'ws://127.0.0.1:47043/zato.wsx.apitests'

        # Convert the subscription list to the format that the service expects
        if subscription_list:
            subscription_list = subscription_list.split(',')
            subscription_list = [elem.strip() for elem in subscription_list]
            subscription_list = '\n'.join(subscription_list)

        # API service to invoke
        service = 'zato.generic.connection.create'

        # API request to send
        request = {
            'name': name,
            'address': address,
            'is_zato': is_zato,
            'is_active': is_active,
            'has_auto_reconnect': has_auto_reconnect,
            'on_connect_service_name': on_connect_service_name,
            'on_message_service_name': on_message_service_name,
            'on_close_service_name': on_close_service_name,
            'subscription_list': subscription_list,
            'pool_size': 1,
            'is_channel': False,
            'is_outconn': True,
            'is_internal': False,
            'sec_use_rbac': False,
            'type_': Config.WSXOutconnType,
        }

        self._invoke_service_and_log_response(service, request)

# ################################################################################################################################
# ################################################################################################################################

if __name__ == '__main__':

    # stdlib
    from argparse import Namespace
    from os import environ

    now = fs_safe_now()

    username = 'cli.username.' + now
    password = 'cli.password.' + now

    args = Namespace()
    args.verbose      = True
    args.store_log    = False
    args.store_config = False
    args.service = Config.ServiceName
    args.basic_auth = f'{username}, {password}'
    args.path = environ['ZATO_SERVER_BASE_DIR']

    command = CreateChannel(args)
    command.run(args)

# ################################################################################################################################
# ################################################################################################################################
