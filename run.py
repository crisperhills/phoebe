#!/usr/bin/python
from circuits import BaseComponent, handler
from lib.commands import CommandExecutor
from lib.core import PlayerManager, MessageProcessor, ICHCAPI
from lib.events import do_join_room, do_shutdown
from os import getpid, path, remove
from requests import Session
from setproctitle import getproctitle, setproctitle
from socket import error as socket_error, socket, AF_UNIX, SOCK_DGRAM
from sys import exit as sys_exit
from yaml import safe_load as load_yaml
import logging

'''phoebe'''
PROCTITLE = 'phoebe'
VERSION = 'v2.3.2'

logging.basicConfig(
    filename='phoebe.log',
    format=' '.join([
        '[%(asctime)s]',
        '[%(module)s.%(funcName)s]',
        '%(levelname)s:',
        '%(message)s'
    ]),
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.ERROR
)


class Phoebe(BaseComponent):

    def __init__(self, config, permissions, *args, **kwargs):
        super(Phoebe, self).__init__(args, kwargs)

        self.shm = {
            'config': config,
            'httpsession': Session(),
            'permissions': permissions,
            'state': dict(),
            'stats': dict()
        }

        self.cmdexec = CommandExecutor(
            self.shm, channel='cmdexec'
        ).register(self)
        self.msgproc = MessageProcessor(
            self.shm, channel='msgproc'
        ).register(self)
        self.playmgr = PlayerManager(
            self.shm, channel='playmgr'
        ).register(self)
        self.ichcapi = ICHCAPI(
            self.shm, channel='ichcapi'
        ).register(self)

        self.version = VERSION
        self.shm['config']['version'] = VERSION

    @handler('started')
    def _start_application(self, component):
        '''App startup routines.'''
        logging.critical(
            "application '{}' {} (PID: {}) started.".format(
                getproctitle(), self.version, getpid()
            ))

        self.fire(do_join_room(), self.ichcapi.channel)

    @handler('signal')
    def _handle_signal(self, event, signo, stack):
        self.fire(do_shutdown(), self.channel)

    @handler('do_shutdown')
    def shutdown(self):
        logging.critical('shutting down...')
        # stop player
        self.playmgr.in_shutdown = True
        self.playmgr.stop_player()

        # clean up socket
        socket_file = self.shm['config']['control_socket_file']
        if path.exists(socket_file):
            remove(socket_file)

        # unregister components
        self.cmdexec.unregister()
        self.msgproc.unregister()
        if self.ichcapi.http_poll_timer:
            self.ichcapi.http_poll_timer.unregister()
        self.ichcapi.unregister()
        self.playmgr.unregister()

        # exit
        self.stop()


# EXECUTION ############################################################

def main():
    # create pidfile
    with open('pidfile', 'w') as pidfile:
        print >>pidfile, getpid()

    # import config
    config = None
    with open('config.yaml', 'r') as config_file:
        config = load_yaml(config_file)
        logging.info('configuration file loaded and parsed.')

        if type(config) is not dict:
            logging.critical(
                'error: configuration file parsed into invalid type.'
            )
            sys_exit(1)

        if len(config) <= 0:
            logging.critical(
                'error: configuration file parsed into empty object.'
            )
            sys_exit(2)

    # craft process title from name (p-{name})
    process_title = 'p-{}'.format(config['name'])

    # set loglevel
    target_lvl = config['log_level']
    if hasattr(logging, target_lvl):
        logging.getLogger().setLevel(getattr(logging, target_lvl))

    # get lock to prevent concurrency
    global lock_socket
    lock_socket = socket(AF_UNIX, SOCK_DGRAM)
    try:
        lock_socket.bind('\0{}'.format(process_title))
        logging.info('got process lock')
    except socket_error:
        logging.critical('failed to get process lock; already running?')
        sys_exit(3)

    # import permissions
    with open('permissions.yaml', 'r') as permissions_file:
        permissions = load_yaml(permissions_file)
        logging.info('permissions file loaded and parsed.')

        if type(permissions) is not dict:
            logging.critical(
                'error: permissions file parsed into invalid type.'
            )
            sys_exit(4)

        if len(permissions) <= 0:
            logging.critical(
                'error: permissions file parsed into empty object.'
            )
            sys_exit(5)

    # set custom process title
    setproctitle(process_title)

    # create the Phoebe instance and start it
    runtime = Phoebe(config, permissions, channel=config['name'])
    runtime.run()


if __name__ == '__main__':
    main()
