# -*- coding: utf-8 -*-

"""
Copyright (C) 2021, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
import os
import logging
import sys
from subprocess import CalledProcessError, PIPE, Popen, run as subprocess_run

# ################################################################################################################################
# ################################################################################################################################

log_format = '%(asctime)s - %(levelname)s - %(name)s:%(lineno)d - %(message)s'
logging.basicConfig(level=logging.DEBUG, format=log_format)

logger = logging.getLogger('zato')

# ################################################################################################################################
# ################################################################################################################################

class PostInstallProcess:
    """ Tasks run after the main installation process.
    """
    def __init__(self, base_dir, bin_dir):
        # type: (str) -> None
        self.base_dir = base_dir
        self.bin_dir = bin_dir
        self.pip_command = os.path.join(self.bin_dir, 'pip')

# ################################################################################################################################

    def run_command(self, command, exit_on_error=True):
        # type: (str) -> str

        logger.info('Running `%s`', command)

        # Turn what is possibly a multi-line command into a list of arguments ..
        command = command.strip()
        command = command.split()

        #process = Popen(command, stdout=PIPE, stderr=PIPE)

        process = Popen(command, stderr=PIPE)

        while True:

            #stdout = process.stdout.readline()
            stderr = process.stderr.readline()

            #print()
            #print(111, stdout)
            #print(222, stderr)
            #print()

            if 0:
                stdout = stdout.strip()
                stdout = stdout.decode('utf8')
                logger.info(stdout)

            if stderr:
                stderr = stderr.strip()
                stderr = stderr.decode('utf8')
                logger.warn(stderr)

                if exit_on_error:
                    process.kill()
                    sys.exit(1)

            if process.poll() is not None:
                break

# ################################################################################################################################

    def update_git_revision(self):

        # sh
        import sh

        # This is where we will store our last git commit ID
        revision_file_path = os.path.join(self.base_dir, 'release-info', 'revision.txt')

        #out = sh.pip('install', 'gevent')

        # Invoke git ..
        out = sh.git('log', '-n', '1', '--pretty=format:%H', '--no-color')

        # .. get our latest commit ID (we use repr because out.stdout may contain shell escape codes) ..
        commit_id = repr(out)

        # .. and store it in an external file for 'zato --version' and other tools to use.
        f = open(revision_file_path, 'w')
        f.write(commit_id)
        f.close()

# ################################################################################################################################

    def pip_install_core_pip(self):

        # Set up the command ..
        command = '{} install --no-warn-script-location -U setuptools pip'.format(self.pip_command)

        # .. and run it.
        self.run_command(command)

# ################################################################################################################################

    def pip_install_requirements(self):

        # Always use full paths to resolve any doubts
        reqs_path = os.path.join(self.base_dir, 'requirements.txt')

        # Set up the command ..
        command = """
            {} install
            --no-warn-script-location
            -r {}
        """.format(self.pip_command, reqs_path)

        # .. and run it.
        self.run_command(command)

# ################################################################################################################################

    def pip_install_zato_packages(self):

        # Note that zato-common must come first.
        packages = [
            'zato-common',
            'zato-agent',
            'zato-broker',
            'zato-cli',
            'zato-client',
            'zato-cy',
            'zato-distlock',
            'zato-hl7',
            'zato-lib',
            'zato-scheduler',
            'zato-server',
            'zato-web-admin',
            'zato-zmq',
            'zato-sso',
            'zato-testing',
        ]

        # All the -e arguments that pip will receive
        pip_args = []

        # Build the arguments
        for name in packages:
            package_path = os.path.join(self.base_dir, name)
            arg = '-e {}'.format(package_path)
            pip_args.append(arg)

        # Build the command ..
        command = '{} install {}'.format(self.pip_command, ' '.join(pip_args))

        # .. and run it.
        self.run_command(command)

# ################################################################################################################################

    def run(self):
        self.pip_install_core_pip()
        self.pip_install_requirements()
        self.pip_install_zato_packages()

        # self.update_git_revision()

# ################################################################################################################################
# ################################################################################################################################

if __name__ == '__main__':

    bin_dir = os.path.dirname(sys.executable)

    base_dir = os.path.join(bin_dir, '..')
    base_dir = os.path.abspath(base_dir)

    process = PostInstallProcess(base_dir, bin_dir)
    process.run()

# ################################################################################################################################
# ################################################################################################################################
