# -*- coding: utf-8 -*-

"""
Copyright (C) 2022, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# pylint: disable=unused-import, redefined-builtin, unused-variable

# stdlib
import logging
from contextlib import closing
from datetime import datetime, timedelta
from inspect import isclass
from io import StringIO
from operator import attrgetter
from traceback import format_exc

# gevent
from gevent import sleep, spawn
from gevent.lock import RLock

# Texttable
from texttable import Texttable

# Zato
from zato.common.api import PUBSUB
from zato.common.broker_message import PUBSUB as BROKER_MSG_PUBSUB
from zato.common.odb.model import WebSocketClientPubSubKeys
from zato.common.odb.query.pubsub.delivery import confirm_pubsub_msg_delivered as _confirm_pubsub_msg_delivered, \
    get_delivery_server_for_sub_key, get_sql_messages_by_msg_id_list as _get_sql_messages_by_msg_id_list, \
    get_sql_messages_by_sub_key as _get_sql_messages_by_sub_key, get_sql_msg_ids_by_sub_key as _get_sql_msg_ids_by_sub_key
from zato.common.odb.query.pubsub.queue import set_to_delete
from zato.common.pubsub import skip_to_external
from zato.common.typing_ import cast_, dict_, optional
from zato.common.util.api import new_cid, spawn_greenlet
from zato.common.util.file_system import fs_safe_name
from zato.common.util.hook import HookTool
from zato.common.util.time_ import datetime_from_ms, utcnow_as_ms
from zato.common.util.wsx import find_wsx_environ
from zato.server.pubsub.model import HookCtx, strsubdict, sublist, Subscription, SubKeyServer, Topic
from zato.server.pubsub.publisher import Publisher
from zato.server.pubsub.sync import InRAMSync

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from sqlalchemy.orm.session import Session as SASession
    from zato.common.model.wsx import WSXConnectorConfig
    from zato.common.typing_ import any_, anydict, anylist, anytuple, callable_, callnone, commondict, dictlist, intdict, \
        intanydict, intlist, intnone, intset, list_, stranydict, strintdict, strstrdict, strlist, strlistdict, \
        strlistempty, strtuple, type_
    from zato.distlock import Lock
    from zato.server.connection.web_socket import WebSocket
    from zato.server.base.parallel import ParallelServer
    from zato.server.pubsub.core.endpoint import EndpointAPI
    from zato.server.pubsub.model import Endpoint, subnone, topiclist
    from zato.server.pubsub.delivery.task import msgiter
    from zato.server.pubsub.delivery.tool import PubSubTool
    from zato.server.service import Service

# ################################################################################################################################
# ################################################################################################################################

logger = logging.getLogger('zato_pubsub.ps')
logger_zato = logging.getLogger('zato')
logger_overflow = logging.getLogger('zato_pubsub_overflow')

# ################################################################################################################################
# ################################################################################################################################

hook_type_to_method = {
    PUBSUB.HOOK_TYPE.BEFORE_PUBLISH: 'before_publish',
    PUBSUB.HOOK_TYPE.BEFORE_DELIVERY: 'before_delivery',
    PUBSUB.HOOK_TYPE.ON_OUTGOING_SOAP_INVOKE: 'on_outgoing_soap_invoke',
    PUBSUB.HOOK_TYPE.ON_SUBSCRIBED: 'on_subscribed',
    PUBSUB.HOOK_TYPE.ON_UNSUBSCRIBED: 'on_unsubscribed',
}

# ################################################################################################################################
# ################################################################################################################################

_service_read_messages_gd = 'zato.pubsub.endpoint.get-endpoint-queue-messages-gd'
_service_read_messages_non_gd = 'zato.pubsub.endpoint.get-endpoint-queue-messages-non-gd'

_service_read_message_gd = 'zato.pubsub.message.get-from-queue-gd'
_service_read_message_non_gd = 'zato.pubsub.message.get-from-queue-non-gd'

_service_delete_message_gd = 'zato.pubsub.message.queue-delete-gd'
_service_delete_message_non_gd = 'zato.pubsub.message.queue-delete-non-gd'

_end_srv_id = PUBSUB.ENDPOINT_TYPE.SERVICE.id

_ps_default = PUBSUB.DEFAULT

# ################################################################################################################################
# ################################################################################################################################

default_sk_server_table_columns = 6, 15, 8, 6, 17, 80
default_sub_pattern_matched = '(No sub pattern)'

# ################################################################################################################################
# ################################################################################################################################

class MsgConst:
    wsx_sub_resumed = 'WSX subscription resumed, sk:`%s`, peer:`%s`'

# ################################################################################################################################
# ################################################################################################################################

class PubSub:

    endpoint_api: 'EndpointAPI'

    def __init__(
        self,
        cluster_id,         # type: int
        server,             # type: ParallelServer
        broker_client=None, # type: any_
        *,
        sync_max_iters=None,      # type: intnone
        spawn_trigger_notify=True # type: bool
    ) -> 'None':

        self.cluster_id = cluster_id
        self.server = server
        self.broker_client = broker_client
        self.sync_max_iters = sync_max_iters
        self.lock = RLock()
        self.keep_running = True
        self.sk_server_table_columns = self.server.fs_server_config.pubsub.get('sk_server_table_columns') or \
            default_sk_server_table_columns # type: anytuple

        # This is a pub/sub tool for delivery of Zato services within this server
        self.service_pubsub_tool = None # type: optional[PubSubTool]

        self.log_if_deliv_server_not_found = \
            self.server.fs_server_config.pubsub.log_if_deliv_server_not_found # type: bool

        self.log_if_wsx_deliv_server_not_found = \
            self.server.fs_server_config.pubsub.log_if_wsx_deliv_server_not_found # type: bool

        # Topic name -> List of Subscription objects
        self.subscriptions_by_topic = {} # type: dict_[str, sublist]

        # Sub key -> Subscription object
        self._subscriptions_by_sub_key = {} # type: strsubdict

        # Sub key -> SubKeyServer server/PID handling it
        self.sub_key_servers = {} # type: dict_[str, SubKeyServer]

        # Topic ID -> Topic object
        self.topics = cast_('inttopicdict', {})

        # Topic name -> Topic ID
        self.topic_name_to_id = {} # type: strintdict

        # Sub key -> PubSubTool object
        self.pubsub_tool_by_sub_key = {} # type: dict_[str, PubSubTool]

        # A list of PubSubTool objects, each containing delivery tasks
        self.pubsub_tools = [] # type: list_[PubSubTool]

        # A backlog of messages that have at least one subscription, i.e. this is what delivery servers use.
        self.sync_backlog = InRAMSync(self)

        # How many messages have been published through this server, regardless of which topic they were for
        self.msg_pub_counter = 0

        # How many messages a given endpoint published, topic_id -> its message counter.
        self.endpoint_msg_counter = {} # type: intdict

        # How often to update metadata about topics and endpoints, if at all
        self.has_meta_topic = self.server.fs_server_config.pubsub_meta_topic.enabled # type: bool
        self.topic_meta_store_frequency = self.server.fs_server_config.pubsub_meta_topic.store_frequency # type: int

        self.has_meta_endpoint = self.server.fs_server_config.pubsub_meta_endpoint_pub.enabled # type: bool
        self.endpoint_meta_store_frequency = self.server.fs_server_config.pubsub_meta_endpoint_pub.store_frequency # type: int
        self.endpoint_meta_data_len = self.server.fs_server_config.pubsub_meta_endpoint_pub.data_len # type:int
        self.endpoint_meta_max_history = self.server.fs_server_config.pubsub_meta_endpoint_pub.max_history # type:int

        # How many bytes to use for look up purposes when conducting message searches
        self.data_prefix_len = self.server.fs_server_config.pubsub.data_prefix_len # type: int
        self.data_prefix_short_len = self.server.fs_server_config.pubsub.data_prefix_short_len # type: int

        # Manages access to service hooks
        self.hook_tool = HookTool(self.server, HookCtx, hook_type_to_method, self.invoke_service)

        # Creates SQL sessions
        self.new_session_func = self.server.odb.session

        # A low level implementation that publishes messages to SQL
        self.impl_publisher = Publisher(
            pubsub = self,
            server = self.server,
            marshal_api = self.server.marshal_api,
            service_invoke_func = self.invoke_service,
            new_session_func = self.new_session_func
        )

        if spawn_trigger_notify:
            _ = spawn_greenlet(self.trigger_notify_pubsub_tasks)

# ################################################################################################################################

    @property
    def subscriptions_by_sub_key(self) -> 'strsubdict':
        return self._subscriptions_by_sub_key

# ################################################################################################################################

    def incr_pubsub_msg_counter(self, endpoint_id:'int') -> 'None':
        with self.lock:

            # Update the overall counter
            self.msg_pub_counter += 1

            # Update the per-endpoint counter too
            if endpoint_id in self.endpoint_msg_counter:
                self.endpoint_msg_counter[endpoint_id] += 1
            else:
                self.endpoint_msg_counter[endpoint_id] = 0

# ################################################################################################################################

    def needs_endpoint_meta_update(self, endpoint_id:'int') -> 'bool':
        with self.lock:
            return self.endpoint_msg_counter[endpoint_id] % self.endpoint_meta_store_frequency == 0

# ################################################################################################################################

    def get_subscriptions_by_topic(self, topic_name:'str', require_backlog_messages:'bool'=False) -> 'sublist':
        with self.lock:
            subs = self.subscriptions_by_topic.get(topic_name, [])
            subs = subs[:]
            if require_backlog_messages:
                out = [] # type: anylist
                for item in subs:
                    if self.sync_backlog.has_messages_by_sub_key(item.sub_key):
                        out.append(item)
                return out
            else:
                return subs

# ################################################################################################################################

    def get_all_subscriptions(self) -> 'strsubdict':
        """ Low-level method to return all the subscriptions by sub_key,
        must be called with self.lock held.
        """
        return self.subscriptions_by_sub_key

# ################################################################################################################################

    def _get_subscription_by_sub_key(self, sub_key:'str') -> 'Subscription':
        """ Low-level implementation of self.get_subscription_by_sub_key, must be called with self.lock held.
        """
        return self.subscriptions_by_sub_key[sub_key]

# ################################################################################################################################

    def get_subscription_by_sub_key(self, sub_key:'str') -> 'subnone':
        with self.lock:
            try:
                return self._get_subscription_by_sub_key(sub_key)
            except KeyError:
                return None

# ################################################################################################################################

    def get_subscription_by_endpoint_id(
        self,
        endpoint_id,      # type: int
        topic_name,       # type: str
        needs_error=True, # type: bool
    ) -> 'subnone':

        with self.lock:
            for sub in self.get_all_subscriptions().values():
                if sub.endpoint_id == endpoint_id:
                    return sub
            else:
                msg = f'No sub to topic `{topic_name}` for endpoint_id `{endpoint_id}`'
                if needs_error:
                    raise KeyError(msg)
                else:
                    logger.info(msg)

# ################################################################################################################################

    def get_subscription_by_id(self, sub_id:'int') -> 'subnone':
        with self.lock:
            for sub in self.subscriptions_by_sub_key.values():
                if sub.id == sub_id:
                    return sub

# ################################################################################################################################

    def get_subscription_by_ext_client_id(self, ext_client_id:'str') -> 'subnone':
        with self.lock:
            for sub in self.subscriptions_by_sub_key.values():
                if sub.ext_client_id == ext_client_id:
                    return sub

# ################################################################################################################################

    def _write_log_sub_data(self, sub:'Subscription', out:'StringIO') -> 'None':
        items = sorted(sub.to_dict().items())

        _ = out.write('\n')
        for key, value in items:
            _ = out.write(' - {} {}'.format(key, value))
            if key == 'creation_time':
                _ = out.write('\n - creation_time_utc {}'.format(datetime_from_ms(value)))
            _ = out.write('\n')

# ################################################################################################################################

    def _log_subscriptions_dict(self, attr_name:'str', prefix:'str', title:'str') -> 'None':
        out = StringIO()
        _ = out.write('\n')

        attr = getattr(self, attr_name) # type: anydict

        for sub_key, sub_data in sorted(attr.items()):
            sub_key = cast_('str', sub_key)
            _ = out.write('* {}\n'.format(sub_key))

            if isinstance(sub_data, Subscription):
                self._write_log_sub_data(sub_data, out)
            else:
                sorted_sub_data = sorted(sub_data)
                for item in sorted_sub_data:
                    if isinstance(item, Subscription):
                        self._write_log_sub_data(item, out)
                    else:
                        item = cast_('any_', item)
                        _ = out.write(' - {}'.format(item))
                        _ = out.write('\n')

            _ = out.write('\n')

        logger_zato.info('\n === %s (%s) ===\n %s', prefix, title, out.getvalue())
        out.close()

# ################################################################################################################################

    def log_subscriptions_by_sub_key(self, title:'str', prefix:'str'='PubSub.subscriptions_by_sub_key') -> 'None':
        with self.lock:
            self._log_subscriptions_dict('subscriptions_by_sub_key', prefix, title)

# ################################################################################################################################

    def log_subscriptions_by_topic_name(self, title:'str', prefix:'str'='PubSub.subscriptions_by_topic') -> 'None':
        with self.lock:
            self._log_subscriptions_dict('subscriptions_by_topic', prefix, title)

# ################################################################################################################################

    def has_sub_key(self, sub_key:'str') -> 'bool':
        with self.lock:
            return sub_key in self.subscriptions_by_sub_key

# ################################################################################################################################

    def has_messages_in_backlog(self, sub_key:'str') -> 'bool':
        with self.lock:
            return self.sync_backlog.has_messages_by_sub_key(sub_key)

# ################################################################################################################################

    def _len_subscribers(self, topic_name:'str') -> 'int':
        """ Low-level implementation of self.len_subscribers, must be called with self.lock held.
        """
        return len(self.subscriptions_by_topic[topic_name])

# ################################################################################################################################

    def len_subscribers(self, topic_name:'str') -> 'int':
        """ Returns the amount of subscribers for a given topic.
        """
        with self.lock:
            return self._len_subscribers(topic_name)

# ################################################################################################################################

    def has_subscribers(self, topic_name:'str') -> 'bool':
        """ Returns True if input topic has at least one subscriber.
        """
        with self.lock:
            return self._len_subscribers(topic_name) > 0

# ################################################################################################################################

    def has_topic_by_name(self, topic_name:'str') -> 'bool':
        with self.lock:
            try:
                _ = self._get_topic_by_name(topic_name)
            except KeyError:
                return False
            else:
                return True

# ################################################################################################################################

    def has_topic_by_id(self, topic_id:'int') -> 'bool':
        with self.lock:
            try:
                self.topics[topic_id]
            except KeyError:
                return False
            else:
                return True

# ################################################################################################################################

    def get_endpoint_by_id(self, endpoint_id:'int') -> 'Endpoint':
        with self.lock:
            return self.endpoint_api.get_by_id(endpoint_id)

# ################################################################################################################################

    def get_endpoint_by_name(self, endpoint_name:'str') -> 'Endpoint':
        with self.lock:
            return self.endpoint_api.get_by_name(endpoint_name)

# ################################################################################################################################

    def get_endpoint_by_ws_channel_id(self, ws_channel_id:'int') -> 'Endpoint':
        with self.lock:
            return self.endpoint_api.get_by_ws_channel_id(ws_channel_id)

# ################################################################################################################################

    def get_endpoint_id_by_sec_id(self, sec_id:'int') -> 'int':
        with self.lock:
            return self.endpoint_api.get_id_by_sec_id(sec_id)

# ################################################################################################################################

    def get_endpoint_id_by_ws_channel_id(self, ws_channel_id:'int') -> 'intnone':
        with self.lock:
            return self.endpoint_api.get_id_by_ws_channel_id(ws_channel_id)

# ################################################################################################################################

    def get_endpoint_id_by_service_id(self, service_id:'int') -> 'int':
        with self.lock:
            return self.endpoint_api.get_id_by_service_id(service_id)

# ################################################################################################################################

    def create_endpoint(self, config:'anydict') -> 'None':
        with self.lock:
            self.endpoint_api.create(config)

# ################################################################################################################################

    def delete_endpoint(self, endpoint_id:'int') -> 'None':
        with self.lock:
            self.endpoint_api.delete(endpoint_id)

# ################################################################################################################################

    def edit_endpoint(self, config:'stranydict') -> 'None':
        with self.lock:
            self.endpoint_api.delete(config['id'])
            self.endpoint_api.create(config)

# ################################################################################################################################

    def _get_topic_id_by_name(self, topic_name:'str') -> 'int':
        return self.topic_name_to_id[topic_name]

# ################################################################################################################################

    def get_topic_id_by_name(self, topic_name:'str') -> 'int':
        with self.lock:
            return self._get_topic_id_by_name(topic_name)

# ################################################################################################################################

    def get_non_gd_topic_depth(self, topic_name:'str') -> 'int':
        """ Returns of non-GD messages for a given topic by its name.
        """
        with self.lock:
            return self.sync_backlog.get_topic_depth(self._get_topic_id_by_name(topic_name))

# ################################################################################################################################

    def _get_topic_by_name(self, topic_name:'str') -> 'Topic':
        """ Low-level implementation of self.get_topic_by_name.
        """
        return self.topics[self._get_topic_id_by_name(topic_name)]

# ################################################################################################################################

    def get_topic_by_name(self, topic_name:'str') -> 'Topic':
        with self.lock:
            return self._get_topic_by_name(topic_name)

# ################################################################################################################################

    def _get_topic_by_id(self, topic_id:'int') -> 'Topic':
        """ Low-level implementation of self.get_topic_by_id, must be called with self.lock held.
        """
        return self.topics[topic_id]

# ################################################################################################################################

    def get_topic_by_id(self, topic_id:'int') -> 'Topic':
        with self.lock:
            return self._get_topic_by_id(topic_id)

# ################################################################################################################################

    def get_topic_name_by_sub_key(self, sub_key:'str') -> 'str':
        with self.lock:
            return self._get_subscription_by_sub_key(sub_key).topic_name

# ################################################################################################################################

    def get_sub_key_to_topic_name_dict(self, sub_key_list:'strlist') -> 'strstrdict':
        out = {} # type: strstrdict
        with self.lock:
            for sub_key in sub_key_list:
                out[sub_key] = self._get_subscription_by_sub_key(sub_key).topic_name

        return out

# ################################################################################################################################

    def _get_topic_by_sub_key(self, sub_key:'str') -> 'Topic':
        return self._get_topic_by_name(self._get_subscription_by_sub_key(sub_key).topic_name)

# ################################################################################################################################

    def get_topic_by_sub_key(self, sub_key:'str') -> 'Topic':
        with self.lock:
            return self._get_topic_by_sub_key(sub_key)

# ################################################################################################################################

    def get_topic_list_by_sub_key_list(self, sk_list:'strlist') -> 'strtopicdict': # type: ignore [valid-type]
        out = cast_('strtopicdict', {})
        with self.lock:
            for sub_key in sk_list:
                out[sub_key] = self._get_topic_by_sub_key(sub_key)
        return out

# ################################################################################################################################

    def edit_subscription(self, config:'stranydict') -> 'None':
        with self.lock:
            sub = self._get_subscription_by_sub_key(config['sub_key'])
            for key, value in config.items():
                sub.config[key] = value

# ################################################################################################################################

    def _add_subscription(self, config:'stranydict') -> 'None':
        """ Low-level implementation of self.add_subscription.
        """
        sub = Subscription(config)

        existing_by_topic = self.subscriptions_by_topic.setdefault(config['topic_name'], [])
        existing_by_topic.append(sub)

        logger_zato.info('Added sub `%s` -> `%s`', config['sub_key'], config['topic_name'])

        self.subscriptions_by_sub_key[config['sub_key']] = sub

# ################################################################################################################################

    def add_subscription(self, config:'stranydict') -> 'None':
        """ Creates a Subscription object and an associated mapping of the subscription to input topic.
        """
        with self.lock:

            # Creates a subscription ..
            self._add_subscription(config)

            # .. triggers a relevant hook, if any is configured.
            hook = self.get_on_subscribed_hook(config['sub_key'])
            if hook:
                _ = self.invoke_on_subscribed_hook(hook, config['topic_id'], config['sub_key'])

# ################################################################################################################################

    def _delete_subscription_by_sub_key(
        self,
        sub_key,          # type: str
        ignore_missing,   # type: bool
        _invalid=object() # type: any_
    ) -> 'subnone':
        """ Deletes a subscription from the list of subscription. By default, it is not an error to call
        the method with an invalid sub_key. Must be invoked with self.lock held.
        """
        sub = self.subscriptions_by_sub_key.get(sub_key, _invalid) # type: Subscription

        #
        # There is no such subscription and we may either log it or raise an exception ..
        #
        if sub is _invalid:

            # If this is on, we only log information about the event ..
            if ignore_missing:
                logger.info('Could not find sub_key to delete `%s`', sub_key)

            # .. otherwise, we raise an entire exception.
            else:
                raise KeyError('No such sub_key `%s`', sub_key)

        #
        # If we are here, it means that the subscription is valid
        #
        else:

            # Now, delete the subscription
            _ = self.subscriptions_by_sub_key.pop(sub_key, _invalid)

            # Delete the subscription's sk_server first because it depends on the subscription
            # for sk_server table formatting.
            self.delete_sub_key_server(sub_key, sub_pattern_matched=sub.sub_pattern_matched)

            # Log what we have done ..
            logger.info('Deleted subscription object `%s` (%s)', sub.sub_key, sub.topic_name)

            return sub # Either valid or invalid but ignore_missing is True

# ################################################################################################################################

    def create_subscription_object(self, config:'stranydict') -> 'None':
        """ Low-level implementation of self.subscribe. Must be called with self.lock held.
        """
        with self.lock:

            # It's possible that we already have this subscription - this may happen if we are the server that originally
            # handled the request to create the subscription and we are now called again through
            # on_broker_msg_PUBSUB_SUBSCRIPTION_CREATE. In such a case, we can just ignore it.
            if not self.has_sub_key(config['sub_key']):
                self._add_subscription(config)

            # We don't start dedicated tasks for WebSockets - they are all dynamic without a fixed server.
            # But for other endpoint types, we create and start a delivery task here.
            if config['endpoint_type'] != PUBSUB.ENDPOINT_TYPE.WEB_SOCKETS.id:

                # We have a matching server..
                if config['cluster_id'] == self.cluster_id and config['server_id'] == self.server.id:

                    # .. but make sure only the first worker of this server will start delivery tasks, not all of them.
                    if self.server.is_first_worker:

                        # Store in shared RAM information that our process handles this key
                        if self.server.has_posix_ipc:
                            self.server.server_startup_ipc.set_pubsub_pid(self.server.pid)

                        config['server_pid'] = self.server.pid
                        config['server_name'] = self.server.name

                        # Starts the delivery task and notifies other servers that we are the one
                        # to handle deliveries for this particular sub_key.
                        _ = self.invoke_service('zato.pubsub.delivery.create-delivery-task', config)

                    # We are not the first worker of this server and the first one must have already stored
                    # in RAM the mapping of sub_key -> server_pid, so we can safely read it here to add
                    # a subscription server.
                    else:
                        if self.server.has_posix_ipc:
                            config['server_pid'] = self.server.server_startup_ipc.get_pubsub_pid()
                            config['server_name'] = self.server.name
                            self.set_sub_key_server(config)

# ################################################################################################################################

    def _set_topic_config_hook_data(self, config:'stranydict') -> 'None':

        hook_service_id = config.get('hook_service_id')

        if hook_service_id:

            if not config['hook_service_name']:
                config['hook_service_name'] = self.server.service_store.get_service_name_by_id(hook_service_id)

            # Invoked when a new subscription to topic is created
            config['on_subscribed_service_invoker'] = self.hook_tool.get_hook_service_invoker(
                config['hook_service_name'], PUBSUB.HOOK_TYPE.ON_SUBSCRIBED)

            # Invoked when an existing subscription to topic is deleted
            config['on_unsubscribed_service_invoker'] = self.hook_tool.get_hook_service_invoker(
                config['hook_service_name'], PUBSUB.HOOK_TYPE.ON_UNSUBSCRIBED)

            # Invoked before messages are published
            config['before_publish_hook_service_invoker'] = self.hook_tool.get_hook_service_invoker(
                config['hook_service_name'], PUBSUB.HOOK_TYPE.BEFORE_PUBLISH)

            # Invoked before messages are delivered
            config['before_delivery_hook_service_invoker'] = self.hook_tool.get_hook_service_invoker(
                config['hook_service_name'], PUBSUB.HOOK_TYPE.BEFORE_DELIVERY)

            # Invoked for outgoing SOAP connections
            config['on_outgoing_soap_invoke_invoker'] = self.hook_tool.get_hook_service_invoker(
                config['hook_service_name'], PUBSUB.HOOK_TYPE.ON_OUTGOING_SOAP_INVOKE)
        else:
            config['hook_service_invoker'] = None

# ################################################################################################################################

    def _create_topic_object(self, config:'stranydict') -> 'None':
        self._set_topic_config_hook_data(config)
        config['meta_store_frequency'] = self.topic_meta_store_frequency

        topic = Topic(config, self.server.name, self.server.pid)
        self.topics[config['id']] = topic
        self.topic_name_to_id[config['name']] = config['id']

        logger.info('Created topic object `%s` (id:%s) on server `%s` (pid:%s)', topic.name, topic.id,
            topic.server_name, topic.server_pid)

# ################################################################################################################################

    def create_topic_object(self, config:'anydict') -> 'None':
        with self.lock:
            self._create_topic_object(config)

# ################################################################################################################################

    def create_topic_for_service(self, service_name:'str', topic_name:'str') -> 'None':
        self.create_topic(topic_name, is_internal=True)
        logger.info('Created topic `%s` for service `%s`', topic_name, service_name)

# ################################################################################################################################

    def wait_for_topic(self, topic_name:'str', timeout:'int'=10, _utcnow:'callable_'=datetime.utcnow) -> 'bool':
        now = _utcnow()
        until = now + timedelta(seconds=timeout)

        # Wait until topic has been created in self.topic or raise an exception on timeout
        while now < until:

            # We have it, good
            with self.lock:
                try:
                    _ = self._get_topic_by_name(topic_name)
                except KeyError:
                    pass # No such topic
                else:
                    return True

            # No such topic, let us sleep for a moment
            sleep(1)
            now = _utcnow()

        # We get here on timeout
        raise ValueError('No such topic `{}` after {}s'.format(topic_name, timeout))

# ################################################################################################################################

    def _delete_topic(self, topic_id:'int', topic_name:'str') -> 'anylist':
        del self.topic_name_to_id[topic_name]
        subscriptions_by_topic = self.subscriptions_by_topic.pop(topic_name, [])
        del self.topics[topic_id]

        logger.info('Deleted topic object `%s` (%s), subs:`%s`',
            topic_name, topic_id, [elem.sub_key for elem in subscriptions_by_topic])

        return subscriptions_by_topic

# ################################################################################################################################

    def delete_topic(self, topic_id:'int') -> 'None':
        with self.lock:
            topic_name = self.topics[topic_id].name
            subscriptions_by_topic = self._delete_topic(topic_id, topic_name) # type: sublist

            for sub in subscriptions_by_topic:
                _ = self._delete_subscription_by_sub_key(sub.sub_key, ignore_missing=True)

# ################################################################################################################################

    def edit_topic(self, del_name:'str', config:'anydict') -> 'None':
        with self.lock:
            subscriptions_by_topic = self.subscriptions_by_topic.pop(del_name, [])
            _ = self._delete_topic(config['id'], del_name)
            self._create_topic_object(config)
            self.subscriptions_by_topic[config['name']] = subscriptions_by_topic

# ################################################################################################################################

    def set_config_for_service_subscription(
        self,
        sub_key, # type: str
        _endpoint_type=_end_srv_id # type: str
    ) -> 'None':

        if self.service_pubsub_tool:
            self.service_pubsub_tool.add_sub_key(sub_key)
        else:
            msg = 'No self.service_pubsub_tool to add sub key to (%s)'
            logger.warning(msg, sub_key)
            logger_zato.warning(msg, sub_key)

        self.set_sub_key_server({
            'sub_key': sub_key,
            'cluster_id': self.server.cluster_id,
            'server_name': self.server.name,
            'server_pid': self.server.pid,
            'endpoint_type': _endpoint_type,
        })

# ################################################################################################################################

    def is_allowed_pub_topic(self, name:'str', security_id:'int'=0, ws_channel_id:'int'=0) -> 'str | bool':
        return self.endpoint_api.is_allowed_pub_topic(
            name=name,
            security_id=security_id,
            ws_channel_id=ws_channel_id
        )

# ################################################################################################################################

    def is_allowed_pub_topic_by_endpoint_id(self, name:'str', endpoint_id:'int') -> 'str | bool':
        return self.endpoint_api.is_allowed_pub_topic_by_endpoint_id(
            name=name,
            endpoint_id=endpoint_id
        )

# ################################################################################################################################

    def is_allowed_sub_topic(self, name:'str', security_id:'int'=0, ws_channel_id:'int'=0) -> 'str | bool':
        return self.endpoint_api.is_allowed_sub_topic(
            name=name,
            security_id=security_id,
            ws_channel_id=ws_channel_id
        )

# ################################################################################################################################

    def is_allowed_sub_topic_by_endpoint_id(self, name:'str', endpoint_id:'int') -> 'str | bool':
        return self.endpoint_api.is_allowed_sub_topic_by_endpoint_id(
            name=name,
            endpoint_id=endpoint_id
        )

# ################################################################################################################################

    def get_topics(self) -> 'inttopicdict': # type: ignore [valid-type]
        """ Returns all topics in existence.
        """
        with self.lock:
            return self.topics

# ################################################################################################################################

    def get_sub_topics_for_endpoint(self, endpoint_id:'int') -> 'topiclist':
        """ Returns all topics to which endpoint_id can subscribe.
        """
        out = [] # type: topiclist
        with self.lock:
            for topic in self.topics.values():
                if self.is_allowed_sub_topic_by_endpoint_id(topic.name, endpoint_id):
                    out.append(topic)

        return out

# ################################################################################################################################

    def _is_subscribed_to(self, endpoint_id:'int', topic_name:'str') -> 'bool':
        """ Low-level implementation of self.is_subscribed_to.
        """
        for sub in self.subscriptions_by_topic.get(topic_name, []):
            if sub.endpoint_id == endpoint_id:
                return True
        else:
            return False

# ################################################################################################################################

    def is_subscribed_to(self, endpoint_id:'int', topic_name:'str') -> 'bool':
        """ Returns True if the endpoint is subscribed to the named topic.
        """
        with self.lock:
            return self._is_subscribed_to(endpoint_id, topic_name)

# ################################################################################################################################

    def get_pubsub_tool_by_sub_key(self, sub_key:'str') -> 'PubSubTool':
        with self.lock:
            return self.pubsub_tool_by_sub_key[sub_key]

# ################################################################################################################################

    def add_wsx_client_pubsub_keys(
        self,
        session,          # type: any_
        sql_ws_client_id, # type: int
        sub_key,          # type: str
        channel_name,     # type: str
        pub_client_id,    # type: str
        wsx_info          # type: anydict
    ) -> 'None':
        """ Adds to SQL information that a given WSX client handles messages for sub_key.
        This information is transient - it will be dropped each time a WSX client disconnects
        """
        # Update state in SQL
        ws_sub_key = WebSocketClientPubSubKeys()
        ws_sub_key.client_id = sql_ws_client_id
        ws_sub_key.sub_key = sub_key
        ws_sub_key.cluster_id = self.cluster_id
        session.add(ws_sub_key)

        # Update in-RAM state of workers
        self.broker_client.publish({ # type: ignore
            'action': BROKER_MSG_PUBSUB.SUB_KEY_SERVER_SET.value,
            'cluster_id': self.cluster_id,
            'server_name': self.server.name,
            'server_pid': self.server.pid,
            'sub_key': sub_key,
            'channel_name': channel_name,
            'pub_client_id': pub_client_id,
            'endpoint_type': PUBSUB.ENDPOINT_TYPE.WEB_SOCKETS.id,
            'wsx_info': wsx_info
        })

# ################################################################################################################################

    def format_sk_servers(self, default:'str'='---', sub_pattern_matched:'str'=default_sub_pattern_matched) -> 'str':

        # Prepare the table
        len_columns = len(self.sk_server_table_columns)

        table = Texttable()
        _ = table.set_cols_width(self.sk_server_table_columns)
        _ = table.set_cols_dtype(['t'] * len_columns)
        _ = table.set_cols_align(['c'] * len_columns)
        _ = table.set_cols_valign(['m'] * len_columns)

        # Add headers
        rows = [['#', 'created', 'name', 'pid', 'channel_name', 'sub_key']] # type: anylist

        servers = list(self.sub_key_servers.values())
        servers.sort(key=attrgetter('creation_time', 'channel_name', 'sub_key'), reverse=True)

        for idx, item in enumerate(servers, 1):

            # Let the basic information contain both the sub_key and the pattern matched during subscription.
            sub = self.get_subscription_by_sub_key(item.sub_key)
            if sub:
                sub_pattern_matched = sub.sub_pattern_matched
            else:
                sub_pattern_matched = sub_pattern_matched or default_sub_pattern_matched
            basic_info = f'{item.sub_key} -> {sub_pattern_matched}'

            sub_key_info = [basic_info]

            if item.wsx_info:
                for name in ('swc', 'name', 'pub_client_id', 'peer_fqdn', 'forwarded_for_fqdn'):
                    if isinstance(name, bytes):
                        name = name.decode('utf8')
                    value = item.wsx_info[name]

                    if isinstance(value, bytes):
                        value = value.decode('utf8')

                    if isinstance(value, str):
                        value = value.strip()

                    sub_key_info.append('{}: {}'.format(name, value))

            rows.append([
                idx,
                item.creation_time,
                item.server_name,
                item.server_pid,
                item.channel_name or default,
                '\n'.join(sub_key_info),
            ])

        # Add all rows to the table
        _ = table.add_rows(rows)

        # And return already formatted output
        return cast_('str', table.draw())

# ################################################################################################################################

    def _set_sub_key_server(
        self,
        config, # type: stranydict
        _endpoint_type=PUBSUB.ENDPOINT_TYPE # type: type_[PUBSUB.ENDPOINT_TYPE]
    ) -> 'None':
        """ Low-level implementation of self.set_sub_key_server - must be called with self.lock held.
        """
        sub = self._get_subscription_by_sub_key(config['sub_key'])
        config['endpoint_id'] = sub.endpoint_id
        config['endpoint_name'] = self.endpoint_api.get_by_id(sub.endpoint_id)
        self.sub_key_servers[config['sub_key']] = SubKeyServer(config)

        endpoint_type = config['endpoint_type']

        config['wsx'] = int(endpoint_type == _endpoint_type.WEB_SOCKETS.id)
        config['srv'] = int(endpoint_type == _endpoint_type.SERVICE.id)

        sks_table = self.format_sk_servers()
        msg = 'Set sk_server{}for sub_key `%(sub_key)s` (wsx/srv:%(wsx)s/%(srv)s) - `%(server_name)s:%(server_pid)s`, ' + \
            'current sk_servers:\n{}'
        msg = msg.format(' ' if config['server_pid'] else ' (no PID) ', sks_table)

        logger.info(msg, config)
        logger_zato.info(msg, config)

# ################################################################################################################################

    def set_sub_key_server(self, config:'anydict') -> 'None':
        with self.lock:
            self._set_sub_key_server(config)

# ################################################################################################################################

    def _get_sub_key_server(self, sub_key:'str', default:'any_'=None) -> 'sksnone': # type: ignore[valid-type]
        return self.sub_key_servers.get(sub_key, default)

# ################################################################################################################################

    def get_sub_key_server(self, sub_key:'str', default:'any_'=None) -> 'sksnone': # type: ignore[valid-type]
        with self.lock:
            return self._get_sub_key_server(sub_key, default)

# ################################################################################################################################

    def get_delivery_server_by_sub_key(self, sub_key:'str', needs_lock:'bool'=True) -> 'sksnone': # type: ignore[valid-type]
        if needs_lock:
            with self.lock:
                return self._get_sub_key_server(sub_key)
        else:
            return self._get_sub_key_server(sub_key)

# ################################################################################################################################

    def delete_sub_key_server(self, sub_key:'str', sub_pattern_matched:'str'='') -> 'None':
        with self.lock:
            sub_key_server = self.sub_key_servers.get(sub_key)
            if sub_key_server:
                msg = 'Deleting sk_server for sub_key `%s`, was `%s:%s`'

                logger.info(msg, sub_key, sub_key_server.server_name, sub_key_server.server_pid)
                logger_zato.info(msg, sub_key, sub_key_server.server_name, sub_key_server.server_pid)

                _ = self.sub_key_servers.pop(sub_key, None)

                sks_table = self.format_sk_servers(sub_pattern_matched=sub_pattern_matched)
                msg_sks = 'Current sk_servers after deletion of `%s`:\n%s'

                logger.info(msg_sks, sub_key, sks_table)
                logger_zato.info(msg_sks, sub_key, sks_table)

            else:
                logger.info('Could not find sub_key `%s` while deleting sub_key server, current `%s` `%s`',
                    sub_key, self.server.name, self.server.pid)

# ################################################################################################################################

    def remove_ws_sub_key_server(self, config:'stranydict') -> 'None':
        """ Called after a WSX client disconnects - provides a list of sub_keys that it handled
        which we must remove from our config because without this client they are no longer usable (until the client reconnects).
        """
        with self.lock:
            for sub_key in config['sub_key_list']:
                _ = self.sub_key_servers.pop(sub_key, None)

                # ->> Compare this loop with the .pop call above
                for server_info in self.sub_key_servers.values():
                    if server_info.sub_key == sub_key:
                        del self.sub_key_servers[sub_key]
                        break

# ################################################################################################################################

    def get_server_pid_for_sub_key(self, server_name:'str', sub_key:'str') -> 'intnone':
        """ Invokes a named server on current cluster and asks it for PID of one its processes that handles sub_key.
        Returns that PID or None if the information could not be obtained.
        """
        try:
            response = self.server.rpc[server_name].invoke('zato.pubsub.delivery.get-server-pid-for-sub-key', {
                'sub_key': sub_key,
            }) # type: anydict
        except Exception:
            msg = 'Could not invoke server `%s` to get PID for sub_key `%s`, e:`%s`'
            exc_formatted = format_exc()
            logger.warning(msg, server_name, sub_key, exc_formatted)
            logger_zato.warning(msg, server_name, sub_key, exc_formatted)
        else:
            return response['response']['server_pid']

# ################################################################################################################################

    def add_missing_server_for_sub_key(
        self,
        sub_key, # type: str
        is_wsx,  # type: bool
        _wsx=PUBSUB.ENDPOINT_TYPE.WEB_SOCKETS.id # type: str
    ) -> 'None':
        """ Adds to self.sub_key_servers information from ODB about which server handles input sub_key.
        Must be called with self.lock held.
        """
        with closing(self.new_session_func()) as session:
            data = get_delivery_server_for_sub_key(session, self.server.cluster_id, sub_key, is_wsx)

        if not data:
            if self.log_if_deliv_server_not_found:
                if is_wsx and (not self.log_if_wsx_deliv_server_not_found):
                    return
                msg = 'Could not find a delivery server in ODB for sub_key `%s` (wsx:%s)'
                logger.info(msg, sub_key, is_wsx)
        else:

            endpoint_type = _wsx if is_wsx else data.endpoint_type

            # This is common config that we already know is valid but on top of it
            # we will try to the server found and ask about PID that handles messages for sub_key.
            config = {
                'sub_key': sub_key,
                'cluster_id': data.cluster_id,
                'server_name': data.server_name,
                'endpoint_type': endpoint_type,
            } # type: anydict

            # Guaranteed to either set PID or None
            config['server_pid'] = self.get_server_pid_for_sub_key(data.server_name, sub_key)

            # OK, set up the server with what we found above
            self._set_sub_key_server(config)

# ################################################################################################################################

    def get_task_servers_by_sub_keys(self, sub_key_data:'dictlist') -> 'anytuple':
        """ Returns a dictionary keyed by (server_name, server_pid, pub_client_id, channel_name) tuples
        and values being sub_keys that a WSX client pointed to by each key has subscribed to.
        """
        with self.lock:
            found = {}     # type: anydict
            not_found = [] # type: anylist

            for elem in sub_key_data:

                sub_key = elem['sub_key']
                is_wsx = elem['is_wsx']

                # If we do not have a server for this sub_key we first attempt to find
                # if there is an already running server that handles it but we do not know it yet.
                # It may happen if our server is down, another server (for this sub_key) boots up
                # and notifies other servers about its existence and the fact that we handle this sub_key
                # but we are still down so we never receive this message. In this case we attempt to look up
                # the target server in ODB and then invoke it to get the PID of worker process that handles
                # sub_key, populating self.sub_key_servers as we go.
                if not sub_key in self.sub_key_servers:
                    self.add_missing_server_for_sub_key(sub_key, is_wsx)

                # At this point, if there is any information about this sub_key at all,
                # no matter if its server is running or not, info will not be None.
                info = self.sub_key_servers.get(sub_key)

                # We report that a server is found only if we know the server itself and its concrete PID,
                # which means that the server is currently running. Checking for server alone is not enough
                # because we may have read this information from self.add_missing_server_for_sub_key
                # and yet, self.get_server_pid_for_sub_key may have returned no information implying
                # that the server, even if found in ODB in principle, is still currently not running.
                if info and info.server_pid:
                    _key = (info.server_name, info.server_pid, info.pub_client_id, info.channel_name, info.endpoint_type)
                    _info = found.setdefault(_key, [])
                    _info.append(sub_key)
                else:
                    not_found.append(sub_key)

            return found, not_found

# ################################################################################################################################

    def get_sql_messages_by_sub_key(
        self,
        session,      # type: any_
        sub_key_list, # type: strlist
        last_sql_run, # type: float
        pub_time_max, # type: float
        ignore_list   # type: intset
    ) -> 'anytuple':
        """ Returns all SQL messages queued up for all keys from sub_key_list.
        """
        if not session:
            session = self.new_session_func()
            needs_close = True
        else:
            needs_close = False

        try:
            return _get_sql_messages_by_sub_key(session, self.server.cluster_id, sub_key_list,
                last_sql_run, pub_time_max, ignore_list)
        finally:
            if needs_close:
                session.close()

# ################################################################################################################################

    def get_initial_sql_msg_ids_by_sub_key(
        self,
        session:'SASession',
        sub_key:'str',
        pub_time_max:'float'
    ) -> 'anytuple':
        return _get_sql_msg_ids_by_sub_key(session, self.server.cluster_id, sub_key, 0.0, pub_time_max).\
               all()

# ################################################################################################################################

    def get_sql_messages_by_msg_id_list(
        self,
        session,      # type: any_
        sub_key,      # type: str
        pub_time_max, # type: float
        msg_id_list   # type: strlist
    ) -> 'anytuple':
        return _get_sql_messages_by_msg_id_list(session, self.server.cluster_id, sub_key, pub_time_max, msg_id_list).\
               all()

# ################################################################################################################################

    def confirm_pubsub_msg_delivered(
        self,
        sub_key,                  # type: str
        delivered_pub_msg_id_list # type: strlist
    ) -> 'None':
        """ Sets in SQL delivery status of a given message to True.
        """
        with closing(self.new_session_func()) as session:
            _confirm_pubsub_msg_delivered(session, self.server.cluster_id, sub_key, delivered_pub_msg_id_list, utcnow_as_ms())
            session.commit()

# ################################################################################################################################

    def store_in_ram(
        self,
        cid,        # type: str
        topic_id,   # type: int
        topic_name, # type: str
        sub_keys,   # type: strlist
        non_gd_msg_list, # type: dictlist
        error_source='', # type: str
        _logger=logger   # type: logging.Logger
    ) -> 'None':
        """ Stores in RAM up to input non-GD messages for each sub_key. A backlog queue for each sub_key
        cannot be longer than topic's max_depth_non_gd and overflowed messages are not kept in RAM.
        They are not lost altogether though, because, if enabled by topic's use_overflow_log, all such messages
        go to disk (or to another location that logger_overflown is configured to use).
        """
        _logger.info('Storing in RAM. CID:`%r`, topic ID:`%r`, name:`%r`, sub_keys:`%r`, ngd-list:`%r`, e:`%s`',
            cid, topic_id, topic_name, sub_keys, [elem['pub_msg_id'] for elem in non_gd_msg_list], error_source)

        with self.lock:

            # Store the non-GD messages in backlog ..
            self.sync_backlog.add_messages(cid, topic_id, topic_name, self.topics[topic_id].max_depth_non_gd,
                sub_keys, non_gd_msg_list)

            # .. and set a flag to signal that there are some available.
            self._set_sync_has_msg(topic_id, False, True, 'PubSub.store_in_ram ({})'.format(error_source))

# ################################################################################################################################

    def unsubscribe(self, topic_sub_keys:'strlistdict') -> 'None':
        """ Removes subscriptions for all input sub_keys. Input topic_sub_keys is a dictionary keyed by topic_name,
        and each value is a list of sub_keys, possibly one-element long.
        """
        with self.lock:
            for topic_name, sub_keys in topic_sub_keys.items():

                # We receive topic_names on input but in-RAM backlog requires topic IDs.
                topic_id = self.topic_name_to_id[topic_name]

                # Delete subscriptions, and any related messages, from RAM
                self.sync_backlog.unsubscribe(topic_id, topic_name, sub_keys)

                # Delete subscription metadata from local pubsub, note that we use .get
                # instead of deleting directly because this dictionary will be empty
                # right after a server starts but before any client for that topic (such as WSX) connects to it.
                subscriptions_by_topic = self.subscriptions_by_topic.get(topic_name, [])

                for sub in subscriptions_by_topic[:]:
                    if sub.sub_key in sub_keys:
                        subscriptions_by_topic.remove(sub)

                for sub_key in sub_keys:

                    # Remove mappings between sub_keys and sub objects but keep the subscription object around
                    # because an unsubscribe hook may need it.
                    deleted_sub = self._delete_subscription_by_sub_key(sub_key, ignore_missing=True)

                    # Find and stop all delivery tasks if we are the server that handles them
                    sub_key_server = self.sub_key_servers.get(sub_key)
                    if sub_key_server:

                        _cluster_id = sub_key_server.cluster_id
                        _server_name = sub_key_server.server_name
                        _server_pid = sub_key_server.server_pid

                        cluster_id = self.server.cluster_id
                        server_name = self.server.name
                        server_pid = self.server.pid

                        # If we are the server that handles this particular sub_key ..
                        if _cluster_id == cluster_id and _server_name == server_name and _server_pid == server_pid:

                            # .. then find the pubsub_tool that actually does it ..
                            for pubsub_tool in self.pubsub_tools:
                                if pubsub_tool.handles_sub_key(sub_key):

                                    # .. stop the delivery task ..
                                    pubsub_tool.remove_sub_key(sub_key)

                                    # and remove the mapping of sub_key -> pubsub_tool ..
                                    del self.pubsub_tool_by_sub_key[sub_key]

                                    # .. and invoke the unsubscription hook, if any is given.
                                    hook = self.get_on_unsubscribed_hook(sub=deleted_sub)
                                    if hook:
                                        self.invoke_on_unsubscribed_hook(hook, topic_id, deleted_sub)

                                    # No need to iterate further, there can be only one task for each sub_key
                                    break

# ################################################################################################################################

    def register_pubsub_tool(self, pubsub_tool:'PubSubTool') -> 'None':
        """ Registers a new pubsub_tool for this server, i.e. a new delivery task container.
        """
        self.pubsub_tools.append(pubsub_tool)

# ################################################################################################################################

    def set_pubsub_tool_for_sub_key(self, sub_key:'str', pubsub_tool:'PubSubTool') -> 'None':
        """ Adds a mapping between a sub_key and pubsub_tool handling its messages.
        """
        self.pubsub_tool_by_sub_key[sub_key] = pubsub_tool

# ################################################################################################################################

    def migrate_delivery_server(self, msg:'anydict') -> 'None':
        """ Migrates the delivery task for sub_key to a new server given by ID on input,
        including all current in-RAM messages. This method must be invoked in the same worker process that runs
        delivery task for sub_key.
        """
        _ = self.invoke_service('zato.pubsub.migrate.migrate-delivery-server', {
            'sub_key': msg['sub_key'],
            'old_delivery_server_id': msg['old_delivery_server_id'],
            'new_delivery_server_name': msg['new_delivery_server_name'],
            'endpoint_type': msg['endpoint_type'],
        })

# ################################################################################################################################

    def get_before_delivery_hook(self, sub_key:'str') -> 'callnone':
        """ Returns a hook for messages to be invoked right before they are about to be delivered
        or None if such a hook is not defined for sub_key's topic.
        """
        with self.lock:
            sub = self.get_subscription_by_sub_key(sub_key)
            if sub:
                return self._get_topic_by_name(sub.topic_name).before_delivery_hook_service_invoker

# ################################################################################################################################

    def get_on_subscribed_hook(self, sub_key:'str') -> 'callnone':
        """ Returns a hook triggered when a new subscription is made to a particular topic.
        """
        with self.lock:
            sub = self.get_subscription_by_sub_key(sub_key)
            if sub:
                return self._get_topic_by_name(sub.topic_name).on_subscribed_service_invoker

# ################################################################################################################################

    def get_on_unsubscribed_hook(self, sub_key:'str'='', sub:'subnone'=None) -> 'callnone':
        """ Returns a hook triggered when a client unsubscribes from a topic.
        """
        with self.lock:
            sub = sub or self.get_subscription_by_sub_key(sub_key)
            if sub:
                return self._get_topic_by_name(sub.topic_name).on_unsubscribed_service_invoker

# ################################################################################################################################

    def get_on_outgoing_soap_invoke_hook(self, sub_key:'str') -> 'callnone':
        """ Returns a hook that sends outgoing SOAP Suds connections-based messages or None if there is no such hook
        for sub_key's topic.
        """
        with self.lock:
            sub = self.get_subscription_by_sub_key(sub_key)
            if sub:
                return self._get_topic_by_name(sub.topic_name).on_outgoing_soap_invoke_invoker

# ################################################################################################################################

    def invoke_before_delivery_hook(
        self,
        hook,     # type: callable_
        topic_id, # type: int
        sub_key,  # type: str
        batch,    # type: msgiter
        messages, # type: anydict
        actions=tuple(PUBSUB.HOOK_ACTION()), # type: strtuple
        _deliver=PUBSUB.HOOK_ACTION.DELIVER  # type: str
    ) -> 'None':
        """ Invokes a hook service for each message from a batch of messages possibly to be delivered and arranges
        each one to a specific key in messages dict.
        """
        for msg in batch:
            response = hook(self.topics[topic_id], msg)
            hook_action = response.get('hook_action', _deliver) # type: str

            if hook_action not in actions:
                raise ValueError('Invalid action returned `{}` for msg `{}`'.format(hook_action, msg))
            else:
                messages[hook_action].append(msg)

# ################################################################################################################################

    def invoke_on_outgoing_soap_invoke_hook(self, batch:'anylist', sub:'Subscription', http_soap:'any_') -> 'None':
        hook = self.get_on_outgoing_soap_invoke_hook(sub.sub_key)
        topic = self.get_topic_by_id(sub.config['topic_id'])
        if hook:
            hook(topic, batch, http_soap=http_soap)
        else:
            # We know that this service exists, it just does not implement the expected method
            service_info = self.server.service_store.get_service_info_by_id(topic.config['hook_service_id'])
            service_class = service_info['service_class'] # type: Service
            service_name = service_class.get_name()
            raise Exception('Hook service `{}` does not implement `on_outgoing_soap_invoke` method'.format(service_name))

# ################################################################################################################################

    def _invoke_on_sub_unsub_hook(
        self,
        hook,       # type: callable_
        topic_id,   # type: int
        sub_key='', # type: str
        sub=None    # type: subnone
    ) -> 'any_':
        sub = sub if sub else self._get_subscription_by_sub_key(sub_key)
        return hook(topic=self._get_topic_by_id(topic_id), sub=sub)

# ################################################################################################################################

    def invoke_on_subscribed_hook(self, hook:'callable_', topic_id:'int', sub_key:'str') -> 'any_':
        return self._invoke_on_sub_unsub_hook(hook, topic_id, sub_key, sub=None)

# ################################################################################################################################

    def invoke_on_unsubscribed_hook(self, hook:'callable_', topic_id:'int', sub:'subnone') -> 'any_':
        return self._invoke_on_sub_unsub_hook(hook, topic_id, sub_key='', sub=sub)

# ################################################################################################################################

    def on_broker_msg_HOT_DEPLOY_CREATE_SERVICE(self, services_deployed:'intlist') -> 'None':
        """ Invoked after a package with one or more services is hot-deployed. Goes over all topics
        and updates hooks that any of these services possibly implements.
        """
        with self.lock:
            for topic in self.topics.values():
                hook_service_id = topic.config.get('hook_service_id')
                if hook_service_id in services_deployed:
                    self._set_topic_config_hook_data(topic.config)
                    topic.set_hooks()

# ################################################################################################################################

    def deliver_pubsub_msg(self, sub_key:'str', msg:'msgiter') -> 'any_':
        """ A callback method invoked by pub/sub delivery tasks for one or more message that is to be delivered.
        """
        return self.invoke_service('zato.pubsub.delivery.deliver-message', {
            'msg':msg,
            'subscription':self.get_subscription_by_sub_key(sub_key)
        })

# ################################################################################################################################

    def set_to_delete(self, sub_key:'str', msg_list:'strlistempty') -> 'None':
        """ Marks all input messages as ready to be deleted.
        """
        logger.info('Deleting messages set to be deleted `%s`', msg_list)

        with closing(self.new_session_func()) as session:
            set_to_delete(session, self.cluster_id, sub_key, msg_list, utcnow_as_ms())

# ################################################################################################################################

    def topic_lock(self, topic_name:'str') -> 'Lock':
        return self.server.zato_lock_manager('zato.pubsub.publish.%s' % topic_name)

# ################################################################################################################################

    def invoke_service(self, name:'str', msg:'any_', *args:'any_', **kwargs:'any_') -> 'any_':
        return self.server.invoke(name, msg, *args, **kwargs)

# ################################################################################################################################

    def after_gd_sync_error(self,
        topic_id,     # type: int
        source,       # type: str
        pub_time_max, # type: float
        _float_str=PUBSUB.FLOAT_STRING_CONVERT # type: str
    ) -> 'None':
        """ Invoked by the after-publish service in case there was an error with letting
        a delivery task know about GD messages it was to handle. Resets the topic's
        sync_has_gd_msg flag to True to make sure the notification will be resent
        in the main loop's next iteration.
        """
        # Get the topic object
        topic = self.topics[topic_id] # type: Topic

        # Store information about what we are about to do
        logger.info('Will resubmit GD messages after sync error; topic:`%s`, src:`%s`', topic.name, source)

        with self.lock:

            # We need to use the correct value of pub_time_max - since we are resyncing
            # a failed message for a delivery task, it is possible that in the meantime
            # another message was published to the topic so in case topic's gd_pub_time_max
            # is bigger than our pub_time_max, the value from topic takes precedence.
            topic_gd_pub_time_max = topic.gd_pub_time_max

            if topic_gd_pub_time_max > pub_time_max:
                logger.warning('Choosing topic\'s gd_pub_time_max:`%s` over `%s`',
                    topic_gd_pub_time_max, _float_str.format(pub_time_max))
                new_pub_time_max = topic_gd_pub_time_max
            else:
                new_pub_time_max = pub_time_max

            self._set_sync_has_msg(topic_id, True, True, source, new_pub_time_max)

# ################################################################################################################################

    def _set_sync_has_msg(self,
        topic_id,            # type: int
        is_gd,               # type: bool
        value,               # type: bool
        source,              # type: str
        gd_pub_time_max=0.0  # type: float
    ) -> 'None':
        """ Updates a given topic's flags indicating that a message has been published since the last sync.
        Must be called with self.lock held.
        """
        topic = self.topics[topic_id] # type: Topic
        if is_gd:
            topic.sync_has_gd_msg = value
            topic.gd_pub_time_max = gd_pub_time_max
        else:
            topic.sync_has_non_gd_msg = value

# ################################################################################################################################

    def set_sync_has_msg(self,
        *,
        topic_id,       # type: int
        is_gd,          # type: bool
        value,          # type: bool
        source,         # type: str
        gd_pub_time_max # type: float
    ) -> 'None':
        with self.lock:
            self._set_sync_has_msg(topic_id, is_gd, value, source, gd_pub_time_max)

# ################################################################################################################################

    def trigger_notify_pubsub_tasks(self) -> 'any_':
        """ A background greenlet which periodically lets delivery tasks know that there are perhaps
        new GD messages for the topic that this class represents.
        """

        # Local aliases

        _current_iter = 0
        _new_cid      = new_cid
        _spawn        = cast_('callable_', spawn)
        _sleep        = cast_('callable_', sleep)
        _self_lock    = self.lock
        _self_topics  = self.topics

        _logger_info      = logger.info
        _logger_warn      = logger.warning
        _logger_zato_warn = logger_zato.warning

        _self_invoke_service   = self.invoke_service
        _self_set_sync_has_msg = self._set_sync_has_msg

        _self_get_subscriptions_by_topic     = self.get_subscriptions_by_topic
        _self_get_delivery_server_by_sub_key = self.get_delivery_server_by_sub_key

        _sync_backlog_get_delete_messages_by_sub_keys = self.sync_backlog.get_delete_messages_by_sub_keys

# ################################################################################################################################

        def _cmp_non_gd_msg(elem:'anydict') -> 'float':
            return elem['pub_time']

# ################################################################################################################################

        # Loop forever or until stopped
        while self.keep_running:

            # Optionally, we may have a limit on how many iterations this loop should last
            # and we need to check if we have reached it.
            if self.sync_max_iters:
                if _current_iter >= self.sync_max_iters:
                    self.keep_running = False

            # This may be handy for logging purposes, even if there is no max. for the loop iters
            _current_iter += 1

            # Sleep for a while before continuing - the call to sleep is here because this while loop is quite long
            # so it would be inconvenient to have it down below.
            _sleep(0.01)

            # Blocks other pub/sub processes for a moment
            with _self_lock:

                # Will map a few temporary objects down below
                topic_id_dict = {} # type: intanydict

                # Get all topics ..
                for _topic in _self_topics.values(): # type: Topic

                    # Does the topic require task synchronization now?
                    if not _topic.needs_task_sync():
                        continue
                    else:
                        _topic.update_task_sync_time()

                    # OK, the time has come for this topic to sync its state with subscribers
                    # but still skip it if we know that there have been no messages published to it since the last time.
                    if not (_topic.sync_has_gd_msg or _topic.sync_has_non_gd_msg):
                        continue

                    # There are some messages, let's see if there are subscribers ..
                    subs = [] # type: sublist
                    _subs = _self_get_subscriptions_by_topic(_topic.name)

                    # Filter out subscriptions for whom we have no subscription servers
                    for _sub in _subs:
                        if _self_get_delivery_server_by_sub_key(_sub.sub_key):
                            subs.append(_sub)

                    # .. if there are any subscriptions at all, we store that information for later use.
                    if subs:
                        topic_id_dict[_topic.id] = (_topic.name, subs)

                # OK, if we had any subscriptions for at least one topic and there are any messages waiting,
                # we can continue.
                try:

                    for topic_id in topic_id_dict:

                        topic = _self_topics[topic_id]

                        # .. get the temporary metadata object stored earlier ..
                        topic_name, subs = topic_id_dict[topic_id]

                        cid = _new_cid()
                        _logger_info('Triggering sync for `%s` len_s:%d gd:%d ngd:%d cid:%s',
                            topic_name, len(subs), topic.sync_has_gd_msg, topic.sync_has_non_gd_msg, cid)

                        # Build a list of sub_keys for whom we know what their delivery server is which will
                        # allow us to send messages only to tasks that are known to be up.
                        sub_keys = [item.sub_key for item in subs]

                        # Continue only if there are actually any sub_keys left = any tasks up and running ..
                        if sub_keys:

                            non_gd_msg_list = _sync_backlog_get_delete_messages_by_sub_keys(topic_id, sub_keys)

                            # .. also, continue only if there are still messages for the ones that are up ..
                            if topic.sync_has_gd_msg or topic.sync_has_non_gd_msg:

                                # Note that we may have both GD and non-GD messages on input
                                # and we need to have a max that takes both into account.
                                max_gd = 0
                                max_non_gd = 0

                                # If there are any non-GD messages, get their max. pub time
                                if non_gd_msg_list:
                                    non_gd_msg_list = sorted(non_gd_msg_list, key=_cmp_non_gd_msg)
                                    max_non_gd = non_gd_msg_list[-1]['pub_time']

                                # This will be always available, even if with a value of 0.0
                                max_gd = topic.gd_pub_time_max

                                # Now, we can build a max. pub time that takes GD and non-GD into account.
                                pub_time_max = max(max_gd, max_non_gd)

                                non_gd_msg_list_msg_id_list = [elem['pub_msg_id'] for elem in non_gd_msg_list]

                                _logger_info('Forwarding messages to a task for `%s` ngd-list:%s (sk_list:%s) cid:%s',
                                    topic_name, non_gd_msg_list_msg_id_list, sub_keys, cid)

                                # .. and notify all the tasks in background.
                                _ = _spawn(_self_invoke_service, 'zato.pubsub.after-publish', {
                                    'cid': cid,
                                    'topic_id':topic_id,
                                    'topic_name':topic_name,
                                    'subscriptions': subs,
                                    'non_gd_msg_list': non_gd_msg_list,
                                    'has_gd_msg_list': topic.sync_has_gd_msg,
                                    'is_bg_call': True, # This is a background call, i.e. issued by this trigger,
                                    'pub_time_max': pub_time_max, # Last time either a non-GD or GD message was received
                                })

                        # OK, we can now reset message flags for the topic
                        _self_set_sync_has_msg(topic_id, True, False, 'PubSub.loop')
                        _self_set_sync_has_msg(topic_id, False, False, 'PubSub.loop')

                except Exception:
                    e_formatted = format_exc()
                    _logger_zato_warn(e_formatted)
                    _logger_warn(e_formatted)

# ################################################################################################################################
# ################################################################################################################################

# Public API methods

# ################################################################################################################################

    def _find_wsx_environ(self, service:'Service') -> 'stranydict':
        wsx_environ = service.wsgi_environ.get('zato.request_ctx.async_msg', {}).get('environ')
        if not wsx_environ:
            raise Exception('Could not find `[\'zato.request_ctx.async_msg\'][\'environ\']` in WSGI environ `{}`'.format(
                service.wsgi_environ))
        else:
            return wsx_environ

# ################################################################################################################################
# ################################################################################################################################

    def publish(self, name:'any_', *args:'any_', **kwargs:'any_') -> 'any_':
        """ Publishes a new message to input name, which may point either to a topic or service.
        POST /zato/pubsub/topic/{topic_name}
        """
        # We need to import it here to avoid circular imports
        from zato.server.service import Service

        # Initialize here for type checking
        ws_channel_id = None

        # For later use
        from_service = cast_('Service', kwargs.get('service'))
        ext_client_id = from_service.name if from_service else kwargs.get('ext_client_id')

        # The first one is used if name is a service, the other one if it is a regular topic
        correl_id = kwargs.get('cid') or kwargs.get('correl_id')

        has_gd = kwargs.get('has_gd')
        has_gd = cast_('bool', has_gd)

        # By default, assume that cannot find any endpoint on input
        endpoint_id = None

        # If this is a WebSocket, we need to find its ws_channel_id ..
        if from_service:
            wsx_environ = find_wsx_environ(from_service, raise_if_not_found=False)
            if wsx_environ:
                wsx_config = wsx_environ['ws_channel_config'] # type: WSXConnectorConfig
                ws_channel_id = wsx_config.id
                endpoint_id = self.get_endpoint_id_by_ws_channel_id(ws_channel_id)

        # Otherwise, use various default data.
        if not endpoint_id:
            endpoint_id = kwargs.get('endpoint_id') or self.server.default_internal_pubsub_endpoint_id
            endpoint_id = cast_('int', endpoint_id)

        # If input name is a topic, let us just use it
        if self.has_topic_by_name(name):
            topic_name = name

            # There is no particular Zato context if the topic name is not really a service name
            zato_ctx = None

        # Otherwise, if there is no topic by input name, it may be actually a service name ..
        else:

            # .. it may be a Python class representing the service ..
            if isclass(name) and issubclass(name, Service):
                name = name.get_name()
            else:
                name = cast_('str', name)

            # .. but if there is no such service at all, we give up.
            if not self.server.service_store.has_service(name):
                raise ValueError('No such service `{}`'.format(name))

            # At this point we know this is a service so we may build the topic's full name,
            # taking into account the fact that a service's name is arbitrary string
            # so we need to make it filesystem-safe.
            topic_name = PUBSUB.TOPIC_PATTERN.TO_SERVICE.format(fs_safe_name(name))

            # We continue only if the publisher is allowed to publish messages to that service.
            if not self.is_allowed_pub_topic_by_endpoint_id(topic_name, endpoint_id):
                msg = 'No pub pattern matched service `{}` and endpoint `{}` (#1)'.format(
                    name, self.get_endpoint_by_id(endpoint_id).name)
                raise ValueError(msg)

            # We create a topic for that service to receive messages from unless it already exists
            if not self.has_topic_by_name(topic_name):
                self.create_topic_for_service(name, topic_name)
                _ = self.wait_for_topic(topic_name)

            # Messages published to services always use GD
            has_gd = True

            # Subscribe the default service delivery endpoint to messages from this topic

            endpoint = self.get_endpoint_by_name(PUBSUB.SERVICE_SUBSCRIBER.NAME)
            if not self.is_subscribed_to(endpoint.id, topic_name):

                # Subscribe the service to this topic ..
                sub_key = self.subscribe(topic_name, endpoint_name=endpoint.name, is_internal=True, delivery_batch_size=1)

                # .. and configure pub/sub metadata for the newly created subscription.
                self.set_config_for_service_subscription(sub_key)

            # We need a Zato context to relay information about the service pointed to by the published message
            zato_ctx = {
                'target_service_name': name
            }

        data = kwargs.get('data') or ''
        data_list = kwargs.get('data_list') or []
        msg_id = kwargs.get('msg_id') or ''
        priority = kwargs.get('priority')
        expiration = kwargs.get('expiration')
        mime_type = kwargs.get('mime_type')
        in_reply_to = kwargs.get('in_reply_to')
        ext_pub_time = kwargs.get('ext_pub_time')
        reply_to_sk = kwargs.get('reply_to_sk')
        deliver_to_sk = kwargs.get('deliver_to_sk')
        user_ctx = kwargs.get('user_ctx')
        zato_ctx = zato_ctx or kwargs.get('zato_ctx')

        request = {
            'topic_name': topic_name,
            'data': data,
            'data_list': data_list,
            'msg_id': msg_id,
            'has_gd': has_gd,
            'priority': priority,
            'expiration': expiration,
            'mime_type': mime_type,
            'correl_id': correl_id,
            'in_reply_to': in_reply_to,
            'ext_client_id': ext_client_id,
            'ext_pub_time': ext_pub_time,
            'endpoint_id': endpoint_id,
            'ws_channel_id': ws_channel_id,
            'reply_to_sk': reply_to_sk,
            'deliver_to_sk': deliver_to_sk,
            'user_ctx': user_ctx,
            'zato_ctx': zato_ctx,
        } # type: commondict

        response = self.invoke_service('zato.pubsub.publish.publish', request, serialize=False)

        if response.has_data():
            return response.get('msg_id') or response.get('msg_id_list')

# ################################################################################################################################
# ################################################################################################################################

    def get_messages(self,
        topic_name,            # type: str
        sub_key,               # type: str
        /,
        needs_details=False,   # type: bool
        needs_msg_id=False,    # type: bool
        _skip=skip_to_external # type: strtuple
        ) -> 'anylist':
        """ Returns messages from a subscriber's queue, deleting them from the queue in progress.
        POST /zato/pubsub/topic/{topic_name}?sub_key=...
        """
        response = self.invoke_service('zato.pubsub.endpoint.get-delivery-messages', {
            'cluster_id': self.server.cluster_id,
            'sub_key': sub_key,
        }, serialize=False)

        # Already includes all the details ..
        if needs_details:
            return response

        # .. otherwise, we need to make sure they are not returned
        out = [] # type: anylist
        for item in response:
            for name in _skip:
                value = item.pop(name, None)
                if needs_msg_id and name == 'pub_msg_id':
                    item['msg_id'] = value
            out.append(item)
        return out

# ################################################################################################################################
# ################################################################################################################################

    def read_messages(self,
        topic_name, # type: str
        sub_key,    # type: str
        has_gd,     # type: bool
        *args,      # type: any_
        **kwargs    # type: any_
    ) -> 'any_':
        """ Looks up messages in subscriber's queue by input criteria without deleting them from the queue.
        """
        service_name = _service_read_messages_gd if has_gd else _service_read_messages_non_gd

        paginate = kwargs.get('paginate') or True
        query = kwargs.get('query') or ''
        cur_page = kwargs.get('cur_page') or 1

        return self.invoke_service(service_name, {
            'cluster_id': self.server.cluster_id,
            'sub_key': sub_key,
            'paginate': paginate,
            'query': query,
            'cur_page': cur_page,
        }, serialize=False).response

# ################################################################################################################################
# ################################################################################################################################

    def read_message(self,
        topic_name, # type: str
        msg_id,     # type: str
        has_gd,     # type: bool
        *args,      # type: any_
        **kwargs    # type: any_
    ) -> 'any_':
        """ Returns details of a particular message without deleting it from the subscriber's queue.
        """
        # Forward reference
        service_data = {} # type: commondict

        if has_gd:
            service_name = _service_read_message_gd
            service_data = {
                'cluster_id': self.server.cluster_id,
                'msg_id': msg_id
            }
        else:
            sub_key = kwargs.get('sub_key')
            server_name = kwargs.get('server_name')
            server_pid = kwargs.get('server_pid')

            if not(sub_key and server_name and server_pid):
                raise Exception('All of sub_key, server_name and server_pid are required for non-GD messages')

            service_name = _service_read_message_non_gd
            service_data = {
                'cluster_id': self.server.cluster_id,
                'msg_id': msg_id,
                'sub_key': sub_key,
                'server_name': server_name,
                'server_pid': server_pid,
            }

        return self.invoke_service(service_name, service_data, serialize=False).response

# ################################################################################################################################
# ################################################################################################################################

    def delete_message(self, sub_key:'str', msg_id:'str', has_gd:'bool', *args:'anytuple', **kwargs:'any_') -> 'any_':
        """ Deletes a message from a subscriber's queue.
        DELETE /zato/pubsub/msg/{msg_id}
        """
        service_data = {
            'sub_key': sub_key,
            'msg_id': msg_id,
        } # type: stranydict

        if has_gd:
            service_name = _service_delete_message_gd
            service_data['cluster_id'] = self.server.cluster_id
        else:
            server_name = cast_('str', kwargs.get('server_name', ''))
            server_pid  = cast_('int', kwargs.get('server_pid', 0))

            if not(sub_key and server_name and server_pid):
                raise Exception('All of sub_key, server_name and server_pid are required for non-GD messages')

            service_name = _service_delete_message_non_gd
            service_data['server_name'] = server_name
            service_data['server_pid'] = server_pid

        # There is no response currently but one may be added at a later time
        return self.invoke_service(service_name, service_data, serialize=False)

# ################################################################################################################################
# ################################################################################################################################

    def subscribe(self,
        topic_name, # type: str
        _find_wsx_environ=find_wsx_environ, # type: callable_
        **kwargs # type: any_
    ) -> 'str':

        # Forward reference
        wsgi_environ = {} # type: stranydict

        # Are we going to subscribe a WSX client?
        use_current_wsx = kwargs.get('use_current_wsx')

        # This is always needed to invoke the subscription service
        request = {
            'topic_name': topic_name,
            'is_internal': kwargs.get('is_internal') or False,
            'wrap_one_msg_in_list': kwargs.get('wrap_one_msg_in_list', True),
            'delivery_batch_size': kwargs.get('delivery_batch_size', PUBSUB.DEFAULT.DELIVERY_BATCH_SIZE),
        } # type: stranydict

        # This is a subscription for a WebSocket client ..
        if use_current_wsx:
            service = cast_('Service', kwargs.get('service'))

            if use_current_wsx and (not service):
                raise Exception('Parameter `service` is required if `use_current_wsx` is True')

            # If the caller wants to subscribe a WebSocket, make sure the WebSocket's metadata
            # is given to us on input - the call below will raise an exception if it was not,
            # otherwise it will return WSX metadata out which we can extract our WebSocket object.
            wsx_environ = _find_wsx_environ(service)
            wsx = wsx_environ['web_socket']

            # All set, we can carry on with other steps now
            sub_service_name = PUBSUB.SUBSCRIBE_CLASS.get(PUBSUB.ENDPOINT_TYPE.WEB_SOCKETS.id)
            wsgi_environ = service.wsgi_environ
            kwargs_wsgi_environ = kwargs.get('wsgi_environ') or {}
            wsgi_environ = wsgi_environ or kwargs_wsgi_environ
            wsgi_environ['zato.request_ctx.pubsub.unsub_on_wsx_close'] = kwargs.get('unsub_on_wsx_close')

        # .. this is a subscription for any client that is not WebSockets-based
        else:

            # We do not use WebSockets here
            wsx = None

            # Non-WSX endpoints always need to be identified by their names
            endpoint_name = cast_('str', kwargs.get('endpoint_name'))
            if not endpoint_name:
                raise Exception('Parameter `endpoint_name` is required for non-WebSockets subscriptions')
            else:
                endpoint = self.get_endpoint_by_name(endpoint_name)

            # Required to subscribe non-WSX endpoints
            request['endpoint_id'] = endpoint.id

            sub_service_name = PUBSUB.SUBSCRIBE_CLASS.get(endpoint.endpoint_type)
            wsgi_environ = {} # # type: ignore[no-redef]

        # Actually subscribe the caller
        response = self.invoke_service(sub_service_name, request, wsgi_environ=wsgi_environ, serialize=False)

        # If this was a WebSocket caller, we can now update its pub/sub metadata
        if use_current_wsx:
            if wsx:
                wsx.set_last_interaction_data('pubsub.subscribe')

        return response.sub_key

# ################################################################################################################################
# ################################################################################################################################

    def resume_wsx_subscription(
        self,
        sub_key, # type: str
        service, # type: Service
        _find_wsx_environ=find_wsx_environ # type: callable_
    ) -> 'None':
        """ Invoked by WSX clients that want to resume deliveries of their messages after they reconnect.
        """
        # Get metadata and the WebSocket itself
        wsx_environ = _find_wsx_environ(service)
        wsx = wsx_environ['web_socket'] # type: WebSocket

        # Actual resume subscription
        _ = self.invoke_service('zato.pubsub.resume-wsx-subscription', {
            'sql_ws_client_id': wsx_environ['sql_ws_client_id'],
            'channel_name': wsx_environ['ws_channel_config'].name,
            'pub_client_id': wsx_environ['pub_client_id'],
            'web_socket': wsx,
            'sub_key': sub_key
        }, wsgi_environ=service.wsgi_environ)

        # If we get here, it means the service succeeded so we can update that WebSocket's pub/sub metadata
        wsx.set_last_interaction_data('wsx.resume_wsx_subscription')

        # All done, we can store a new entry in logs now
        peer_info = wsx.get_peer_info_pretty()

        logger.info(MsgConst.wsx_sub_resumed, sub_key, peer_info)
        logger_zato.info(MsgConst.wsx_sub_resumed, sub_key, peer_info)

# ################################################################################################################################
# ################################################################################################################################

    def create_topic(self,
        name,                    # type: str
        has_gd=False,            # type: bool
        accept_on_no_sub=True,   # type: bool
        is_active=True,          # type: bool
        is_internal=False,       # type: bool
        is_api_sub_allowed=True, # type: bool
        hook_service_id=None,    # type: intnone
        task_sync_interval=_ps_default.TASK_SYNC_INTERVAL,         # type: int
        task_delivery_interval=_ps_default.TASK_DELIVERY_INTERVAL, # type: int
        depth_check_freq=_ps_default.DEPTH_CHECK_FREQ,             # type: int
        max_depth_gd=_ps_default.TOPIC_MAX_DEPTH_GD,               # type: int
        max_depth_non_gd=_ps_default.TOPIC_MAX_DEPTH_NON_GD,       # type: int
        pub_buffer_size_gd=_ps_default.PUB_BUFFER_SIZE_GD,         # type: int
    ) -> 'None':

        _ = self.invoke_service('zato.pubsub.topic.create', {
            'cluster_id': self.server.cluster_id,
            'name': name,
            'is_active': is_active,
            'is_internal': is_internal,
            'is_api_sub_allowed': is_api_sub_allowed,
            'has_gd': has_gd,
            'hook_service_id': hook_service_id,
            'on_no_subs_pub': PUBSUB.ON_NO_SUBS_PUB.ACCEPT.id if accept_on_no_sub else PUBSUB.ON_NO_SUBS_PUB.DROP.id,
            'task_sync_interval': task_sync_interval,
            'task_delivery_interval': task_delivery_interval,
            'depth_check_freq': depth_check_freq,
            'max_depth_gd': max_depth_gd,
            'max_depth_non_gd': max_depth_non_gd,
            'pub_buffer_size_gd': pub_buffer_size_gd,
        })

# ################################################################################################################################
# ################################################################################################################################

sksnone      = optional[SubKeyServer]
strtopicdict = dict_[str, Topic]
inttopicdict = dict_[int, Topic]

# ################################################################################################################################
# ################################################################################################################################
