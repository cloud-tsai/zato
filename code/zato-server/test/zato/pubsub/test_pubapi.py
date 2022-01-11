# -*- coding: utf-8 -*-

"""
Copyright (C) 2022, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
from time import sleep

# Zato
from zato.common import PUBSUB
from zato.common.pubsub import prefix_sk
from zato.common.test.rest_client import RESTClientTestCase

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from zato.common.typing_ import anydict

# ################################################################################################################################
# ################################################################################################################################

_default = PUBSUB.DEFAULT

sec_name   = _default.DEMO_SECDEF_NAME
username   = _default.DEMO_USERNAME
topic_name = '/zato/demo/sample'

class config:
    path_publish     = f'/zato/pubsub/topic/{topic_name}'
    path_receive     = f'/zato/pubsub/topic/{topic_name}'
    path_subscribe   = f'/zato/pubsub/subscribe/topic/{topic_name}'
    path_unsubscribe = f'/zato/pubsub/subscribe/topic/{topic_name}'

# ################################################################################################################################
# ################################################################################################################################

class PubAPITestCase(RESTClientTestCase):

    needs_current_app     = False
    payload_only_messages = False

# ################################################################################################################################

    def setUp(self) -> None:
        super().setUp()
        self.rest_client.init(username=username, sec_name=sec_name)

# ################################################################################################################################

    def _unsubscribe(self, sub_key:'str'='') -> 'anydict':
        response = self.rest_client.delete(
            config.path_unsubscribe,
            qs={'sub_key': sub_key}
        ) # type: anydict

        # We always expect an empty dict on reply from unsubscribe
        self.assertDictEqual(response, {})

        # Our caller may want to run its own assertion too
        return response

# ################################################################################################################################

    def test_self_subscribe(self):

        # Before subscribing, make sure we are not currently subscribed
        self._unsubscribe()

        response = self.rest_client.post(config.path_subscribe)

        # Wait a moment to make sure the subscription data is created
        sleep(0.1)

        sub_key       = response['sub_key']
        queue_depth = response['queue_depth']

        #
        # Validate sub_key
        #

        self.assertIsInstance(sub_key, str)
        self.assertTrue(sub_key.startswith(prefix_sk))

        len_sub_key = len(sub_key)
        len_prefix  = len(prefix_sk)

        self.assertTrue(len_sub_key >= len_prefix + 5) # We expect at least a few random characters here

        #
        # Validate queue_depth
        #

        self.assertIsInstance(queue_depth, int)

        # Clean up after the test
        self._unsubscribe(sub_key)

# ################################################################################################################################

    def xtest_self_unsubscribe(self):

        # Unsubscribe once ..
        response = self._unsubscribe()

        # .. we expect an empty dict on reply
        self.assertDictEqual(response, {})

        # .. unsubscribe once more - it is not an error to unsubscribe
        # .. even if we are already unsubscribed.
        response = self._unsubscribe()
        self.assertDictEqual(response, {})

# ################################################################################################################################
# ################################################################################################################################

if __name__ == '__main__':
    from unittest import main
    main()

# ################################################################################################################################
# ################################################################################################################################
