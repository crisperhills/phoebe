from . import commands, events
from .utils import PlayRequest, RequestTypes
from circuits import BaseComponent, handler, Timer
from collections import deque
from imp import load_source
from multiprocessing.connection import Client
from OpenSSL.SSL import Error as OpenSSLError, SysCallError
from os import path, remove
from re import compile as rexcomp
from requests import Response
from requests.exceptions import (
    ConnectionError, SSLError as ReqSSLError, Timeout as HTTPTimeout)
from subprocess import Popen
from time import sleep, time
from urlparse import urlparse
import logging


class ICHCAPI(BaseComponent):

    def __init__(self, shm, *args, **kwargs):
        super(ICHCAPI, self).__init__(args, kwargs)

        self.shm = shm
        self.config = self.shm['config']['ICHCAPI']

        self.roomname = self.config['room_to_join']
        self.url = self.config['entrypoint_url']
        self.shm['state']['ICHCAPI'] = {
            'last_action': '',
            'last_interval': self.config['polling_interval'],
            'last_query': 0,
            'last_receipt': 0,
            'empty_recvs': 0,
            'api_join_attempts': 0,
            'room_joined': False,
            'rejoining': False,
            'join_lock': False,
            'just_rejoined': False
        }
        self.shm['stats']['ICHCAPI'] = {
            'api_requests': 0,
            'messages_sent': 0
        }

        self.actionqueue = deque([])
        self.httpsession = self.shm['httpsession']
        self.http_poll_timer = None

        self.ctrlre = rexcomp(self.config['control_regex'])
        self.privmsgre = rexcomp(self.config['privmsg_regex'])
        self.selfmsgre = rexcomp(
            self.config['chat_prefix_regex'] +
            self.config['app_username']
        )

        self.in_shutdown = False

    # TIMER-DRIVEN EVENT HANDLERS ######################################

    # PROCESS NEXT ACTION FROM QUEUE #################################

    @handler('do_process_next_action')
    def _execute_action_from_queue(self):
        discard = False
        action = None

        if len(self.actionqueue):
            logging.debug('dequeuing next API action')
            # pop from queue
            action = self.actionqueue.popleft()
        elif self.shm['state']['ICHCAPI']['room_joined']:
            # no actions; simply receive any new message from the API
            action = ['recv']

        delay = self.config['polling_interval']

        if action:
            requeue = True
            # process only join requests when not joined
            query_type = action[0]
            if query_type == 'join':
                # ensure rejoin gets sent
                requeue = False
                if not self.shm['state']['ICHCAPI']['join_lock']:
                    self.shm['state']['ICHCAPI']['join_lock'] = True
                else:
                    # suppress superfluous join requests
                    discard = True
            elif self.shm['state']['ICHCAPI']['room_joined']:
                requeue = False

            if requeue:
                # put back actions we're not ready to execute
                self.actionqueue.appendleft(action)
            elif not discard:
                self.shm['state']['ICHCAPI']['last_action'] = action[0]
                self.shm['state']['ICHCAPI']['last_query'] = time()
                # send in line
                self._query_api_from_action(action)

                self.shm['stats']['ICHCAPI']['api_requests'] += 1

                # throttle if we recv'd last time and got no new messages
                if (
                    action[0] == 'recv' and
                    self.shm['state']['ICHCAPI']['last_action'] == 'recv' and
                    self.shm['state']['ICHCAPI']['empty_recvs'] > (int(
                        self.config['api_throttle_idle_timeout'] /
                        self.config['polling_interval']) - 1)
                ):
                    delay = self.shm['state']['ICHCAPI']['last_interval']
                    if (
                        delay <
                        self.config['max_polling_interval']
                    ):
                        delay += self.config['api_throttle_step']
                        self.shm['state']['ICHCAPI']['last_interval'] = delay
                        logging.debug(
                            "throttling API polling to {}s".format(delay))

        self.http_poll_timer = Timer(
            float(delay),
            events.do_process_next_action(),
            self.channel
        ).register(self)

    # NON-HANDLER METHODS ##############################################

    # JOIN ICHC API ###################################################
    def _send_join_request(self):
        response = self.httpsession.request(
            'GET',
            self.url,
            data='join',
            params={
                'v': 1,
                'u': self.config['app_username'],
                'p': self.config['api_key'],
                'a': 'join',
                'w': self.roomname
            },
            timeout=float(self.config['http_timeout'])
        )

        return response

    # SYNC WITH ICHC API ##############################################
    def _send_recv_request(self):
        response = self.httpsession.request(
            'GET',
            self.url,
            data='recv',
            params={
                'v': 1,
                'k': self.roomkey,
                'a': 'recv'
            },
            timeout=float(self.config['http_timeout'])
        )

        return response

    # SEND MESSAGE TO API #############################################
    def _send_message_request(self, message):
        logging.debug("sending message: '{}'".format(message))

        # query API with action send
        response = self.httpsession.request(
            'GET',
            self.url,
            data='send',
            params={
                'v': 1,
                'k': self.roomkey,
                'a': 'send',
                'w': message
            },
            timeout=float(self.config['http_timeout'])
        )

        self.shm['stats']['ICHCAPI']['messages_sent'] += 1

        return response

    # PROCESS ICHC API RESPONSE ######################################
    @handler('do_process_api_response')
    def _process_api_response_body(self, query_type, content):
        messages = list()
        error = None

        if query_type == 'join':
            self.shm['state']['ICHCAPI']['join_lock'] = False

        response_text = content.replace('\r', '').split('\n')

        for idx, line in enumerate(response_text):
            # all responses must start with OK
            if idx == 0:
                if line.strip() != 'OK':
                    error = 'non-OK response'
                    break
            # extract room key from (successful) join response
            elif (idx == 1) and (query_type == 'join'):
                self.shm['state']['ICHCAPI']['room_joined'] = True
                self.shm['state']['ICHCAPI']['api_join_attempts'] = 0
                self.roomkey = line.strip()
                self.fire(events.room_joined(), self.channel)
            # assume remaining lines are chat messages
            else:
                messages.append(line)

        # LEAK?
        # del response_text
        # del content

        if error:
            self.shm['state']['ICHCAPI']['room_joined'] = False
            logging.warning(
                'retrying join due to error in last API transaction: '
                '{}'.format(error)
            )

        filtered_messages = list()
        if not self.shm['state']['ICHCAPI']['room_joined']:
            reason = 'not joined'
            self._join_or_shutdown(reason)
        elif len(messages):
            self.shm['state']['ICHCAPI']['last_receipt'] = time()
            for line in messages:
                # filter out empty lines
                if len(line) == 0:
                    continue

                # filter out control messages (might use later)
                if self.ctrlre.search(line):
                    # keep PMs
                    privmsg_match = self.privmsgre.search(line)
                    if privmsg_match:
                        filtered_messages.append(
                            privmsg_match.group('message'))
                    # else:
                        # other control seqs: for now, do nothing; just skip
                    continue

                # filter out our own messages
                if self.selfmsgre.search(line):
                    continue

                # retain all other lines yet unfiltered
                filtered_messages.append(line)

            # process anything left over (ideally just chat messages)
            if len(filtered_messages):
                self.fire(
                    events.messages_received(filtered_messages),
                    self.parent.msgproc.channel
                )
                self.shm['state']['ICHCAPI']['empty_recvs'] = 0
                self.shm['state']['ICHCAPI']['last_interval'] = self.shm[
                    'config']['ICHCAPI']['polling_interval']
            else:
                self.shm['state']['ICHCAPI']['empty_recvs'] += 1

    # ON-DEMAND EVENT HANDLERS #########################################

    def _join_or_shutdown(self, reason):
        # join the room if we haven't already tried too many times
        max_attempts = self.config['api_rejoin_retry_count']
        api_join_attempts = self.shm['state']['ICHCAPI']['api_join_attempts']
        if api_join_attempts < max_attempts:
            remaining = max_attempts - api_join_attempts
            api_join_attempts += 1
            logging.info(
                '{}; attempting to join API ({} attempts remaining)'.format(
                    reason, remaining
                ))
            self.actionqueue.append(['join'])
        else:
            logging.critical('failed to rejoin API after {}'.format(reason))
            self.fire(events.do_shutdown(), self.parent.channel)

    # SEND MESSAGE WITH ICHC API #####################################
    @handler('do_send_message')
    def _send_message(self, message):
        logging.debug("sending message: '{}'".format(message))
        self.actionqueue.append(['send', message])
        self.shm['stats']['ICHCAPI']['messages_sent'] += 1

    # FSM EVENT HANDLERS ###############################################

    # STATE TRANSITION: *->JOINING ###################################
    @handler('do_join_room')
    def _join_room(self):
        logging.info("joining room '{}'".format(self.roomname))
        self.actionqueue.append(['join'])
        self.fire(events.do_process_next_action(), self.channel)

    # STATE TRANSITION: JOINING->ACTIVE ##############################
    @handler('room_joined')
    def _room_joined(self):
        logging.warning(
            "joined room '{}' successfully. starting receive polling".format(
                self.roomname
            ))
        # get mod; start broadcasting
        msg = [
            '/modme',
            '/broadcast',
            '/cam onx',
            '/cam audio-on'
        ]

        if not self.shm['state']['ICHCAPI']['just_rejoined']:
            msg.append(
                '/me (phoebe '
                '{}) ready &mdash; type **!help** to get started.'.format(
                    self.parent.version))
        else:
            msg.append('/me has rejoined after disconnect.')

        for line in msg:
            self.fire(events.do_send_message(line), self.channel)
        self.shm['state']['ICHCAPI']['just_rejoined'] = False

    # NON-HANDLER METHODS ##############################################

    # HTTPS SEND THREAD ##############################################
    def _query_api_from_action(self, action):
        if self.in_shutdown:
            return

        query_type = action[0]
        query_msg = None
        request_method = None
        response = None

        if query_type == 'join':
            request_method = getattr(self, '_send_join_request')
        elif query_type == 'recv':
            request_method = getattr(self, '_send_recv_request')
        elif query_type == 'send':
            request_method = getattr(self, '_send_message_request')
            query_msg = action[1]

        attempts_remaining = self.config['polling_retry_count']

        # keep trying until we succeed or run out of retries
        while True:
            try:
                if query_msg:
                    response = request_method(query_msg)
                else:
                    response = request_method()
            except (
                ConnectionError,
                HTTPTimeout,
                OpenSSLError,
                ReqSSLError,
                SysCallError
            ) as err:
                if attempts_remaining:
                    attempts_remaining -= 1
                    logging.warning(
                        'error sending HTTP(S) request '
                        '({}); retrying in {} seconds'.format(
                            str(err),
                            self.config['polling_retry_interval']
                        ))
                    sleep(self.config['polling_retry_interval'])
                    continue
                else:
                    logging.critical(
                        'failed to query API after reset ({})'.format(
                            str(err)))
                    self.fire(events.do_shutdown(), self.parent.channel)
                    break

            # log message if this is a retry
            if attempts_remaining < self.config['polling_retry_count']:
                attempts = self.config[
                    'polling_retry_count'] - attempts_remaining
                logging.warning(
                    'retry successful after {} attempts'.format(attempts))

            # exit retry loop if successful
            break

        if response:
            if isinstance(response, Response):
                # set encoding to prevent chardet invocation
                response.encoding = 'utf-8'

                # grab content, then close
                content = response.content
                # response.close()

                if content:
                    if len(content):
                        self.fire(
                            events.do_process_api_response(
                                query_type, content),
                            self.channel
                        )

                # LEAK?
                # del response

        return None


class MessageProcessor(BaseComponent):

    def __init__(self, shm, *args, **kwargs):
        super(MessageProcessor, self).__init__(args, kwargs)

        self.shm = shm
        self.shm['stats']['MessageProcessor'] = {
            'messages_received': 0,
            'commands_executed': 0
        }
        self.cmdre = rexcomp(
            self.shm['config']['MessageProcessor']['command_regex']
        )
        self.stridre = rexcomp(
            self.shm['config']['MessageProcessor']['stream_id_regex']
        )
        self.stream_id = None

    @handler('messages_received')
    def _parse_messages(self, messages):
        # parse lines for commands
        for line in messages:
            self.shm['stats']['MessageProcessor']['messages_received'] += 1
            cmd_match = self.cmdre.search(line)

            # if we don't yet have a stream_id, check for one
            if not self.stream_id:
                strid_match = self.stridre.search(line)
                if strid_match:
                    self.stream_id = strid_match.group('stream_id').strip()
                    self.fire(
                        events.broadcast_ready(
                            self.stream_id), self.parent.playmgr.channel)
            if cmd_match:
                self.fire(events.command_received(
                    *cmd_match.group('user', 'command')), self.channel)

    @handler('command_received')
    def _dispatch_command(self, raw_sender, raw_command):
        command_string = raw_command.strip()
        sender = raw_sender.strip().lower()
        logging.debug(
            "received command string '{}' from sender {}".format(
                command_string, sender
            ))

        command_parts = command_string.split(' ')
        command = command_parts[0].strip().lower()
        arguments = None
        if len(command_parts) > 1:
            arguments = ' '.join(command_parts[1:])

        c_name = 'c_{}'.format(command)
        if hasattr(commands, c_name):
            command_event = getattr(commands, c_name)
            self.fire(
                command_event(
                    sender, command, arguments), self.parent.cmdexec.channel)
            self.shm['stats']['MessageProcessor']['commands_executed'] += 1


class PlayerManager(BaseComponent):

    def __init__(self, shm, *args, **kwargs):
        super(PlayerManager, self).__init__(args, kwargs)

        self.shm = shm
        self.httpsession = self.shm['httpsession']

        # player, request queue, and states
        self.player_process = None
        self.player_client = None
        self.player_mode = None
        self.requestqueue = deque([])
        self.current_request = None
        self.stream_id = None

        self.in_shutdown = False

        # self._player_ready = False
        self._dequeue_lock = False

        # load the search filter module
        filter_module = None
        filter_name = self.shm['config']['PlayRequest']['search_filter']
        try:
            filter_module = load_source(
                filter_name, 'filters/{}.py'.format(filter_name))
        except:
            error = ' '.join([
                'error loading search filter',
                '\'filters/{}.py\''.format(filter_name)
            ])
            logging.warning(error)
        self.filter_module = filter_module

    # CONVENIENCE METHODS ##############################################

    def _media_playing(self):
        if self.player_mode is None:
            return False
        if self.player_mode != 'media':
            return False
        if not self.current_request:
            return False
        return True

    def _command_player(self, command):
        try:
            self.player_client.send(command)
        except IOError:
            # DEBUG THIS BRANCH -- SEEMS TO HANG; CHECK FOR DEFUNCTS
            logging.error(
                "IO error encountered when attempting to send to player "
                "connection")
            return None
        try:
            response = self.player_client.recv()
        except EOFError:
            logging.error(
                "EOF encountered when attempting to read from player "
                "connection")
            return None
        return response

    def player_active(self):
        socket_file = self.shm['config']['control_socket_file']
        if path.exists(socket_file):
            # check for zombie
            if self.player_process:
                if self.player_process.poll():
                    logging.warning(
                        'player socket active with exited player process')
                    if self.player_client:
                        self.player_client.close()
                    # del self.player_process
                    self.player_process.wait()
                    remove(socket_file)
                    return False
                return True
            else:
                logging.critical(
                    'player socket active with no tracked player process')
                self.fire(events.do_shutdown(), self.parent.channel)
        else:
            return False

    def stop_player(self):
        self.player_mode = None
        # try graceful stop with command
        if self.player_client:
            logging.info('sending stop command to player')
            try:
                self.player_client.send(['stop'])
            except IOError:
                # DEBUG THIS BRANCH -- SEEMS TO HANG; CHECK FOR DEFUNCTS
                logging.error(
                    "IO error encountered when attempting to send to player "
                    "connection")
            finally:
                # loop until socket file created
                timeout = self.shm['config']['PlayerManager'][
                    'player_state_change_timeout']
                wait = self.shm['config']['PlayerManager'][
                    'player_state_change_delay']
                socket_file = self.shm['config']['control_socket_file']
                while timeout > 0:
                    timeout -= 1
                    if not path.exists(socket_file):
                        break
                    sleep(wait)

                self.player_client.close()

    # HANDLER METHODS ##################################################

    @handler('broadcast_ready')
    def _start_queue_checks(self, stream_id):
        self.stream_id = stream_id
        logging.warning(
            "stream id "
            "'{}' received; checking request queue".format(self.stream_id))
        self.fire(events.do_check_request_queue(), self.channel)

    @handler('do_queue_play_request')
    def _queue_request(self, request):
        config = self.shm['config']['PlayRequest']

        request_type = request[0]
        request_sender = request[1]
        request_body = request[2]

        request = None

        if request_type == 'search':
            request = PlayRequest(
                config,
                self.httpsession,
                request_sender,
                filter_module=self.filter_module,
                search_terms=request_body
            )
        elif request_type == 'site':
            request = PlayRequest(
                config,
                self.httpsession,
                request_sender,
                request_uri=urlparse(request_body, scheme='http')
            )
        elif request_type == 'direct':
            request = PlayRequest(
                config,
                self.httpsession,
                request_sender,
                direct=True,
                request_uri=urlparse(request_body, scheme='http')
            )

        if request:

            request.prepare()

            if request.prepared:

                logging.info(
                    'queuing request: "{}" (page: {} | media: {})'.format(
                        request.title, request.request_uri, request.media_uri
                    ))
                self.requestqueue.append(request)

                dur_string = '~'
                if request.live_source:
                    dur_string = 'LIVE'
                elif request.duration > 0:
                    dur_string = '{:d}:{:02d}'.format(
                        *self.get_min_sec(request.duration))

                if self.player_mode == 'media':
                    msg = '/msg {} "{}" (from {}) &mdash; '.format(
                        request.sender,
                        request.title,
                        request.source_site
                    ) + '{} &mdash; added to queue (#{}).'.format(
                        dur_string, len(self.requestqueue))
                    self.fire(events.do_send_message(msg),
                              self.parent.ichcapi.channel)
            else:
                msg = "/msg {} couldn't queue your request &mdash; {}".format(
                    request_sender, request.error)
                self.fire(events.do_send_message(msg),
                          self.parent.ichcapi.channel)

    @handler('do_check_request_queue')
    def _check_request_queue(self):
        if not self.in_shutdown:

            self._dequeue_lock = True

            start_playback = False

            if self.player_process:
                if not self.player_active():
                    self.player_mode = None
                    # player exited; make new player process, at least for idle
                    start_playback = True
                elif self.player_mode != 'media' and len(self.requestqueue):
                    # player in idle mode; requests queued -- stop current
                    # player
                    self.stop_player()
                    start_playback = True
            else:
                # no player loaded; make new player process, at least for idle
                start_playback = True

            playback_error = None

            if start_playback:
                address = self.shm['config']['control_socket_file']
                if not len(self.requestqueue):
                    # nothing queued to play; idle
                    logging.info('request queue empty; idling')

                    self.player_mode = 'idle'
                    self.player_process = Popen([
                        '/usr/bin/python', 'bin/play.py', self.stream_id
                    ])
                else:
                    # pop next request from queue
                    logging.info('dequeing and playing next request')
                    self.current_request = self.requestqueue.popleft()

                    # refresh info in case our media url went stale
                    if self.current_request.request_type == RequestTypes.SITE:
                        age = time() - self.current_request.last_fetched
                        if age > self.shm['config']['PlayerManager'][
                                'site_media_info_max_age']:
                            logging.warning('media info stale; updating')
                            self.current_request.update_site_media_info()

                    # send error message if request prep failed, otherwise play
                    if self.current_request.error:
                        logging.warning('request failed to update')
                        playback_error = True
                    else:

                        dur_string = '~'
                        if self.current_request.live_source:
                            dur_string = 'LIVE'
                        elif self.current_request.duration > 0:
                            dur_string = '{:d}:{:02d}'.format(
                                *self.get_min_sec(
                                    self.current_request.duration))

                        msg = '/me is now playing * **{}** (from '.format(
                            self.current_request.title
                        ) + '**{}**)* &mdash; {} &mdash; *for {}*'.format(
                            self.current_request.source_site,
                            dur_string, self.current_request.sender)
                        self.fire(events.do_send_message(msg),
                                  self.parent.ichcapi.channel)

                        self.player_mode = 'media'
                        self.player_process = Popen([
                            '/usr/bin/python',
                            'bin/play.py',
                            self.stream_id,
                            self.current_request.media_uri
                        ])

                if self.player_process:
                    # loop until socket file created
                    timeout = self.shm['config']['PlayerManager'][
                        'player_state_change_timeout']
                    wait = self.shm['config']['PlayerManager'][
                        'player_state_change_delay']
                    while timeout > 0:
                        timeout -= 1
                        if path.exists(address):
                            break
                        sleep(wait)

                    if timeout == 0:
                        # see if player simply failed to start
                        if not self.player_process.poll():
                            logging.critical(
                                'timed out waiting for player socket and '
                                'player still alive')
                            self.fire(events.do_shutdown(),
                                      self.parent.channel)
                        else:
                            logging.error('player failed to start')
                            playback_error = True
                    else:
                        # send play command to new player process, via client
                        self.player_client = Client(address, authkey='phoebe')

                        try:
                            self.player_client.send(['play'])
                        except IOError:
                            logging.error(
                                "IO error encountered when attempting to send "
                                "to player connection")
                            logging.critical(
                                'failed to issue play command to player '
                                'process')
                            self.fire(events.do_shutdown(),
                                      self.parent.channel)

                if playback_error:
                    msg = "/msg {} error trying to play ".format(
                        self.current_request.sender
                    ) + "your request &mdash; {}".format(
                        self.current_request.error)
                    self.fire(
                        events.do_send_message(msg),
                        self.parent.ichcapi.channel
                    )

            self._dequeue_lock = False

            Timer(
                float(self.shm['config']['PlayerManager']
                      ['queue_check_interval']),
                events.do_check_request_queue(),
                self.channel
            ).register(self)

    @handler('do_change_vote')
    def _change_vote(self, sender, change):
        if not self._media_playing():
            return None

        verb = ''
        if change > 0:
            if self.current_request.upvote(sender):
                verb = 'increased'
        elif change < 0:
            if self.current_request.downvote(sender):
                verb = 'decreased'
        if len(verb):
            adjective = ''
            if self.current_request.rating > 0:
                adjective = '+'

            a = '/me {} rating of * **{}** (from **{}**)*'.format(
                verb,
                self.current_request.title,
                self.current_request.source_site
            )
            b = '&mdash; to **{}{}** &mdash; *for {}*'.format(
                adjective, self.current_request.rating, sender)
            msg = '{} {}'.format(a, b)
            self.fire(events.do_send_message(msg), self.parent.ichcapi.channel)

        min_rating = self.shm['config']['PlayerManager']['min_request_rating']
        if self.current_request.rating < min_rating:
            logging.info(
                'stopping playback for low rating: "{}" ({})'.format(
                    self.current_request.title,
                    self.current_request.request_uri
                ))
            msg = '/me stopped player &mdash; item voted out.'
            self.fire(events.do_send_message(msg), self.parent.ichcapi.channel)

            self.stop_player()

    @handler('do_seek_current_media')
    def _seek_current_media(self, sender, seek_secs, is_elevated):
        if not self._media_playing():
            return None
        if not self.player_client:
            return None
        if self.current_request.live_source:
            return None
        if sender != self.current_request.sender and not is_elevated:
            return None

        # send seek command, interval to player
        response = self._command_player(['seek', seek_secs])

        if not response:
            return None
        if response[0] != 'OK':
            logging.warning(
                'seek failed: {} seconds (from {})'.format(seek_secs, sender))

    @handler('do_jump_current_media')
    def _jump_current_media(self, sender, jump_secs, is_elevated):
        if not self._media_playing():
            return None
        if not self.player_client:
            return None
        if self.current_request.live_source:
            return None
        if sender != self.current_request.sender and not is_elevated:
            return None

        # send jump command, target to player
        response = self._command_player(['jump', jump_secs])

        if not response:
            return None
        if response[0] != 'OK':
            logging.warning(
                'jump failed: {} seconds (from {})'.format(jump_secs, sender))

    @handler('do_stop_current_media')
    def _stop_current_media(self, sender, is_elevated):
        if not self._media_playing():
            return None
        if not self.player_client:
            return None
        if sender != self.current_request.sender and not is_elevated:
            return None

        verb = 'halting' if is_elevated else 'stopping'

        logging.info(
            '{} media: "{}" ({})'.format(
                verb,
                self.current_request.title,
                self.current_request.request_uri
            ))
        self.stop_player()

    @handler('do_get_current_info')
    def _get_current_info(self, sender):
        if not self._media_playing():
            return None
        if not self.player_client:
            return None

        timestamp = '~'
        if self.current_request.live_source:
            # get position from player
            response = self._command_player(['getlivepos'])

            if response:
                if response[0] == 'OK':
                    cur_time = response[1]
                    pos_string = '{:d}:{:02d}'.format(
                        *self.get_min_sec(cur_time))
                    timestamp = "LIVE for {}".format(pos_string)
                else:
                    logging.error(response[1])
        else:
            # get position, duration from player
            response = self._command_player(['getpos'])

            if response:
                if response[0] == 'OK':
                    cur_time = response[1]
                    pos_string = '{:d}:{:02d}'.format(
                        *self.get_min_sec(cur_time[0]))
                    dur_string = '{:d}:{:02d}'.format(
                        *self.get_min_sec(cur_time[1]))
                    timestamp = "{}/{}".format(pos_string, dur_string)
                else:
                    logging.error(response[1])

        msg = list()
        part_a = '/me is playing * **{}** (from **{}**)*'.format(
            self.current_request.title, self.current_request.source_site)
        part_b = '&mdash; {} &mdash; rated **{}** &mdash; *for {}*'.format(
            timestamp,
            self.current_request.rating,
            self.current_request.sender
        )
        msg.append('{} {}'.format(part_a, part_b))
        msg.append(
            '/me also has a * **direct link** &mdash;* {}'.format(
                self.current_request.request_uri))
        for m in msg:
            self.fire(events.do_send_message(m), self.parent.ichcapi.channel)

    @handler('do_get_queue_info')
    def _get_queue_info(self, sender):
        if not len(self.requestqueue):
            msg = '/me has no items queued.'
            self.fire(events.do_send_message(msg), self.parent.ichcapi.channel)
            return None

        items = list()
        for idx, request in enumerate(self.requestqueue):
            items.append('**{}.** *{}* &mdash; for {}'.format(
                idx + 1, request.title, request.sender))

        msg = '/me has queued: {}'.format(', '.join(items))
        self.fire(events.do_send_message(msg), self.parent.ichcapi.channel)

    @handler('do_drop_queue_item')
    def _drop_queue_item(self, sender, arguments):
        if not len(self.requestqueue):
            return False
        item_number = arguments
        if not item_number:
            # get last queued from sender
            for idx, item in enumerate(reversed(self.requestqueue)):
                if item.sender == sender:
                    item_number = idx + 1
                    break
            if not item_number:
                return False
        if item_number > len(self.requestqueue):
            return False
        item_idx = item_number - 1
        # check for ownership
        if self.requestqueue[item_idx].sender != sender:
            return False
        dropped_title = self.requestqueue[item_idx].title
        del self.requestqueue[item_idx]

        msg = '/me has dropped from the queue: {}. &mdash; *{}*'.format(
            item_number, dropped_title)
        self.fire(events.do_send_message(msg), self.parent.ichcapi.channel)

    # MISC UTILITY METHODS
    @staticmethod
    def get_min_sec(time):
        minutes = 0
        seconds = 0
        while time > 0:
            if time >= 60:
                minutes += 1
                time -= 60
            else:
                seconds = time
                time = 0
        return (minutes, seconds)
