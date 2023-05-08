# -*- coding: utf-8 -*-

"""
Copyright (C) 2023, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
import logging
import os
from json import dumps
from traceback import format_exc

# Bunch
from bunch import Bunch

# gevent
from gevent.pywsgi import WSGIServer

# Zato
from zato.common.api import ZATO_ODB_POOL_NAME
from zato.common.broker_message import code_to_name
from zato.common.crypto.api import CryptoManager
from zato.common.odb.api import ODBManager, PoolStore
from zato.common.util.api import as_bool, absjoin, get_config, new_cid
from zato.common.util.json_ import json_loads

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from zato.common.typing_ import any_, anydict, byteslist, callable_, type_

# ################################################################################################################################
# ################################################################################################################################

logger = logging.getLogger(__name__)

# ################################################################################################################################
# ################################################################################################################################

class StatusCode:
    OK                 = '200 OK'
    InternalError      = '500 Internal Server error'
    ServiceUnavailable = '503 Service Unavailable'

headers = [('Content-Type', 'application/json')]

# ################################################################################################################################
# ################################################################################################################################

class AuxServerConfig:
    """ Encapsulates configuration of various server-related layers.
    """
    odb: 'ODBManager'
    server_type: 'str'
    conf_file_name: 'str'
    crypto_manager: 'CryptoManager'
    crypto_manager_class: 'type_[CryptoManager]'

    def __init__(self) -> 'None':
        self.main = Bunch()
        self.stats_enabled = None
        self.component_dir = 'not-set-component_dir'

# ################################################################################################################################

    @classmethod
    def from_repo_location(
        class_,         # type: type_[AuxServerConfig]
        server_type,    # type: str
        repo_location,  # type: str
        conf_file_name, # type: str
        crypto_manager_class, # type: type_[CryptoManager]
    ) -> 'AuxServerConfig':

        # Zato
        from zato.common.util.cli import read_stdin_data

        # Response to produce
        config = class_()
        config.server_type = server_type

        # Path to the component can be built from its repository location
        component_dir = os.path.join(repo_location, '..', '..')
        component_dir = os.path.abspath(component_dir)
        config.component_dir = component_dir

        # Read config in and extend it with ODB-specific information
        config.main = get_config(repo_location, conf_file_name, require_exists=True)
        config.main.odb.fs_sql_config = get_config(repo_location, 'sql.conf', needs_user_config=False)
        config.main.crypto.use_tls = as_bool(config.main.crypto.use_tls)

        # Make all paths absolute
        if config.main.crypto.use_tls:
            config.main.crypto.ca_certs_location = absjoin(repo_location, config.main.crypto.ca_certs_location)
            config.main.crypto.priv_key_location = absjoin(repo_location, config.main.crypto.priv_key_location)
            config.main.crypto.cert_location = absjoin(repo_location, config.main.crypto.cert_location)

        # Set up the crypto manager need to access credentials
        config.crypto_manager = crypto_manager_class(repo_location, stdin_data=read_stdin_data())

        # ODB connection
        odb = ODBManager()
        sql_pool_store = PoolStore()

        if config.main.odb.engine != 'sqlite':

            config.main.odb.host = config.main.odb.host
            config.main.odb.username = config.main.odb.username
            config.main.odb.password = config.crypto_manager.decrypt(config.main.odb.password)
            config.main.odb.pool_size = config.main.odb.pool_size

        # Decrypt the password used to invoke servers
        server_password = config.main.server.server_password or ''
        if server_password and server_password.startswith('gA'):
            server_password = config.crypto_manager.decrypt(server_password)
            config.main.server.server_password = server_password

        sql_pool_store[ZATO_ODB_POOL_NAME] = config.main.odb

        odb.pool = sql_pool_store[ZATO_ODB_POOL_NAME].pool
        odb.init_session(ZATO_ODB_POOL_NAME, config.main.odb, odb.pool, False)
        odb.pool.ping(odb.fs_sql_config)

        config.odb = odb

        return config

# ################################################################################################################################
# ################################################################################################################################

class AuxServer:
    """ Main class spawning an auxilliary server and listening for API requests.
    """
    api_server: 'WSGIServer'
    cid_prefix: 'str'

    def __init__(self, config:'AuxServerConfig') -> 'None':
        self.config = config
        main = self.config.main

        if main.crypto.use_tls:
            tls_kwargs = {
                'keyfile': main.crypto.priv_key_location,
                'certfile': main.crypto.cert_location
            }
        else:
            tls_kwargs = {}

        # API server
        self.api_server = WSGIServer((main.bind.host, int(main.bind.port)), self, **tls_kwargs)

# ################################################################################################################################

    def before_config_hook(self) -> 'None':
        pass

# ################################################################################################################################

    def after_config_hook(self) -> 'None':
        pass

# ################################################################################################################################

    def serve_forever(self) -> 'None':
        self.api_server.serve_forever()

# ################################################################################################################################

    def get_action_func_impl(self, action_name:'str') -> 'callable_':
        raise NotImplementedError()

# ################################################################################################################################

    def handle_api_request(self, request:'bytes') -> 'any_':

        # Log what we are about to do
        logger.info('Handling API request -> `%s`', request)

        # Convert to a Python dict ..
        request = json_loads(request)

        # .. callback functions expect Bunch instances on input ..
        request = Bunch(request) # type: ignore

        # .. look up the action we need to invoke ..
        action = request.get('action') # type: ignore

        # .. make sure that the basic information was given on input ..
        if not action:
            raise Exception('No action key found in API request')

        action_name = code_to_name[action] # type: ignore

        # .. convert it to an actual method to invoke ..
        func = self.get_action_func_impl(action_name)

        # .. finally, invoke the function with the input data.
        response = func(request)
        return response

# ################################################################################################################################

    def __call__(self, env:'anydict', start_response:'callable_') -> 'byteslist':

        cid      = '<cid-unassigned>'
        response = {}

        status_text = '<status_text-unassigned>'
        status_code = StatusCode.ServiceUnavailable

        try:

            # Assign a new cid
            cid = '{}'.format(self.cid_prefix, new_cid())

            # Get the contents of our request ..
            request = env['wsgi.input'].read()

            # .. if there was any, invoke the business function ..
            if request:
                response = self.handle_api_request(request)

            # If we are here, it means that there was no exception
            status_text = 'ok'
            status_code = StatusCode.OK

        except Exception:

            # We are here because there was an exception
            logger.warning(format_exc())

            status_text = 'error'
            status_code = StatusCode.InternalError

        finally:

            # Build our response ..
            return_data = {
                'cid': cid,
                'status': status_text,
                'response': response
            }

            # .. make sure that we return bytes representing a JSON object ..
            return_data = dumps(return_data)
            return_data = return_data.encode('utf8')

            start_response(status_code, headers)
            return [return_data]

# ################################################################################################################################
# ################################################################################################################################
