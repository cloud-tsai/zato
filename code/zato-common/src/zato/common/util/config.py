# -*- coding: utf-8 -*-

"""
Copyright (C) 2023, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
import os
from urllib.parse import parse_qsl, urlparse, urlunparse

# Bunch
from bunch import Bunch

# Zato
from zato.common.api import Secret_Shadow
from zato.common.const import SECRETS

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from zato.common.typing_ import any_
    from zato.server.base.parallel import ParallelServer

# ################################################################################################################################
# ################################################################################################################################

def resolve_value(key, value, decrypt_func=None, _default=object(), _secrets=SECRETS):
    """ Resolves final value of a given variable by looking it up in environment if applicable.
    """
    # Skip non-resolvable items
    if not isinstance(value, str):
        return value

    if not value:
        return value

    value = value.decode('utf8') if isinstance(value, bytes) else value

    # It may be an environment variable ..
    if value.startswith('$'):

        # .. but not if it's $$ which is a signal to skip this value ..
        if value.startswith('$$'):
            return value

        # .. a genuine pointer to an environment variable.
        else:
            env_key = value[1:].strip().upper()
            value = os.environ.get(env_key, _default)

            # Use a placeholder if the actual environment key is missing
            if value is _default:
                value = 'Env_Key_Missing_{}'.format(env_key)

    # It may be an encrypted value
    elif key in _secrets.PARAMS and value.startswith(_secrets.PREFIX):
        value = decrypt_func(value)

    # Pre-processed, we can assign this pair to output
    return value

# ################################################################################################################################

def resolve_env_variables(data):
    """ Given a Bunch instance on input, iterates over all items and resolves all keys/values to ones extracted
    from environment variables.
    """
    out = Bunch()
    for key, value in data.items():
        out[key] = resolve_value(None, value)

    return out

# ################################################################################################################################

def replace_query_string_items(server:'ParallelServer', data:'any_') -> 'str':

    # Local variables
    query_string_new = []

    # Parse the data ..
    data = urlparse(data)

    # .. extract the query string ..
    query_string = data.query
    query_string = parse_qsl(query_string)

    # .. convert the data to a list to make it possible to unparse it later on ..
    data = list(data)

    # .. replace all the required elements ..
    for key, value in query_string:

        # .. so we know if we matched something in the inner loops ..
        should_continue = True

        # .. check exact keys ..
        for name in server.sio_config.secret_config.exact:
            if key == name:
                value = Secret_Shadow
                should_continue = False
                break

        # .. check prefixes ..
        if should_continue:
            for name in server.sio_config.secret_config.prefixes:
                if key.startswith(name):
                    value = Secret_Shadow
                    should_continue = should_continue
                    break

        # .. check suffixes ..
        if should_continue:
            for name in server.sio_config.secret_config.suffixes:
                if key.endswith(name):
                    value = Secret_Shadow
                    break

        # .. if we are here, either it means that the value was replaced ..
        # .. or we are going to use as it was because it needed no replacing ..
        query_string_new.append(f'{key}={value}')

    # .. replace the query string ..
    query_string_new = '&'.join(query_string_new)

    # .. now, set the query string back ..
    data[-2] = query_string_new

    # .. build a full address once more ..
    data = urlunparse(data)

    # .. and return it to our caller.
    return data

# ################################################################################################################################
# ################################################################################################################################
