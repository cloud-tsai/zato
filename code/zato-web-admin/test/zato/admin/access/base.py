# -*- coding: utf-8 -*-

"""
Copyright (C) 2021, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
import json
import os
from unittest import TestCase

# Bunch
from bunch import bunchify

# Django
import django

# Selenium
from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions as conditions
from selenium.webdriver.support.ui import WebDriverWait

# Zato
from zato.admin.zato_settings import update_globals
from zato.common.util import new_cid

# ################################################################################################################################
# ################################################################################################################################

class Config:

    username_prefix = 'zato.unit-test.web-admin.'
    user_password = 'sJNlk8XOQs74E'
    user_email = 'test@example.com'

    web_admin_location = os.path.expanduser('~/env/web-admin.test/web-admin')
    web_admin_address  = 'http://localhost:8183'

# ################################################################################################################################
# ################################################################################################################################

class BaseTestCase(TestCase):

    # Whether we should automatically log in during setUp
    needs_auto_login = True

    # This can be set by each test separately
    run_in_background:'bool'

    # Selenium client
    client: 'webdriver.Firefox'

    def _set_up_django(self):

        import pymysql
        pymysql.install_as_MySQLdb()

        config_path = os.path.join(Config.web_admin_location, 'config', 'repo', 'web-admin.conf')
        config = open(config_path).read()
        config = json.loads(config)

        config['config_dir'] = Config.web_admin_location
        config['log_config'] = os.path.join(Config.web_admin_location, config['log_config'])

        update_globals(config, needs_crypto=False)

        os.environ['DJANGO_SETTINGS_MODULE'] = 'zato.admin.settings'
        django.setup()

# ################################################################################################################################

    def _set_up_django_auth(self):
        from zato.cli.web_admin_auth import CreateUser, UpdatePassword

        # User names are limited to 30 characters
        self.username = (Config.username_prefix + new_cid())[:30]

        create_args = bunchify({
            'path': Config.web_admin_location,
            'username': self.username,
            'password': Config.user_password, # This is ignored by CreateUser yet we need it so as not to prompt for it
            'email': Config.user_email,
            'verbose': True,
            'store_log': False,
            'store_config': False
        })

        command = CreateUser(create_args)
        command.is_interactive = False

        try:
            command.execute(create_args, needs_sys_exit=False)
        except Exception:
            # We need to ignore it as there is no specific exception to catch
            # while the underyling root cause is that the user already exists.
            pass

        update_password_args = bunchify({
            'path': Config.web_admin_location,
            'username': self.username,
            'password': Config.user_password,
            'verbose': True,
            'store_log': False,
            'store_config': False
        })

        command = UpdatePassword(update_password_args)
        command.execute(update_password_args)

# ################################################################################################################################

    def setUp(self):

        # Set up everything on Django end ..
        self._set_up_django()
        self._set_up_django_auth()

        # .. add a convenience alias for subclasses ..
        self.config = Config

        # .. log in if requested to.
        if self.needs_auto_login:
            self.login()

# ################################################################################################################################

    def login(self):

        run_in_background = getattr(self, 'run_in_background', None)
        run_in_background = True if run_in_background is None else run_in_background
        self.run_in_background = run_in_background

        # Custom options for the web client ..
        options = Options()

        if self.run_in_background:
            options.headless = True

        # .. set up our Selenium client ..
        self.client = webdriver.Firefox(options=options)
        self.client.get(self.config.web_admin_address)

        # .. get our form elements ..
        username = self.client.find_element_by_name('username')
        password = self.client.find_element_by_name('password')

        # .. fill out the form ..
        username.send_keys(self.username)
        password.send_keys(self.config.user_password)

        # .. and submit it.
        password.send_keys(Keys.RETURN)

        wait = WebDriverWait(self.client, 2)
        wait.until(conditions.title_contains('Hello'))

# ################################################################################################################################

    def tearDown(self):
        if self.run_in_background:
            self.client.quit()

        delattr(self, 'run_in_background')

# ################################################################################################################################
# ################################################################################################################################
