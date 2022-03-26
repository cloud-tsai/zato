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
from zato.common.util.api import fs_safe_now
from zato.common.util.cli import BasicAuthManager

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from argparse import Namespace
    Namespace = Namespace

# ################################################################################################################################
# ################################################################################################################################

class CreateDefinition(ServerAwareCommand):
    """ Creates a new Basic Auth definition.
    """
    allow_empty_secrets = True # type: ignore

    opts = [
        {'name':'--name', 'help':'Name of the definition to create', 'required':False,},
        {'name':'--realm', 'help':'HTTP realm of the definition', 'required':False,},
        {'name':'--username', 'help':'Username for the definition to use', 'required':False},
        {'name':'--password', 'help':'Password for the definition to use', 'required':False},
        {'name':'--is-active', 'help':'Should the definition be active upon creation', 'required':False},
        {'name':'--path', 'help':'Path to a Zato server', 'required':True},
    ]

    def execute(self, args:'Namespace'):

        name = getattr(args, 'name', None)
        realm = getattr(args, 'realm', None)
        username = getattr(args, 'username', None)
        password = getattr(args, 'password', None)

        name = name or 'auto.basic-auth.name.' + fs_safe_now()
        realm = realm or 'auto.basic-auth.realm.' + fs_safe_now()
        username = username or 'auto.basic-auth.username.' + fs_safe_now()
        password = password or 'auto.basic-auth.password.' + uuid4().hex

        is_active = getattr(args, 'is_active', True)
        if is_active is None:
            is_active = True

        # Use a reusable object to create a new definition and set its password
        manager = BasicAuthManager(self, name, is_active, username, realm, password)
        _ = manager.create(needs_stdout=True)

# ################################################################################################################################
# ################################################################################################################################

class DeleteDefinition(ServerAwareCommand):
    """ Deletes a Basic Auth definition.
    """
    opts = [
        {'name':'--id', 'help':'ID of the definition to delete', 'required':False},
        {'name':'--name', 'help':'Name of the definition to delete', 'required':False},
        {'name':'--path', 'help':'Path to a Zato server', 'required':True},
    ]

    def execute(self, args:'Namespace'):

        id = getattr(args, 'id', None)
        name = getattr(args, 'name', None)

        # Make sure we have input data to delete the channel by
        if not (id or name):
            self.logger.warn('Cannot continue. To delete a Basic Auth definition, either --id or --name is required on input.')
            sys.exit(self.SYS_ERROR.INVALID_INPUT)

        # API service to invoke
        service = 'zato.security.basic-auth.delete'

        # API request to send
        request = {
            'id': id,
            'name': name,
        }

        self._invoke_service_and_log_response(service, request)

# ################################################################################################################################
# ################################################################################################################################

if __name__ == '__main__':

    # stdlib
    from argparse import Namespace
    from os import environ

    args = Namespace()
    args.verbose      = True
    args.store_log    = False
    args.store_config = False
    args.path = environ['ZATO_SERVER_BASE_DIR']

    command = CreateDefinition(args)
    command.run(args)

# ################################################################################################################################
# ################################################################################################################################
