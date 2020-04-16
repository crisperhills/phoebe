from . import events
from circuits import BaseComponent, Event, handler
from re import match, search


class c_hello(Event):
    help_text = '**!hello** &mdash; generate a simple test message'
    restricted = True


class c_stats(Event):
    help_text = '**!stats** &mdash; display various runtime statistics'
    restricted = True


class c_say(Event):
    help_text = "**!say** &mdash; 'twas brillig, and the slithy toves did "
    "gyre and gimble in the wabe"
    restricted = True


class c_commands(Event):
    help_text = '**!commands** &mdash; list all unprivileged commands'


class c_direct(Event):
    help_text = '**!direct** *url* &mdash; play media files by URL (e.g. .mp4)'


class c_help(Event):
    help_text = ' '.join([
        '**!help** *[command]* &mdash; display help',
        '[optionally about a specific command]'
    ])


class c_play(Event):
    help_text = ' '.join([
        '**!play** *url* or *search terms* &mdash;',
        'play video/audio from supported sites'
    ])


class c_stop(Event):
    help_text = '**!stop** &mdash; stop playback of media you\'ve queued'


class c_jump(Event):
    help_text = ' '.join([
        '**!jump [hh:][mm:]ss** (go ahead and) jump to a specific',
        'time in currently playing media you\'ve queued'
    ])


class c_ff(Event):
    help_text = ' '.join([
        '**!ff** *[secs]* &mdash; fast-forward 10 (or *[secs]*)',
        'seconds in currently playing media you\'ve queued'
    ])


class c_rew(Event):
    help_text = ' '.join([
        '**!rew** *[secs]* &mdash; rewind 30 (or *[secs]*)',
        'seconds in currently playing media you\'ve queued'
    ])


class c_now(Event):
    help_text = '**!now** &mdash; show details about what\'s playing right now'


class c_next(Event):
    help_text = '**!next** &mdash; list media coming up next'


class c_drop(Event):
    help_text = ' '.join([
        '**!drop** *[item number]* &mdash; drop from the queue',
        'the most recent (or specified) item you\'ve added'
    ])


class c_sites(Event):
    help_text = '**!sites** &mdash; list sites from which the bot plays media'


class c_yea(Event):
    help_text = '**!yea** &mdash; increase the rating of what\'s playing'


class c_nay(Event):
    help_text = '**!nay** &mdash; decrease the rating of what\'s playing'


class CommandExecutor(BaseComponent):

    def __init__(self, shm, *args, **kwargs):
        super(CommandExecutor, self).__init__(args, kwargs)

        self.shm = shm

    def _get_allowed_commands(self, sender):
        permissions = self.shm['permissions']
        permitted = list()
        if sender in permissions['users'].keys():
            # add explicitly-granted permissions
            for member_group in permissions['users'][sender]['groups']:
                permitted += permissions['groups'][member_group]
            # add control commands
            permitted += ['stop', 'jump', 'ff', 'rew']

        return permitted

    def _allowed(self, sender, command):
        if command in self._get_allowed_commands(sender):
            return True

        return False

    # COMMAND HANDLERS #################################################

    @handler('c_commands')
    def _cmd_commands(self, sender, command, arguments):
        c_events = dict()
        for c_name, obj in globals().iteritems():
            if c_name[:2].lower() == 'c_':
                c_events[c_name] = obj

        outlines = list()

        help_commands = list()
        for c_name, obj in c_events.iteritems():
            name = c_name[2:]
            if hasattr(obj, 'restricted'):
                help_commands.append('{}*'.format(name))
            else:
                help_commands.append(name)

        if len(help_commands):
            help_commands.sort()

            quoted_commands = [
                "**!{}**".format(cmd_name) for cmd_name in help_commands]
            command_string = ', '.join(
                quoted_commands[:-1]
            ) + ', and {}'.format(quoted_commands[-1])
            outlines = [
                ' '.join([
                    '/me responds to the commands {} &mdash;'.format(
                        command_string),
                    '* **&#42;**access-controlled commands*'
                ]),
                ' '.join([
                    '/me can tell you how any one of them works with',
                    '**!help** followed by a command name (e.g., **!help',
                    'play**).'
                ])
            ]

        if len(outlines):
            for line in outlines:
                self.fire(
                    events.do_send_message(line),
                    self.parent.ichcapi.channel
                )

    @handler('c_sites')
    def _cmd_sites(self, sender, command, arguments):
        msg = ' '.join([
            '/me can fetch media from most sites listed here:',
            'http://bit.ly/2d9yknp'
        ])
        self.fire(
            events.do_send_message(msg),
            self.parent.ichcapi.channel
        )

    @handler('c_help')
    def _cmd_help(self, sender, command, arguments):
        c_events = dict()
        for c_name, obj in globals().iteritems():
            if c_name[:2].lower() == 'c_':
                c_events[c_name] = obj

        outlines = list()
        if not arguments:
            outlines = [
                ' '.join([
                    '/me plays videos ( **!play',
                    'http://video.site/whatever **), or searches for a',
                    'random video ( **!play search terms** ).'
                ]),
                ' '.join([
                    '/me stops ( **!stop** ), jumps forward and back (',
                    '**!ff seconds** and **!rew seconds** ), or gives',
                    'details ( **!now** ).'
                ]),
                ' '.join([
                    '/me takes votes ( **!yea** and **!nay** ), and',
                    'lists all commands ( **!commands** ).'
                ])
            ]

        else:
            event_name = 'c_{}'.format(arguments.replace('!', ''))
            if event_name in c_events.keys():
                outlines.append(c_events[event_name].help_text)

        if len(outlines):
            for line in outlines:
                self.fire(
                    events.do_send_message(line),
                    self.parent.ichcapi.channel
                )
        # if len(pm_outlines):
        #   self.fire(events.do_send_message(
        #     '/msg {} {}'.format(
        #       sender,
        #       ' &mdash; '.join(pm_outlines))), self.parent.ichcapi.channel)

    @handler('c_hello')
    def _cmd_hello(self, sender, command, arguments):
        if self._allowed(sender, 'hello'):
            self.fire(
                events.do_send_message(
                    "Hello, {}.".format(sender)
                ),
                self.parent.ichcapi.channel
            )

    @handler('c_yea')
    def _cmd_yea(self, sender, command, arguments):
        self.fire(
            events.do_change_vote(sender, 1),
            self.parent.playmgr.channel
        )

    @handler('c_nay')
    def _cmd_nay(self, sender, command, arguments):
        self.fire(
            events.do_change_vote(sender, -1),
            self.parent.playmgr.channel
        )

    @handler('c_play')
    def _cmd_play(self, sender, command, arguments):
        if arguments:
            if len(arguments):
                request = None
                # check http(s) or .tld/
                if not (
                        search('^https?', arguments) or search(
                            '\.[a-zA-Z]{1,3}/', arguments)
                ):
                    # assume search terms; query PH
                    request = ['search', sender, arguments]
                else:
                    # hack: assume spaces were '%20' entities we lost
                    url = arguments.replace(' ', '%20')
                    request = ['site', sender, url]

                self.fire(
                    events.do_queue_play_request(request),
                    self.parent.playmgr.channel
                )

    @handler('c_direct')
    def _cmd_direct(self, sender, command, arguments):
        if arguments:
            if len(arguments):
                if (
                        search('^https?', arguments) or search(
                            '\.[a-zA-Z]{1,3}/', arguments)
                ):
                    # generate direct-play request
                    url = arguments.replace(' ', '%20')

                self.fire(
                    events.do_queue_play_request(
                        ['direct', sender, url]
                    ),
                    self.parent.playmgr.channel
                )

    @handler('c_now')
    def _cmd_now(self, sender, command, arguments):
        self.fire(
            events.do_get_current_info(sender),
            self.parent.playmgr.channel
        )

    @handler('c_next')
    def _cmd_queue(self, sender, command, arguments):
        self.fire(
            events.do_get_queue_info(sender),
            self.parent.playmgr.channel
        )

    @handler('c_drop')
    def _cmd_drop(self, sender, command, arguments):
        item_number = None
        if arguments:
            if len(arguments):
                try:
                    item_number = int(arguments)
                except ValueError:
                    return False
                if item_number < 1:
                    return False

        is_elevated = self._allowed(sender, 'drop')

        self.fire(
            events.do_drop_queue_item(sender, is_elevated, item_number),
            self.parent.playmgr.channel
        )

    @handler('c_stop')
    def _cmd_stop(self, sender, command, arguments):

        is_elevated = self._allowed(sender, 'stop')

        self.fire(
            events.do_stop_current_media(sender, is_elevated),
            self.parent.playmgr.channel
        )

    @handler('c_jump')
    def _cmd_jump(self, sender, command, arguments):
        if not arguments:
            return None
        if not len(arguments):
            return None
        time = arguments.strip()

        jump_match = match('^:?\d{1,2}(?::\d{1,2}){0,2}$', time)
        if not jump_match:
            return None

        split_time = time.strip(':').split(':')
        split_time.reverse()

        if len(split_time) < 1 or len(split_time) > 3:
            return None

        jump_secs = 0
        for idx, value in enumerate(split_time):
            if idx == 0:
                # seconds
                jump_secs += int(value)
            elif idx == 1:
                # minutes
                jump_secs += int(value) * 60
            elif idx == 2:
                # hours
                jump_secs += int(value) * 360

        is_elevated = self._allowed(sender, 'jump')

        self.fire(
            events.do_jump_current_media(
                sender, jump_secs, is_elevated),
            self.parent.playmgr.channel
        )

    @handler('c_ff')
    def _cmd_ff(self, sender, command, arguments):
        seek_secs = 10
        if arguments:
            if not match('^\d+$', arguments):
                return None
            seek_secs = int(arguments.split(' ')[0])
        if seek_secs <= 0:
            return None

        is_elevated = self._allowed(sender, 'ff')

        self.fire(
            events.do_seek_current_media(
                sender, seek_secs, is_elevated),
            self.parent.playmgr.channel
        )

    @handler('c_rew')
    def _cmd_rew(self, sender, command, arguments):
        seek_secs = 30
        if arguments:
            if not match('^\d+$', arguments):
                return None
            seek_secs = int(arguments.split(' ')[0])
        if seek_secs <= 0:
            return None

        is_elevated = self._allowed(sender, 'rew')

        self.fire(
            events.do_seek_current_media(
                sender, seek_secs * -1, is_elevated),
            self.parent.playmgr.channel
        )

    @handler('c_stats')
    def _cmd_stats(self, sender, command, arguments):
        if self._allowed(sender, 'stats'):
            pretty_stats = list()
            for component, stats in self.shm['stats'].iteritems():
                for stat, value in stats.iteritems():
                    pretty_stats.append(
                        '**{}**: {}'.format(
                            stat.replace('_', ' '),
                            value
                        )
                    )
            msg = '/me is tracking {}, and {}'.format(
                ', '.join(pretty_stats[:-1]),
                pretty_stats[-1]
            )
            self.fire(
                events.do_send_message(msg),
                self.parent.ichcapi.channel
            )

    @handler('c_say')
    def _cmd_say(self, sender, command, arguments):
        if self._allowed(sender, 'say'):
            if len(arguments):
                self.fire(
                    events.do_send_message(arguments),
                    self.parent.ichcapi.channel
                )
