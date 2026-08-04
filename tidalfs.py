#!/usr/bin/env python
import tidalapi
import requests
import tempfile
import threading
import os
import re
import stat
import logging
from pathlib import Path
from time import sleep

from fuse import FUSE, FuseOSError, Operations, LoggingMixIn
from mutagen.mp4 import MP4, MP4Cover

SESSION_FILE = Path.home() / ".config" / "tidalfs" / "session.json"
BASE_DIRS = ['.', '..']
ABC = list('ABCDEFGHIJKLMNOPQRSTUVWXYZ')

CACHE_DIR = tempfile.TemporaryDirectory()
TRACKS_CACHE = {}
ALBUMS_CACHE = {}
DIRS_CACHE = {}
LINKS_CACHE = {}

# Limit concurrent Tidal API + download requests to avoid 429s
DOWNLOAD_SEM = threading.Semaphore(1)


def _login(session):
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)

    def _print_url(url):
        print("\n" + "=" * 60)
        print("  Tidal login required.")
        print("  Visit the link below and approve — you have 5 minutes.")
        print("=" * 60)
        print(f"\n  --> {url}\n")

    session.login_session_file(SESSION_FILE, fn_print=_print_url)


def _favorites(session):
    return tidalapi.user.Favorites(session, session.user.id)


def get_entries_for_path(path, session, root):
    logging.info('readdir: %s', path)

    if path == '/':
        return BASE_DIRS + ['Artist', 'Album', 'Track', 'Favorites']

    if path == '/Favorites':
        return BASE_DIRS + ['Albums', 'Tracks']

    if path == '/Favorites/Artists':
        dirs = []
        for artist in _favorites(session).artists():
            name = artist.name.replace('/', '-')
            LINKS_CACHE[f'{path}/{name}'] = f'{root}/.artists/{artist.id}'
            dirs.append(name)
        return BASE_DIRS + dirs

    if path == '/Favorites/Albums':
        dirs = []
        for album in _favorites(session).albums():
            name = album.name.replace('/', '-')
            LINKS_CACHE[f'{path}/{name}'] = f'{root}/.albums/{album.id}'
            dirs.append(name)
        return BASE_DIRS + dirs

    if path == '/Favorites/Tracks':
        files = []
        for track in _favorites(session).tracks():
            name = f"{track.name.replace('/', '-')} ({track.artist.name}).m4a"
            LINKS_CACHE[f'{path}/{name}'] = f'{root}/.tracks/{track.id}.m4a'
            files.append(name)
        return BASE_DIRS + files

    if path in ('/Artist', '/Album', '/Track'):
        return BASE_DIRS + ABC

    if path.endswith('/Search'):
        parts = path.split('/')
        entity = parts[1]
        term = ' '.join(parts[2:-1]).replace('Space', ' ').lower()
        type_map = {
            'Artist': tidalapi.artist.Artist,
            'Album':  tidalapi.album.Album,
            'Track':  tidalapi.media.Track,
        }
        results = session.search(term, models=[type_map[entity]])
        dirs = []
        if entity == 'Artist':
            for a in results['artists']:
                name = a.name.replace('/', '-')
                LINKS_CACHE[f'{path}/{name}'] = f'{root}/.artists/{a.id}'
                dirs.append(name)
        elif entity == 'Album':
            for a in results['albums']:
                name = f"{a.name} ({a.artist.name})".replace('/', '-')
                LINKS_CACHE[f'{path}/{name}'] = f'{root}/.albums/{a.id}'
                dirs.append(name)
        return BASE_DIRS + dirs

    if any(path.startswith(f'/{t}/') for t in ('Artist', 'Album', 'Track')):
        return BASE_DIRS + ['Search', 'Space'] + ABC

    if path.startswith('/.albums/'):
        album_id = path.split('/')[-1]
        album = session.album(album_id=album_id)
        files = []
        for i, track in enumerate(album.tracks()):
            name = f"{i+1:02d} - {track.name.replace('/', '-')}.m4a"
            TRACKS_CACHE[track.id] = track
            LINKS_CACHE[f'{path}/{name}'] = f'{root}/.tracks/{track.id}.m4a'
            files.append(name)
        return BASE_DIRS + files

    if re.match(r'^/\.artists/\d+$', path):
        return BASE_DIRS + ['Albums', 'EPs & Singles', 'Top Tracks', 'Radio', 'Similar Artists']

    if re.match(r'^/\.artists/\d+/Albums$', path):
        artist_id = path.split('/')[2]
        artist = session.artist(artist_id=artist_id)
        dirs = []
        for album in artist.get_albums():
            name = f"{album.year} - {album.name.replace('/', '-')}"
            ALBUMS_CACHE[album.id] = album
            LINKS_CACHE[f'{path}/{name}'] = f'{root}/.albums/{album.id}'
            dirs.append(name)
        return BASE_DIRS + dirs

    if re.match(r'^/\.artists/\d+/EPs & Singles$', path):
        artist_id = path.split('/')[2]
        artist = session.artist(artist_id=artist_id)
        dirs = []
        for album in artist.get_albums_ep_singles():
            name = f"{album.year} - {album.name.replace('/', '-')}"
            ALBUMS_CACHE[album.id] = album
            LINKS_CACHE[f'{path}/{name}'] = f'{root}/.albums/{album.id}'
            dirs.append(name)
        return BASE_DIRS + dirs

    if re.match(r'^/\.artists/\d+/Top Tracks$', path):
        artist_id = path.split('/')[2]
        artist = session.artist(artist_id=artist_id)
        files = []
        for i, track in enumerate(artist.get_top_tracks(limit=100)):
            name = f"{i+1:02d}. {track.name.replace('/', '-')}.m4a"
            TRACKS_CACHE[track.id] = track
            LINKS_CACHE[f'{path}/{name}'] = f'{root}/.tracks/{track.id}.m4a'
            files.append(name)
        return BASE_DIRS + files

    if re.match(r'^/\.artists/\d+/Radio$', path):
        artist_id = path.split('/')[2]
        artist = session.artist(artist_id=artist_id)
        files = []
        for i, track in enumerate(artist.get_radio()):
            name = f"{i+1:02d}. {track.name.replace('/', '-')} ({track.artist.name}).m4a"
            TRACKS_CACHE[track.id] = track
            LINKS_CACHE[f'{path}/{name}'] = f'{root}/.tracks/{track.id}.m4a'
            files.append(name)
        return BASE_DIRS + files

    if re.match(r'^/\.artists/\d+/Similar Artists$', path):
        artist_id = path.split('/')[2]
        artist = session.artist(artist_id=artist_id)
        dirs = []
        for a in artist.get_similar():
            name = a.name.replace('/', '-')
            LINKS_CACHE[f'{path}/{name}'] = f'{root}/.artists/{a.id}'
            dirs.append(name)
        return BASE_DIRS + dirs

    return BASE_DIRS


def _tag_track(track, track_path):
    try:
        tags = MP4(track_path)
        tags['\xa9nam'] = [track.name]
        tags['\xa9ART'] = [track.artist.name]
        tags['\xa9alb'] = [track.album.name]
        tags['trkn'] = [(track.track_num or 1, 0)]
        if hasattr(track.album, 'year') and track.album.year:
            tags['\xa9day'] = [str(track.album.year)]
        album_artist = getattr(track.album, 'artist', None)
        tags['aART'] = [album_artist.name if album_artist else track.artist.name]
        try:
            cover_url = track.album.image(320)
            if cover_url:
                r = requests.get(cover_url, timeout=10)
                if r.status_code == 200:
                    tags['covr'] = [MP4Cover(r.content, imageformat=MP4Cover.FORMAT_JPEG)]
        except Exception:
            pass
        tags.save()
    except Exception as e:
        logging.warning('tagging failed: %s', e)


def _download_track(session, track_id, track_path):
    done = track_path + '.done'
    err = track_path + '.error'
    if os.path.exists(done) or os.path.exists(err):
        return
    if os.path.exists(track_path):
        return  # another thread is downloading
    Path(track_path).touch()
    with DOWNLOAD_SEM:
        try:
            track = TRACKS_CACHE.get(int(track_id)) or session.track(track_id=track_id)
            TRACKS_CACHE[int(track_id)] = track
            for attempt in range(8):
                try:
                    url = track.get_url()
                    break
                except Exception as e:
                    if attempt == 7:
                        raise
                    # honour Retry-After if present, else exponential backoff
                    retry_after = None
                    cause = getattr(e, '__cause__', None)
                    if cause is not None:
                        resp = getattr(cause, 'response', None)
                        if resp is not None:
                            retry_after = resp.headers.get('Retry-After')
                    wait = float(retry_after) if retry_after else min(2 ** attempt, 60)
                    logging.warning('get_url rate-limited, waiting %.0fs (attempt %d) track=%s', wait, attempt + 1, track_id)
                    sleep(wait)
            with requests.get(url, stream=True) as r:
                with open(track_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            _tag_track(track, track_path)
            Path(done).touch()
        except Exception as e:
            logging.error('download failed track=%s: %s', track_id, e)
            try:
                os.remove(track_path)
            except OSError:
                pass
            Path(err).touch()  # unblock any read() waiting on this track


class Tidal(LoggingMixIn, Operations):
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.session = tidalapi.Session()
        _login(self.session)

    def getattr(self, path, fh=None):
        base = {
            'st_atime': 1, 'st_gid': os.getgid(), 'st_uid': os.getuid(),
            'st_mtime': 1, 'st_size': 4096, 'st_mode': stat.S_IFDIR | 0o555,
        }
        if path.endswith('.m4a'):
            if path.startswith('/.tracks/'):
                base['st_mode'] = stat.S_IFREG | 0o444
                base['st_size'] = 50_000_000
            else:
                base['st_mode'] = stat.S_IFLNK | 0o444
        elif path in LINKS_CACHE or '/Search/' in path:
            base['st_mode'] = stat.S_IFLNK | 0o444
        return base

    def readdir(self, path, fh):
        try:
            if path not in DIRS_CACHE:
                DIRS_CACHE[path] = get_entries_for_path(path, self.session, self.root)
            return DIRS_CACHE[path]
        except Exception as e:
            logging.error('readdir failed %s: %s', path, e)
            return BASE_DIRS

    def readlink(self, path):
        return LINKS_CACHE.get(path, path)

    def read(self, path, size, offset, fh):
        if not path.endswith('.m4a'):
            return b''
        filename = path.split('/')[-1]
        if filename.startswith('._'):
            return b''
        track_id = filename[:-4]
        track_path = os.path.join(CACHE_DIR.name, f'{track_id}.m4a')
        threading.Thread(
            target=_download_track,
            args=(self.session, track_id, track_path),
            daemon=True,
        ).start()
        done = track_path + '.done'
        err = track_path + '.error'
        while not os.path.exists(done) and not os.path.exists(err):
            sleep(0.01)
        if os.path.exists(err):
            return b''
        with open(track_path, 'rb') as f:
            f.seek(offset)
            return f.read(size)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Mount Tidal as a local filesystem')
    parser.add_argument('mount', help='Mount point directory')
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    FUSE(Tidal(args.mount), args.mount, foreground=True, nothreads=False, allow_other=True)
