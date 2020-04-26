# from imp import load_source
from __future__ import absolute_import
from json import loads
from re import search
from subprocess import CalledProcessError, check_output
from time import time
from six.moves.urllib.parse import urlparse, ParseResult


class RequestTypes:
    SITE = 1
    DIRECT = 2


class PlayRequest:

    def __init__(
        self,
        config,
        httpsession,
        sender,
        direct=False,
        request_uri=None,
        filter_module=None,
        search_terms=None
    ):
        self.config = config
        self.httpsession = httpsession
        self.sender = sender
        self.direct = direct
        self.request_uri = request_uri
        self.filter_module = filter_module
        self.search_terms = search_terms
        self.duration = 0
        self.last_fetched = 0
        self.rating = 0
        self.title = ''
        self.votes = dict()
        self.error = None
        self.source_site = None
        self.media_uri = None
        self.request_type = None

        self.live_source = False

        self.prepared = False

    # PREPARE REQUEST BY POPULATING OTHER ATTRIBUTES ###################

    def update_site_media_info(self):
        # fix slash-escape issue in request URI
        if isinstance(self.request_uri, ParseResult):
            self.request_uri = self.request_uri.geturl()
        if self.request_uri.count('///'):
            self.request_uri = self.request_uri.replace('///', '//')

        # use youtube_dl to extract vital media info (into media_info)
        ydl_raw_json = ''
        try:
            ydl_raw_json = check_output([
                self.config['ydl_bin'],
                '--dump-json',
                '--format',
                'best[height <=? 1080][protocol !=? m3u8_native]',
                self.request_uri
            ])
        except CalledProcessError:
            self.error = ' '.join([
                'failed to extract any playable media;',
                'confirm site is supported and check URL for typos'
            ])
            return False

        # parse returned string JSON to a dict
        ydl_output = ''
        try:
            ydl_output = loads(ydl_raw_json)
        except ValueError:
            self.error = ' '.join([
                'error parsing youtube-dl json output;',
                'please contact the developer'
            ])
            return False

        media_info = {}

        # need at lease a 'url' key to do anything useful
        if 'url' not in ydl_output:
            self.error = ' '.join([
                'no video URL found;',
                'check that media isn\'t private or deleted'
            ])
            return False
        else:
            media_info['url'] = ydl_output['url']

        optional_keys = [
            "duration",
            "ext",
            "extractor_key",
            # "format",
            # "format_id",
            # "format_note",
            "is_live",
            ["http_headers", "User-Agent"],
            "title"
        ]

        bad_exts = [
            'asp',
            'aspx',
            'htm',
            'html',
            'js',
            'jsp',
            'php',
            'xml',
            'xhtml'
        ]

        # check for (and copy to media_info) any optional keys found in
        # ydl_output
        for key in optional_keys:
            if isinstance(key, list):
                label = key[0]
                value = key[1]
                if label in ydl_output:
                    if value in ydl_output[label]:
                        media_info[label] = {value: ydl_output[label][value]}
            else:
                if key in ydl_output:
                    if isinstance(ydl_output[key], float):
                        media_info[key] = int(ydl_output[key])
                    elif key == 'ext':
                        if ydl_output[key] in bad_exts:
                            self.error = ' '.join([
                                'no video URL found;',
                                'check that media isn\'t private or deleted'
                            ])
                            return False
                    else:
                        media_info[key] = ydl_output[key]

        # derive site title from domain if 'Generic' extractor was used
        if 'extractor_key' in media_info:
            if media_info['extractor_key'] == 'Generic':
                # parse url into urlparse object
                parsed_url = urlparse(media_info['url'])

                # extract domain (e.g., www.site.com)
                domain = parsed_url.netloc
                if not len(domain):
                    domain = parsed_url.path.split('/')[0] if (
                        parsed_url.path.count('/')) else parsed_url.path

                # extract source site (e.g., 'site' from www.site.com)
                site_match = search('(\w+\.\w+)$', domain)

                if not site_match:
                    self.error = 'malformed domain {} in URL'.format(domain)
                    return False

                media_info['source_site'] = site_match.groups()[0]
            else:
                media_info['source_site'] = media_info['extractor_key']

        # set request to live source if is_live returned True from ydl
        if 'is_live' in media_info:
            if media_info['is_live']:
                self.live_source = True

        self.title = ''
        self.duration = 0

        # get needed values from media_info; use blanks for nulls/empty strings
        self.media_uri = media_info['url']
        self.source_site = media_info['source_site']

        if 'title' in media_info:
            if len(media_info['title']):
                self.title = media_info['title']
        if 'duration' in media_info:
            if media_info['duration'] > 0:
                self.duration = media_info['duration']

        self.last_fetched = time()

        return True

    def prepare(self):

        if not self.request_uri:
            if not self.search_terms:
                self.error = 'empty request; nothing to do'
                return False

            # if not self.config['search_filter']:
            if not self.filter_module:
                self.error = ' '.join([
                    'no search filter configured;',
                    'keyword search disabled'
                ])
                return False

            # attempt to fetch a URL

            filter_output = []
            filter_output = self.filter_module.get_uri(
                self.httpsession, self.search_terms)

            if not isinstance(filter_output, list):
                self.error = 'search filter returned unexpected data'
                return False

            if not len(filter_output) == 2:
                self.error = 'search filter returned unexpected data'
                return False

            if filter_output[0] != 0:
                self.error = filter_output[1]
                return False

            self.request_uri = urlparse(filter_output[1], scheme='http')

        if self.request_uri.scheme not in ('http', 'https'):
            self.error = "unsupported scheme '{}'".format(
                self.request_uri.scheme)
            return False

        if self.direct:
            # direct play; assume raw media URL given
            self.request_type = RequestTypes.DIRECT
            self.media_uri = self.request_uri.geturl()
            self.title = '[direct-play] file: {}'.format(
                self.request_uri.path.split('/')[-1])
            self.request_uri = self.request_uri.geturl()
        else:
            # page URL given; parse for metadata and media URL
            self.request_type = RequestTypes.SITE

            if not self.update_site_media_info():
                return False

        self.prepared = True
        return True

    def upvote(self, sender):
        if sender in self.votes:
            if self.votes[sender] > 0:
                return False
            self.votes[sender] += 1
        else:
            self.votes[sender] = 1

        self.rating = sum(self.votes.values())
        return True

    def downvote(self, sender):
        if sender in self.votes:
            if self.votes[sender] < 0:
                return False
            self.votes[sender] -= 1
        else:
            self.votes[sender] = -1

        self.rating = sum(self.votes.values())
        return True
