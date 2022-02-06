# -*- coding: utf-8 -*-

"""
Copyright (C) 2022, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
from json import loads

# Zato
from zato.cli import ServerAwareCommand
from zato.common.api import GENERIC
from zato.common.typing_ import cast_

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from argparse import Namespace
    from zato.common.typing_ import anydict, anylist
    Namespace = Namespace

# ################################################################################################################################
# ################################################################################################################################

_opaque_attr = GENERIC.ATTR_NAME

class Config:
    DefaultTopicKeys = ('id', 'name', 'current_depth_gd', 'last_pub_time', 'last_pub_msg_id', 'last_endpoint_name',
        'last_pub_server_name', 'last_pub_server_pid', 'last_pub_has_gd')

# ################################################################################################################################
# ################################################################################################################################

class CreateTopic(ServerAwareCommand):
    """ Creates a new publish/subscribe topic.
    """
    opts = [
        {'name':'--name', 'help':'Name of the topic to create', 'required':False},
        {'name':'--gd',   'help':'Should the topic use Guaranteed Delivery', 'required':False},
        {'name':'--is-internal', 'help':'Is it a topic internal to the platform', 'required':False},
        {'name':'--is-api-sub-allowed', 'help':'Can applications subscribe to the topic via a public API', 'required':False},
        {'name':'--limit-retention', 'help':'Limit retention time in topic to that many seconds', 'required':False},
        {'name':'--limit-message-expiry', 'help':'Limit max. message expiration time to that many seconds', 'required':False},
        {'name':'--path', 'help':'Path to a Zato server', 'required':False},
    ]

# ################################################################################################################################

    def execute(self, args:'Namespace'):

        # Zato
        from zato.common.api import PUBSUB
        from zato.common.util.file_system import fs_safe_now

        _default = PUBSUB.DEFAULT

        # Topic name will be generated if it is now given on input
        topic_name  = getattr(args, 'name', None)

        has_gd             = getattr(args, 'gd',   False)
        is_internal        = getattr(args, 'is_internal', None)
        is_api_sub_allowed = getattr(args, 'is_api_sub_allowed', True)

        limit_expiry    = getattr(args, 'limit_expiry', 0)    or _default.LimitMessageExpiry
        limit_retention = getattr(args, 'limit_retention', 0) or _default.LimitTopicRetention

        limit_expiry    = int(limit_expiry)
        limit_retention = int(limit_retention)

        if not topic_name:
            topic_name = '/auto/topic.{}'.format(fs_safe_now())

        service = 'zato.pubsub.topic.create'
        request = {
            'name': topic_name,
            'is_active': True,
            'is_internal': is_internal,
            'has_gd': has_gd,
            'is_api_sub_allowed': is_api_sub_allowed,
            'max_depth_gd': _default.TOPIC_MAX_DEPTH_GD,
            'max_depth_non_gd': _default.TOPIC_MAX_DEPTH_NON_GD,
            'depth_check_freq': _default.DEPTH_CHECK_FREQ,
            'pub_buffer_size_gd': _default.PUB_BUFFER_SIZE_GD,
            'task_sync_interval': _default.TASK_SYNC_INTERVAL,
            'task_delivery_interval': _default.TASK_DELIVERY_INTERVAL,
            'limit_expiry': limit_expiry,
            'limit_retention': limit_retention,
        }

        self._invoke_service_and_log_response(service, request)

# ################################################################################################################################
# ################################################################################################################################

class GetTopics(ServerAwareCommand):
    """ Returns one or more topic by their name. Accepts partial names, e.g. "demo" will match "/my/demo/topic".
    """
    opts = [
        {'name':'--query', 'help':'Query to look up topics by', 'required':False},
        {'name':'--keys',  'help':'What JSON keys to return on put. Use "all" to return them all', 'required':False},
        {'name':'--path',  'help':'Path to a Zato server', 'required':False},
    ]

# ################################################################################################################################

    def execute(self, args:'Namespace'):

        # Make sure that keys are always a set object to look up information in
        args_keys = getattr(args, 'keys', '')
        if args_keys:
            if isinstance(args_keys, str):
                args_keys = args_keys.split(',')
                args_keys = [elem.strip() for elem in args_keys]

            has_all = 'all' in args_keys
            needs_default_keys = has_all or (not args_keys)

        else:
            has_all = False
            needs_default_keys = True
            args_keys = Config.DefaultTopicKeys

        args_keys = set(args_keys)

        def hook_func(data:'anydict') -> 'anylist':

            # Response to produce ..
            out = []

            # .. extract the top-level element ..
            data = data['zato_pubsub_topic_get_list_response']

            # .. go through each response element found ..
            for elem in data: # type: dict
                elem = cast_('anydict', elem)

                # .. turn opaque data into path of the response ..
                opaque = elem.pop(_opaque_attr, '')
                if opaque:
                    opaque = loads(opaque)
                    if opaque:
                        elem.update(opaque)

                # Make sure we return only the requested keys. Note that we build a new dictionary
                # because we want to preserve the order of DefaultConfigKeys. Also note that if all keys
                # are requested, for consistency, we still initially populate the dictionary
                # with keys from DefaultTopicKeys and only then do we proceed to the remaining keys.
                out_elem = {}

                # We are possibly return the default keys
                if needs_default_keys:

                    # First, populate the default keys ..
                    for name in Config.DefaultTopicKeys:
                        value = elem.get(name)
                        out_elem[name] = value

                # .. otherwise, we return only the specifically requested keys
                for name, value in sorted(elem.items()):
                    if has_all or (name in args_keys):
                        if name not in out_elem:
                            out_elem[name] = value

                # .. we are finished with pre-processing of this element ..
                out.append(out_elem)

            # .. and return the output to our caller.
            return out

        # Our service to invoke
        service = 'zato.pubsub.topic.get-list'

        # Get a list of topics matching the input query, if any
        request = {
            'paginate': True,
            'needs_details': True,
            'query': getattr(args, 'query', ''),
        }

        # Invoke and log, pre-processing the data first with a hook function
        self._invoke_service_and_log_response(service, request, hook_func=hook_func)

# ################################################################################################################################
# ################################################################################################################################

class GetTopic(GetTopics):
    """ A convenience alias for get-topics. It does exactly the same.
    """

# ################################################################################################################################
# ################################################################################################################################

if __name__ == '__main__':

    # stdlib
    from argparse import Namespace
    from os import environ

    args = Namespace()
    args.keys         = 'all'
    args.verbose      = True
    args.store_log    = False
    args.store_config = False
    args.path = environ['ZATO_SERVER_BASE_DIR']

    command = GetTopics(args)
    command.run(args)

    """
    args = Bunch()
    args.verbose      = True
    args.store_log    = False
    args.store_config = False
    args.path = environ['ZATO_SERVER_BASE_DIR']

    command = CreateTopic(args)
    command.run(args)
    """

# ################################################################################################################################
# ################################################################################################################################
