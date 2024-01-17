'''# -*- coding: utf-8 -*-

"""
Copyright (C) 2024, Zato Source s.r.o. https://zato.io

Licensed under AGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
from logging import getLogger

# gevent
from gevent.lock import RLock

# Zato
from zato.common.api import Groups, Sec_Def_Type
from zato.common.crypto.api import is_string_equal
from zato.common.groups import Member
from zato.server.service import Service

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from zato.common.typing_ import anylist, boolnone, dict_, intanydict, intlist, intnone, intset, list_, strlist
    from zato.server.base.parallel import ParallelServer

# ################################################################################################################################
# ################################################################################################################################

logger = getLogger(__name__)

# ################################################################################################################################
# ################################################################################################################################

class _BasicAuthSecDef:
    security_id: 'int'
    username: 'str'
    password: 'str'

# ################################################################################################################################
# ################################################################################################################################

class _APIKeySecDef:
    security_id: 'int'
    header_value: 'str'

# ################################################################################################################################
# ################################################################################################################################

class SecurityGroupCtx:
    """ An instance of this class is attached to each channel using security groups.
    """

    # ID of a channel this ctx object is attached to
    channel_id: 'int'

    # IDs of all the security groups attached to this channel
    security_groups: 'intset'

    # Maps group IDs to security IDs
    group_to_sec_map: 'intanydict'

    # Maps group IDs to member IDs
    group_to_member_map: 'intanydict'

    # Maps usernames to _BasicAuthSecDef objects
    basic_auth_credentials: 'dict_[str, _BasicAuthSecDef]'

    # Maps header values to _APIKeySecDef objects
    apikey_credentials: 'dict_[str, _APIKeySecDef]'

    def __init__(self) -> 'None':

        #self.members_to_sec = {}
        #self.sec_to_member = {}

        self.group_to_sec_map = {}
        self.security_groups = set()

        self.basic_auth_credentials = {}
        self.apikey_credentials = {}

        self._lock = RLock()

# ################################################################################################################################

    def add_member(self, member_id:'int', security_id:'int') -> 'None':
        return
        '''
        with self._lock:
            self.members_to_sec[member_id] = security_id
            self.sec_to_member[security_id] = member_id
        '''

# ################################################################################################################################

    # def add_security_group(self, group_id:'int') -> 'None':
    #     with self._lock:
    #         self.members.add(group_id)

# ################################################################################################################################

    def check_security_basic_auth(self, cid:'str', channel_name:'str', username:'str', password:'str') -> 'intnone':

        if sec_info := self.basic_auth_credentials.get(username):
            if is_string_equal(password, sec_info.password):
                return sec_info.security_id
            else:
                logger.info(f'Invalid password; username={username}; channel={channel_name}; cid={cid}')
        else:
            logger.info(f'Username not found; username={username}; channel={channel_name}; cid={cid}')

# ################################################################################################################################

    def _get_basic_auth_by_security_id(self, security_id:'int') -> '_BasicAuthSecDef | None':

        for value in self.basic_auth_credentials.values():
            if value.security_id == security_id:
                return value

# ################################################################################################################################

    def _create_basic_auth(
        self,
        group_id:'int',
        member_id:'int',
        security_id:'int',
        username:'str',
        password:'str'
    ) -> 'None':

        # Build a business object containing all the data needed in runtime ..
        item = _BasicAuthSecDef()
        item.security_id = security_id
        item.username = username
        item.password = password

        # .. and add the business object to our container ..
        self.basic_auth_credentials[username] = item

        # .. store information that we are aware of this group ..
        self.security_groups.add(group_id)

        # .. map the group ID to a list of security definitions that are related to it, ..
        # .. note that a single group may point to multiple security IDs ..
        sec_def_id_list = self.group_to_sec_map.setdefault(group_id, set())
        sec_def_id_list.add(security_id)

        # .. same as above but maps groups to their members ..
        sec_def_id_list = self.group_to_sec_map.setdefault(group_id, set())
        sec_def_id_list.add(security_id)

# ################################################################################################################################

    def _create_apikey(
        self,
        security_id:'int',
        header_value:'str',
    ) -> 'None':

        # .. build a business object containing all the data needed in runtime ..
        item = _APIKeySecDef()
        item.security_id = security_id
        item.header_value = header_value

        # .. add the business object to our container.
        self.apikey_credentials[item.header_value] = item

# ################################################################################################################################

    def edit_basic_auth(self, security_id:'int', current_username:'str', password:'str') -> 'None':

        with self._lock:
            if self._delete_basic_auth(security_id):
                self._create_basic_auth(security_id, current_username, password)

# ################################################################################################################################

    def _delete_basic_auth(self, security_id:'int') -> 'boolnone':
        if sec_info := self._get_basic_auth_by_security_id(security_id):
            _ = self.basic_auth_credentials.pop(sec_info.username, None)
            return True

# ################################################################################################################################

    def delete_basic_auth(self, security_id:'int') -> 'None':
        with self._lock:
            _ = self._delete_basic_auth(security_id)

# ################################################################################################################################

    def check_security_apikey(self, cid:'str', channel_name:'str', header_value:'str') -> 'intnone':

        if sec_info := self.apikey_credentials.get(header_value):
            return sec_info.security_id
        else:
            logger.info(f'Invalid API key; channel={channel_name}; cid={cid}')

# ################################################################################################################################

    def on_basic_auth_created(
        self,
        group_id:'int',
        member_id:'int',
        security_id:'int',
        username:'str',
        password:'str'
    ) -> 'None':

        # Creates a new Basic Auth groups definition for this context object
        self._create_basic_auth(group_id, member_id, security_id, username, password)

    def on_basic_auth_edited(self):

        # Delete the credentials from the main map
        # Add new credentials on place of the deleted ones
        pass

    def on_basic_auth_deleted(self):

        # Delete the credentials from the main map
        # Delete security_id from any maps that point to it
        pass

# ################################################################################################################################

    def on_apikey_created(
        self,
        security_id:'int',
        header_value:'str',
    ) -> 'None':

        # Creates a new API key groups definition for this context object
        self._create_apikey(security_id, header_value)

    def on_apikey_edited(self):
        pass

    def on_apikey_deleted(self):
        pass

# ################################################################################################################################

    def on_group_deleted(self, group_id:'int') -> 'None':

        # A list of all the Basic Auth usernames we are going to delete
        basic_auth_list:'strlist' = []

        # A list of all the API key header values we are going to delete
        apikey_list:'strlist' = []

        with self._lock:

            # Continue only if this group has been previously assigned to our context object ..
            if not group_id in self.security_groups:
                return

            # If we are here, it means that we really have a group to delete

            # Find all member IDs related to this group
            # member_id_list =

            # Find all security IDs related to this group
            sec_id_list = self.group_to_sec_map.pop(group_id, [])

            # .. turn security IDs into their names (Basic Auth) ..
            for username, item in self.basic_auth_credentials.items():
                if item.security_id in sec_id_list:
                    basic_auth_list.append(username)

            # .. turn security IDs into their header values (API keys) ..
            for header_value, item in self.apikey_credentials.items():
                if item.security_id in sec_id_list:
                    apikey_list.append(header_value)

            # .. remove security definitions (Basic Auth) ..
            for item in basic_auth_list:
                _ = self.basic_auth_credentials.pop(item)

            # .. remove security definitions (API keys) ..
            for item in apikey_list:
                _ = self.apikey_credentials.pop(item)

            # .. remove member IDs too ..

            # .. and remove the group itself.
            pass

# ################################################################################################################################

    def on_member_added_to_group(self):
        pass

    def on_member_removed_from_group(self):
        pass

# ################################################################################################################################

    def on_group_assigned_to_channel(self):
        pass

    def on_group_unassigned_from_channel(self):
        pass

# ################################################################################################################################
# ################################################################################################################################

class SecurityGroupCtxBuilder:

    def __init__(self, server:'ParallelServer') -> 'None':
        self.server = server
        self.members = self.server.groups_manager.get_member_list(Groups.Type.API_Clients)

# ################################################################################################################################

    def _get_members_by_group_id(self, group_id:'int') -> 'list_[Member]':

        # Our response to produce
        out:'list_[Member]' = []

        # Go through each of the members that we are aware of ..
        for item in self.members:

            # .. check if the member belongs to our input group ..
            if item.group_id == group_id:

                # .. if yes, add it to our output ..
                out.append(item)

        # .. finally, we can return the response to our caller.
        return out

# ################################################################################################################################

    def build_ctx(self, channel_id:'int', security_groups: 'intlist') -> 'SecurityGroupCtx':

        # Build a basic object ..
        ctx = SecurityGroupCtx()

        # .. populate it with the core data ..
        ctx.channel_id = channel_id

        # .. add all the credentials ..
        for group_id in security_groups:

            # .. first, add an indication that we use this group,
            # .. no matter what members are in it ..
            # ctx.add_security_group(group_id)

            # .. next, extract all the members from this group ..
            members = self._get_members_by_group_id(group_id)

            # .. now, go through each of the members found ..
            for member in members:

                # .. and add it to a container corresponding to its security type ..
                if member.sec_type == Sec_Def_Type.BASIC_AUTH:

                    # .. get the member's security definition ..
                    sec_def = self.server.worker_store.basic_auth_get_by_id(member.security_id)

                    # .. populate the correct container ..
                    ctx.on_basic_auth_created(
                        group_id,
                        member.id,
                        sec_def['id'],
                        sec_def['username'],
                        sec_def['password'],
                    )

                elif member.sec_type == Sec_Def_Type.APIKEY:

                    # .. get the member's security definition ..
                    sec_def = self.server.worker_store.apikey_get_by_id(member.security_id)

                    # .. populate the correct container ..
                    ctx.on_apikey_created(
                        sec_def['id'],
                        sec_def['password'],
                    )

                # .. add an indication that this channel has such a member,
                # .. along with information which security ID it is.
                ctx.add_member(member.id, member.security_id)

        # .. and return the business object to our caller.
        return ctx

# ################################################################################################################################
# ################################################################################################################################

class BuildCtx(Service):
    name = 'dev.groups.build-ctx'

    def handle(self):

        channel_id = 85
        security_groups = [1, 3]

        builder = SecurityGroupCtxBuilder(self.server)
        ctx = builder.build_ctx(channel_id, security_groups)

        cid = 'cid.1'
        channel_name = 'channel.1'

        result = ctx.check_security_basic_auth(cid, channel_name, 'user1', 'pass1')
        print('QQQ-1', result)

        result = ctx.check_security_apikey(cid, channel_name, 'key1')
        print('QQQ-2', result)

        ctx.on_group_deleted(1)

# ################################################################################################################################
# ################################################################################################################################
'''
