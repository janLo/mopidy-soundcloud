from __future__ import unicode_literals

import collections
import logging
import re
import string
import time
import unicodedata
from multiprocessing.pool import ThreadPool
from urllib import quote_plus

from mopidy.models import Album, Artist, Track

import requests
from requests.adapters import HTTPAdapter


logger = logging.getLogger(__name__)


def safe_url(uri):
    return quote_plus(
        unicodedata.normalize('NFKD', unicode(uri)).encode('ASCII', 'ignore'))


def readable_url(uri):
    valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
    safe_uri = unicodedata.normalize('NFKD', unicode(uri)).encode('ASCII',
                                                                  'ignore')
    return re.sub('\s+', ' ',
                  ''.join(c for c in safe_uri if c in valid_chars)).strip()


class cache(object):
    # TODO: merge this to util library

    def __init__(self, ctl=8, ttl=3600):
        self.cache = {}
        self.ctl = ctl
        self.ttl = ttl
        self._call_count = 1

    def __call__(self, func):
        def _memoized(*args):
            self.func = func
            now = time.time()
            try:
                value, last_update = self.cache[args]
                age = now - last_update
                if self._call_count >= self.ctl or age > self.ttl:
                    self._call_count = 1
                    raise AttributeError

                self._call_count += 1
                return value

            except (KeyError, AttributeError):
                value = self.func(*args)
                self.cache[args] = (value, now)
                return value

            except TypeError:
                return self.func(*args)

        return _memoized


class SoundCloudClient(object):
    CLIENT_ID = '93e33e327fd8a9b77becd179652272e2'

    def __init__(self, config):
        super(SoundCloudClient, self).__init__()
        token = config['auth_token']
        self.explore_songs = config.get('explore_songs', 10)
        max_retries = config.get('http_max_retries',
                                 requests.adapters.DEFAULT_RETRIES)
        self.http_client = requests.Session()
        self.http_client.mount('https://api.soundcloud.com',
                               HTTPAdapter(max_retries=max_retries))
        self.http_client.headers.update({'Authorization': 'OAuth %s' % token})

        try:
            self._get('me')
        except Exception as err:
            if err.response is not None and err.response.status_code == 401:
                logger.error('Invalid "auth_token" used for SoundCloud '
                             'authentication!')
            else:
                raise

    @property
    @cache()
    def user(self):
        return self._get('me')

    @cache()
    def get_user_stream(self):
        # https://developers.soundcloud.com/docs/api/reference#activities
        tracks = []
        stream = self._get('me/activities?limit=500').get('collection')
        if not stream:
            raise Exception('Could not load your stream!')
        else:
            for data in stream:
                track = data.get('origin')
                # multiple types of track with same data
                if track:
                    if track['kind'] == 'track':
                        tracks.append(self.parse_track(track))
                    if track['kind'] == 'playlist':
                        playlist = track.get('tracks')
                        if isinstance(playlist, collections.Iterable):
                            tracks.extend(self.parse_results(playlist))

        return self.sanitize_tracks(tracks)

    def get_followings(self, query_user_id=None):
        if query_user_id:
            return self._get('users/%s/tracks' % query_user_id)

        users = []
        for playlist in self._get('me/followings?limit=500')['collection']:
            name = playlist.get('username')
            user_id = str(playlist.get('id'))
            logger.debug('Fetched user %s with id %s' % (
                name, user_id
            ))

            users.append((name, user_id))
        return users

    def get_history(self):
        tracks = []
        for track in self._get('me/play-history/tracks?limit=30&offset=0&linked_partitioning=1', 'api-v2')['collection']:
            tracks.append(track['track'])
        return tracks

    def load_from_history(self, track_ids):
        history = self.get_history();

        tracks = []
        missing = []
        for track_id in track_ids:
            found = False
            for historical in history:
                if historical.get('id') == track_id:
                    tracks.append(self.parse_track(historical))
                    found = True
                    break
            if not found:
                missing.append(track_id)

        tracks.extend(self.resolve_tracks(missing))

        return self.sanitize_tracks(tracks);


    def get_selections(self, selection_id=None, option_id=None):
        # Return the first list
        if not selection_id:
            selections = []
            selection_id = 0
            for selection in self._get('selections?limit=30&offset=0&linked_partitioning=1', 'api-v2')['collection']:
                selections.append((selection.get('title'), str(selection_id)))
                selection_id += 1
            return selections

        selection = self._get('selections?limit=50&offset=0&linked_partitioning=1', 'api-v2')['collection'][int(selection_id)]
        playlistString = "system_playlists" if selection.get('system_playlists') else "playlists"

        # We have the selection_id but not 2nd, return list of playlists.
        if not option_id:
            playlists = [];
            option_id = 0
            for option in selection.get(playlistString):
                if option.get('description'):
                    playlists.append(("%s - %s" % (option.get('title'), option.get('description')), str(option_id)))
                else:
                    playlists.append((option.get('title'), str(option_id)))
                option_id += 1
            return playlists

        selection = selection.get(playlistString)[int(option_id)]

        if selection['kind'] == "playlist":
            return self.get_set(selection['id'])

        # We have both options, return the tracks.
        track_ids = [x.get('id') for x in selection['tracks']]

        #tracks = self.resolve_tracks(track_ids)
        
        return self.load_from_history(track_ids);


    @cache()
    def get_set(self, set_id):
        # https://developers.soundcloud.com/docs/api/reference#playlists
        playlist = self._get('playlists/%s' % set_id)
        return playlist.get('tracks', [])

    def get_genre(self, genre):
        # https://developers.soundcloud.com/docs/api/reference#playlists
        results = []
        logger.info("Getting genre %s" % genre)
        for record in self._get('charts?kind=top&genre=soundcloud:genres:%s&limit=50' % genre, 'api-v2')['collection']:
            results.append(self.parse_track(record['track']))
        return self.sanitize_tracks(results)
        
    def get_sets(self):
        playable_sets = []
        for record in self._get('users/%s/playlists/liked_and_owned?limit=500' % self._get('me')['id'], 'api-v2')['collection']:
            playlist = record.get('playlist')
            name = playlist.get('title')
            set_id = str(playlist.get('id'))
            tracks = playlist.get('track_count')
            logger.info('Fetched set %s with id %s (%s tracks)' % (
                name, set_id, tracks
            ))
            playable_sets.append((name, set_id, tracks)) 
        return self.sanitize_tracks(playable_sets)

    def get_user_liked(self):
        # https://developers.soundcloud.com/docs/api/reference#GET--users--id--favorites
        likes = []
        liked = self._get('me/favorites?limit=500')
        if not liked:
            raise Exception('Could not load your likes!')
        else:
            for data in liked:
                if data['kind'] == 'track':
                    likes.append(self.parse_track(data))
                else:
                    likes.append((data['title'], str(data['id'])))
        return self.sanitize_tracks(likes)

    # Public
    @cache()
    def get_track(self, track_id, streamable=False):
        logger.debug('Getting info for track with id %s' % track_id)
        try:
            return self.parse_track(self._get('tracks/%s' % track_id),
                                    streamable)
        except Exception:
            return None

    def parse_track_uri(self, track):
        logger.debug('Parsing track %s' % (track))
        if hasattr(track, "uri"):
            track = track.uri
        return track.split('.')[-1]

    def search(self, query):
        # https://developers.soundcloud.com/docs/api/reference#tracks
        search_results = self._get(
            'tracks?q=%s&limit=%d' % (
                quote_plus(query.encode('utf-8')), self.explore_songs))
        tracks = []
        for track in search_results:
            tracks.append(self.parse_track(track, False))
        return self.sanitize_tracks(tracks)

    def parse_results(self, res):
        tracks = []
        for item in res:
            logger.debug('Parsing item %s in results...', item['kind'])
            if item['kind'] == 'track':
                tracks.append(self.parse_track(item))
            elif item['kind'] == 'playlist':
                for track in item['tracks']:
                    logger.debug('  Parsing item %s in playlist...',
                                 track['kind'])
                    tracks.append(self.parse_track(track))
            else:
                logger.warning("I don't know how to parse a '%s'.",
                               item['kind'])
        return self.sanitize_tracks(tracks)

    def resolve_url(self, uri):
        return self.parse_results([self._get('resolve?url=%s' % uri)])

    def _get(self, url, endpoint='api'):
        if '?' in url:
            url = '%s&client_id=%s' % (url, self.CLIENT_ID)
        else:
            url = '%s?client_id=%s' % (url, self.CLIENT_ID)

        url = 'https://%s.soundcloud.com/%s' % (endpoint, url)

        logger.debug('Requesting %s' % url)
        res = self.http_client.get(url)
        res.raise_for_status()
        return res.json()

    def sanitize_tracks(self, tracks):
        return filter(None, tracks)

    @cache()
    def parse_track(self, data, remote_url=False):
        if not data:
            return None
        if not data['streamable']:
            logger.info(
                "'%s' can't be streamed from SoundCloud - data'streamable doesn't exist" % data.get('title'))
            return None
        if not data['kind'] == 'track':
            logger.debug('%s is not track' % data.get('title'))
            return None

        # NOTE kwargs dict keys must be bytestrings to work on Python < 2.6.5
        # See https://github.com/mopidy/mopidy/issues/302 for details.

        track_kwargs = {}
        artist_kwargs = {}
        album_kwargs = {}

        if 'title' in data:
            name = data['title']
            label_name = data.get('label_name')

            if bool(label_name):
                track_kwargs[b'name'] = name
                artist_kwargs[b'name'] = label_name
            else:
                track_kwargs[b'name'] = name
                artist_kwargs[b'name'] = data.get('user').get('username')

            album_kwargs[b'name'] = 'SoundCloud'

        if 'date' in data:
            track_kwargs[b'date'] = data['date']

        if remote_url:
            if not self.can_be_streamed(data['stream_url']):
                logger.info("'%s' can't be streamed from SoundCloud canbestreamed return false" % data.get('title'))
                return None
            track_kwargs[b'uri'] = self.get_streamble_url(data['stream_url'])
        else:
            track_kwargs[b'uri'] = 'soundcloud:song/%s.%s' % (
                readable_url(data.get('title')), data.get('id')
            )

        track_kwargs[b'length'] = int(data.get('duration', 0))
        track_kwargs[b'comment'] = data.get('permalink_url', '')

        if artist_kwargs:
            artist = Artist(**artist_kwargs)
            track_kwargs[b'artists'] = [artist]

        if album_kwargs:
            if 'artwork_url' in data and data['artwork_url']:
                album_kwargs[b'images'] = [data['artwork_url'].replace("large","t500x500")]
            else:
                image = data.get('user').get('avatar_url').replace("large","t500x500") 
                album_kwargs[b'images'] = [image]

            album = Album(**album_kwargs)
            track_kwargs[b'album'] = album

        track = Track(**track_kwargs)
        return track

    @cache()
    def can_be_streamed(self, url):
        req = self.http_client.head(self.get_streamble_url(url))
        logger.info(self.get_streamble_url(url))
        return req.status_code == 302
 
    def get_streamble_url(self, url):
        return '%s?client_id=%s' % (url, self.CLIENT_ID)

    def resolve_tracks(self, track_ids):
        """Resolve tracks concurrently emulating browser

        :param track_ids:list of track ids
        :return:list `Track`
        """
        pool = ThreadPool(processes=16)
        tracks = pool.map(self.get_track, track_ids)
        pool.close()
        return self.sanitize_tracks(tracks)
