#!/usr/bin/python3
from __future__ import division
from __future__ import absolute_import
from __future__ import print_function
from multiprocessing import Value
from multiprocessing.connection import Listener
from os import getpid
from setproctitle import setproctitle
from signal import signal, SIGABRT, SIGINT, SIGHUP, SIGQUIT, SIGTERM
from socket import error as socket_error, socket, AF_UNIX, SOCK_DGRAM
from sys import argv, exit as sys_exit
from threading import Thread
from yaml import safe_load as load_yaml
import logging
import gi
from io import open

'''phoebe-player'''

# # uncomment when debugging gst
# from os import putenv
# putenv('GST_DEBUG', '3')
# putenv('GST_PLUGIN_PATH', '/usr/lib/x86_64-linux-gnu/gstreamer-1.0:/usr/local/lib/gstreamer-1.0')

gi.require_version('Gst', '1.0')
from gi.repository import GObject, GLib, Gst
Gst.init(None)

logging.basicConfig(
    filename='play.log',
    format='[%(asctime)s] [%(funcName)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.ERROR
)


class Idler(Thread):

    def __init__(self, config, state, stream_id):
        super(Idler, self).__init__()

        # self._config = config
        self._state = state

        self._mainloop = GLib.MainLoop()
        self._pipeline = Gst.Pipeline()

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message::application', self._on_application)
        bus.connect('message::segment-done', self._on_segment_done)

        self.src = Gst.ElementFactory.make('filesrc', None)
        self.demux = Gst.ElementFactory.make('qtdemux', None)
        self.parse = Gst.ElementFactory.make('h264parse', None)
        self.mux = Gst.ElementFactory.make('flvmux', None)
        self.sink = Gst.ElementFactory.make('rtmpsink', None)

        self.src.set_property('location', 'idlebg.mp4')
        self.mux.set_property('streamable', 'true')
        self.sink.set_property('location',
                               '/'.join([config['output_rtmp_baseurl'], stream_id]))

        self._pipeline.add(self.src)
        self._pipeline.add(self.demux)
        self._pipeline.add(self.parse)
        self._pipeline.add(self.mux)
        self._pipeline.add(self.sink)

        # dynamically link demux to parse when video pad is added
        self.demux.connect("pad-added", self._on_pad_added)

        self.src.link(self.demux)
        self.parse.link(self.mux)
        self.mux.link(self.sink)

    def _play(self):
        if self._pipeline.current_state == Gst.State.PLAYING:
            return

        logging.warning('idling')
        self._pipeline.set_state(Gst.State.PLAYING)
        self._pipeline.get_state(Gst.CLOCK_TIME_NONE)
        logging.info('performing initial seek')
        self._rewind()

    def _rewind(self):
        # issue seek to restart playback
        logging.debug('performing rewind seek')
        self._pipeline.seek(
            1.0,
            Gst.Format.TIME,
            Gst.SeekFlags.SEGMENT,
            Gst.SeekType.SET, 0,
            Gst.SeekType.NONE, 0
        )

    def _postroll(self):
        self._pipeline.set_state(Gst.State.NULL)
        self._mainloop.quit()

    def _stop(self):
        if self._pipeline.current_state == Gst.State.NULL:
            return

        logging.debug('deidling')
        self._postroll()

    # BUS MESSAGE HANDLERS

    def _on_application(self, bus, msg):
        # pointer; don't free
        structure = msg.get_structure()

        if structure.has_name('GstLaunchInterrupt'):
            logging.warning('caught interrupt; stopping pipeline')
            self.kill()

    def _on_segment_done(self, bus, msg):
        self._rewind()

    # SIGNAL HANDLERS

    def _on_pad_added(self, element, pad):
        string = pad.query_caps(None).to_string()
        logging.debug('pad added: {}'.format(string))
        if pad.name == 'video_0':
            pad.link(self.parse.get_static_pad('sink'))

    # PUBLIC METHODS

    def run(self):
        if self._mainloop.is_running():
            logging.warning(
                'run requested when mainloop already running')
            return None

        # 0: init   1: started    2: stopped
        self._state.value = 1
        logging.debug('starting mainloop')
        try:
            self._mainloop.run()
        except:
            self.stop()
        finally:
            logging.debug('end of thread run method reached')
            self._state.value = 2

    def play(self):
        self._play()

    def stop(self):
        self._stop()

    def is_running(self):
        return self._mainloop.is_running()

    def is_playing(self):
        if (self._pipeline.current_state == Gst.State.PAUSED or
                self._pipeline.current_state == Gst.State.PLAYING):
            return True
        else:
            return False


class Player(Thread):

    def __init__(
        self,
        config,
        state,
        stream_id,
        media_uri,
        live_source=False
    ):
        super(Player, self).__init__()

        # params

        self._config = config
        self._state = state
        self._live_source = live_source

        # mainloop and pipeline

        self._mainloop = GLib.MainLoop()
        self._pipeline = Gst.Pipeline('playerpipeline')

        # bus

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message::buffering', self._on_buffering)
        bus.connect('message::eos', self._on_eos)
        bus.connect('message::error', self._on_error)
        bus.connect('message::warning', self._on_warning)
        bus.connect('message::clock-lost', self._on_clock_lost)
        bus.connect('message::latency', self._on_latency)
        bus.connect('message::request-state', self._on_request_state)
        bus.connect('message::application', self._on_application)

        # makes

        self._decodebin = Gst.ElementFactory.make(
            'uridecodebin', 'playerdecodebin')

        self._video_raw_queue = Gst.ElementFactory.make(
            'queue', 'rawvideoqueue')
        self._video_rate = Gst.ElementFactory.make('videorate', None)
        self._video_scale = Gst.ElementFactory.make('videoscale', None)
        self._video_convert = Gst.ElementFactory.make('videoconvert', None)
        self._video_enc = Gst.ElementFactory.make('x264enc', None)
        self._video_parse = Gst.ElementFactory.make('h264parse', None)

        self._audio_raw_queue = Gst.ElementFactory.make(
            'queue', 'rawaudioqueue')
        self._audio_resample = Gst.ElementFactory.make('audioresample', None)
        self._audio_convert = Gst.ElementFactory.make('audioconvert', None)
        self._audio_rate = Gst.ElementFactory.make('audiorate', None)
        self._audio_enc = Gst.ElementFactory.make('lamemp3enc', None)
        self._audio_parse = Gst.ElementFactory.make('mpegaudioparse', None)

        self._mux = Gst.ElementFactory.make('flvmux', None)
        self._sink = Gst.ElementFactory.make('rtmpsink', None)

        # adds

        self._pipeline.add(self._decodebin)

        self._pipeline.add(self._video_raw_queue)
        self._pipeline.add(self._video_rate)
        self._pipeline.add(self._video_scale)
        self._pipeline.add(self._video_convert)
        self._pipeline.add(self._video_enc)
        self._pipeline.add(self._video_parse)

        self._pipeline.add(self._audio_raw_queue)
        self._pipeline.add(self._audio_resample)
        self._pipeline.add(self._audio_convert)
        self._pipeline.add(self._audio_rate)
        self._pipeline.add(self._audio_enc)
        self._pipeline.add(self._audio_parse)

        self._pipeline.add(self._mux)
        self._pipeline.add(self._sink)

        # properties

        self._decodebin.set_property(
            'connection-speed', config['connection_speed'])
        self._decodebin.set_property('uri', media_uri)
        self._decodebin.set_property('use-buffering', 'true')

        self._video_raw_queue.set_property('flush-on-eos', 'true')
        self._video_scale.set_property('add-borders', 'true')
        self._video_enc.set_property('bframes', 0)
        self._video_enc.set_property('bitrate', config['output_video_bitrate'])
        self._video_enc.set_property('tune', 'fastdecode')

        self._audio_raw_queue.set_property('flush-on-eos', 'true')
        self._audio_enc.set_property('target', 1)
        self._audio_enc.set_property('bitrate', config['output_audio_bitrate'])
        self._audio_enc.set_property('cbr', 'true')

        self._mux.set_property('streamable', 'true')
        self._sink.set_property('location', '/'.join(
            [self._config['output_rtmp_baseurl'], stream_id]))

        # connect, link

        # decode linked to raw queues as pads are added
        self._decodebin.connect('pad-added', self._on_pad_added)

        self._video_raw_queue.link(self._video_rate)
        self._video_rate.link(self._video_scale)
        self._video_scale.link(self._video_convert)
        self._video_convert.link_filtered(
            self._video_enc,
            Gst.caps_from_string(
                ','.join([
                    'video/x-raw', 'format=I420',
                    'framerate={}'.format(
                        config['output_video_framerate']),
                    'width={}'.format(
                        config['output_video_frame_width']),
                    'height={}'.format(
                        config['output_video_frame_height']),
                    'pixel-aspect-ratio=1/1'
                ])))
        self._video_enc.link(self._video_parse)
        self._video_parse.link(self._mux)

        self._audio_raw_queue.link(self._audio_resample)
        self._audio_resample.link(self._audio_convert)
        self._audio_convert.link(self._audio_rate)
        self._audio_rate.link_filtered(
            self._audio_enc,
            Gst.caps_from_string(
                ','.join([
                    'audio/x-raw',
                    'rate={}'.format(config['output_audio_samplerate']),
                    'channels={}'.format(
                        config['output_audio_channels'])
                ])))
        self._audio_enc.link(self._audio_parse)
        self._audio_parse.link(self._mux)

        self._mux.link(self._sink)

        # states
        self._is_buffering = False

    # INTERNAL CONTROL METHODS

    def _play(self):
        if self._pipeline.current_state not in (
                Gst.State.PAUSED, Gst.State.PLAYING):
            logging.info('playing media')
            logging.debug('pausing pipeline for preroll')
            self._pipeline.set_state(Gst.State.PAUSED)
            logging.debug('prerolled; pipeline paused')

    def _postroll(self):
        if self._pipeline != Gst.State.NULL:
            logging.debug('nulling pipeline')
            self._pipeline.set_state(Gst.State.NULL)

        self._mainloop.quit()

    def _stop(self):
        if self._pipeline.current_state == Gst.State.NULL:
            return

        logging.info('stopping pipeline')
        self._postroll()

    def _get_live_position(self):
        pos_result, pos_ns = self._pipeline.query_position(
            Gst.Format.TIME)

        position = 0

        if pos_result:
            position = int(pos_ns // Gst.SECOND)

        return position

    def _get_position(self):
        pos_result, pos_ns = self._pipeline.query_position(
            Gst.Format.TIME)
        dur_result, dur_ns = self._pipeline.query_duration(
            Gst.Format.TIME)

        (position, duration) = (0, 0)

        if pos_result:
            position = int(pos_ns // Gst.SECOND)
        if dur_result:
            duration = int(dur_ns // Gst.SECOND)

        return [position, duration]

    def _seek(self, secs):
        # dur_result, dur_ns = self._pipeline.query_duration(
        dur_result, dur_ns = self._decodebin.query_duration(
            Gst.Format.TIME)
        if not dur_result:
            return False

        # pos_result, pos_ns = self._pipeline.query_position(
        pos_result, pos_ns = self._decodebin.query_position(
            Gst.Format.TIME)
        if not pos_result:
            return False

        # don't seek past file boundaries
        seek_to = pos_ns + (secs * Gst.SECOND)
        if seek_to > dur_ns or seek_to < 0:
            return False

        logging.info(
            'seeking {} secs (to {} s)'.format(
                secs, seek_to // Gst.SECOND))

        seek_result = self._decodebin.seek_simple(
            Gst.Format.TIME, Gst.SeekFlags.SEGMENT, seek_to
            # Gst.Format.TIME, Gst.SeekFlags.NONE, seek_to
            # Gst.Format.TIME, Gst.SeekFlags.KEY_UNIT | Gst.SeekFlags.SKIP, seek_to
            # Gst.Format.TIME,
            # Gst.SeekFlags.KEY_UNIT | Gst.SeekFlags.SKIP | Gst.SeekFlags.FLUSH,
            # seek_to
        )

        return seek_result

    # BUS MESSAGE HANDLERS

    def _on_buffering(self, bus, msg):
        percent = msg.parse_buffering()
        if percent == 100:
            self._is_buffering = False
            logging.debug('buffering complete; playing pipeline')
            if not self._pipeline.set_state(Gst.State.PLAYING):
                logging.error('pipeline failed to play')
                self._stop()
        else:
            if not self._is_buffering:
                logging.debug('buffering needed; pausing pipeline')
                self._pipeline.set_state(Gst.State.PAUSED)
                self._is_buffering = True

    def _on_eos(self, bus, msg):
        logging.debug('reached end of stream')
        self._postroll()

    def _on_error(self, bus, msg):
        gerror, debug = msg.parse_error()
        out = ''
        if gerror:
            out += str(gerror)
        if debug:
            out += ': {}'.format(str(gerror.message))
        if len(out):
            logging.critical('fatal error: {}'.format(out))
        self._stop()

    def _on_warning(self, bus, msg):
        # copies; free with GLib.Error.free() and GLib.free())
        gerror, debug = msg.parse_warning()
        out = ''
        if gerror:
            out += str(gerror)
        if debug:
            out += ': {}'.format(str(gerror.message))
        if len(out):
            logging.error(
                'warning from {}: {}'.format(msg.src.get_name(), out))

    def _on_clock_lost(self, bus, msg):
        logging.warning('clock lost; restarting playback')
        self._pipeline.set_state(Gst.State.PAUSED)
        self._pipeline.set_state(Gst.State.PLAYING)

    def _on_latency(self, bus, msg):
        logging.debug('redistributing latency')
        self._pipeline.recalculate_latency()

    def _on_request_state(self, bus, msg):
        state = msg.parse_request_state()
        source_name = msg.src.get_name()
        logging.debug(
            'state {} requested by {}'.format(
                state.value_nick, source_name))
        self._pipeline.set_state(state)

    def _on_application(self, bus, msg):
        # pointer; don't free
        structure = msg.get_structure()

        if structure.has_name('GstLaunchInterrupt'):
            logging.warning('caught interrupt; stopping pipeline')
            self.kill()

    # SIGNAL HANDLERS

    def _on_pad_added(self, element, pad):
        string = pad.query_caps(None).to_string()
        logging.debug('pad added: {}'.format(string))
        if string.startswith('audio/'):
            pad.link(self._audio_raw_queue.get_static_pad('sink'))
        elif string.startswith('video/'):
            pad.link(self._video_raw_queue.get_static_pad('sink'))

    # PUBLIC METHODS

    def run(self):
        if self._mainloop.is_running():
            logging.warning(
                'run requested when mainloop already running')
            return None

        # 0: init   1: started    2: stopped
        self._state.value = 1
        logging.debug('starting mainloop')
        try:
            self._mainloop.run()
        except:
            self.stop()
        finally:
            logging.debug('end of thread run method reached')
            self._state.value = 2

    def play(self):
        self._play()

    def stop(self):
        self._stop()

    def is_playing(self):
        if (self._pipeline.current_state == Gst.State.PAUSED or
                self._pipeline.current_state == Gst.State.PLAYING):
            return True
        else:
            return False

    def get_live_play_position(self):
        position = self._get_live_position()
        if not position:
            return None
        return position

    def get_play_position(self):
        position = self._get_position()
        if not position:
            return None
        if position[1] == 0:
            return None
        return position

    def seek(self, secs):
        return self._seek(secs)


def main():
    # write PID to file
    with open('player_pidfile', 'w') as pidfile:
        print(getpid(), file=pidfile)

    # check length of arguments;
    # 1 = error, 2 = idle, 3 = media, 4 = media w/modifier
    stream_id = None
    media_uri = None
    live_source = False

    if len(argv) < 2:
        logging.critical('error: no stream ID specified.')
        sys_exit(4)

    stream_id = argv[1]

    if len(argv) > 2:
        # media; capture media_uri
        media_uri = argv[2]

    if len(argv) > 3 and argv[3] == 'live':
        live_source = True

    # import config
    global_config = None
    with open('config.yaml', 'r') as config_file:
        global_config = load_yaml(config_file)
        logging.info('configuration file loaded and parsed.')

        if not isinstance(global_config, dict):
            logging.critical(
                'error: configuration file parsed into invalid type.')
            sys_exit(2)

        if len(global_config) <= 0:
            logging.critical(
                'error: configuration file parsed into empty object.')
            sys_exit(3)

    # craft process title from name (p-{name})
    process_title = 'pp-{}'.format(global_config['name'])

    # set loglevel
    target_lvl = global_config['log_level']
    if hasattr(logging, target_lvl):
        logging.getLogger().setLevel(getattr(logging, target_lvl))

    # set lock to prevent concurrency and set proctitle
    global lock_socket
    lock_socket = socket(AF_UNIX, SOCK_DGRAM)
    try:
        lock_socket.bind('\0{}'.format(process_title))
        logging.info('got process lock')
    except socket_error:
        logging.critical('failed to get process lock; already running?')
        sys_exit(1)

    # set custom process title
    setproctitle(process_title)

    # declare now to allow access by _exit
    state = None
    runtime = None
    conn = None
    listener = None

    def _exit():
        logging.debug(
            'stopping player, closing control connection, and exiting')
        if runtime:
            # 0: init   1: started    2: stopped
            if state.value == 1:
                runtime.stop()
        if conn:
            conn.close()
        if listener:
            listener.close()

    # handle signals gracefully
    def _exit_on_signal(signal, frame):
        logging.warning('caught signal {}'.format(str(signal)))
        _exit()
    signal(SIGABRT, _exit_on_signal)
    signal(SIGINT, _exit_on_signal)
    signal(SIGHUP, _exit_on_signal)
    signal(SIGQUIT, _exit_on_signal)
    signal(SIGTERM, _exit_on_signal)

    state = Value('i', 0)

    # create new runtime object based on type
    runtime = None
    if media_uri:
        runtime = Player(
            global_config['SquishPlayer'],
            state,
            stream_id,
            media_uri,
            live_source
        )
    else:
        runtime = Idler(global_config['SquishPlayer'], state, stream_id)

    # set up listener for comms with bot
    address = global_config['control_socket_file']
    listener = Listener(address, authkey=b'phoebe')

    # block on connection from bot
    logging.debug(
        'awaiting control connection on socket at {}'.format(address))
    conn = listener.accept()

    # connection made; start runtime (runs in new thread)
    logging.debug('connection accepted')
    runtime.start()

    # enter command loop (in this thread)
    while True:
        # exit if player mainloop no longer running
        # 0: init   1: started    2: stopped
        if state.value == 2:
            logging.info('player thread no longer alive')
            break

        # check for a command
        if not conn.poll(1):
            continue

        try:
            # wait for a command
            command = conn.recv()
        except (EOFError, IOError):
            logging.error(
                'Error encountered when attempting '
                'to receive from control connection'
            )
            break

        # parse into name/optional args
        cmd_name = command[0].lower()
        cmd_arg = None
        if len(command) > 1:
            cmd_arg = command[1]

        # execute command actions
        if cmd_name == 'play':
            runtime.play()

        # stop player and exit
        elif cmd_name == 'stop':
            logging.debug('stopping player on command')
            break

        # retrieve current position, duration
        elif cmd_name == 'getpos':
            if hasattr(runtime, 'get_play_position'):
                pos = runtime.get_play_position()
                if pos:
                    conn.send(['OK', pos])
                else:
                    conn.send(['ERROR', 'no position available'])
            else:
                conn.send(
                    ['ERROR', 'getpos not supported by active runtime'])

        elif cmd_name == 'getlivepos':
            if hasattr(runtime, 'get_live_play_position'):
                pos = runtime.get_live_play_position()
                if pos:
                    conn.send(['OK', pos])
                else:
                    conn.send(['ERROR', 'no live position available'])
            else:
                conn.send(
                    ['ERROR', 'getlivepos not supported by active runtime'])

        # seek by specified amount
        elif cmd_name == 'seek':
            if hasattr(runtime, 'seek'):
                if cmd_arg:
                    result = runtime.seek(cmd_arg)
                    if result:
                        conn.send(['OK'])
                    else:
                        conn.send(['ERROR', 'seek failed'])
            else:
                conn.send(
                    ['ERROR', 'seek not supported by active runtime'])

        # jump to specified position
        elif cmd_name == 'jump':
            if hasattr(runtime, 'seek'):
                error = False
                if cmd_arg:
                    pos = runtime.get_play_position()
                    if pos:
                        jump_to = cmd_arg - pos[0]
                        result = runtime.seek(jump_to)
                        if result:
                            conn.send(['OK'])
                        else:
                            error = True
                    else:
                        error = True
                if error:
                    conn.send(['ERROR', 'jump failed'])
            else:
                conn.send(
                    ['ERROR', 'jump not supported by active runtime'])

    # out of command loop; clean up and exit
    logging.debug('exited command loop')
    _exit()


if __name__ == '__main__':
    main()
    logging.debug('exited main(); EOF')
