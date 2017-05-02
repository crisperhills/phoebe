from circuits import Event


class broadcast_ready(Event):
    '''
    Event fired when a stream ID is received, signaling broadcasting can begin.
    '''


class command_received(Event):
    '''
    Event fired whenever a command is parsed from new messages.
    '''


class do_change_vote(Event):
    '''
    Event fired to change a user's vote on currently playing items.
    '''


class do_check_request_queue(Event):
    '''
    Event fired to check request queue and
    play queued media, or idle if queue empty.
    '''


class do_get_current_info(Event):
    '''
    Event fired to fetch info about media
    currently playing and return it to the room.
    '''


class do_get_queue_info(Event):
    '''
    Event fired to fetch info about media
    queued for playback and return it to the room.
    '''


class do_drop_queue_item(Event):
    '''
    Event fired to drop an item from the media queue.
    '''


class do_join_room(Event):
    '''
    Event fired during startup (or when disconnected) to join a room.
    '''


class do_process_api_response(Event):
    '''
    Event fired when response received from API.
    '''


class do_process_next_action(Event):
    '''
    Event fired to process the next item in the HTTP API action queue.
    '''


class do_queue_play_request(Event):
    '''
    Event fired to add a request to the player queue.
    '''


class do_send_message(Event):
    '''
    Event fired whenever a new message is to be sent to the channel.
    '''


class do_stop_current_media(Event):
    '''
    Event fired on user request to stop currently-playing media.
    '''


class do_seek_current_media(Event):
    '''
    Event fired on user request to seek within currently-playing media.
    '''


class do_jump_current_media(Event):
    '''
    Event fired on user request to jump to
    a specific position currently-playing media.
    '''


class do_shutdown(Event):
    '''
    Event fired when the bot is instructed to exit via command or signal.
    '''


class messages_received(Event):
    '''
    Event fired whenever new messages received.
    '''


class room_joined(Event):
    '''
    Event fired after room successfully joined.
    '''
