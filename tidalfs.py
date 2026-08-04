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

SESSION_FILE = Path.home() / ".config" / "tidalfs" / "session.json"
BASE_DIRS = ['.', '..']
ABC = list('ABCDEFGHIJKLMNOPQRSTUVWXYZ')

CACHE_DIR = tempfile.TemporaryDirectory()
TRACKS_CACHE = {}
ALBUMS_CACHE = {}
DIRS_CACHE = {}
LINKS_CACHE = {}


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
        return BASE_DIRS + ['Artists', 'Albums', 'Tracks']

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


def _download_track(session, track_id, track_path):
    if os.path.exists(track_path):
        return
    Path(track_path).touch()
    try:
        track = TRACKS_CACHE.get(int(track_id)) or session.track(track_id=track_id)
        TRACKS_CACHE[int(track_id)] = track
        url = track.get_url()
        with requests.get(url, stream=True) as r:
            with open(track_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        Path(track_path + '.done').touch()
    except Exception as e:
        logging.error('download failed: %s', e)
        try:
            os.remove(track_path)
        except OSError:
            pass


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
        if path not in DIRS_CACHE:
            DIRS_CACHE[path] = get_entries_for_path(path, self.session, self.root)
        return DIRS_CACHE[path]

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
        while not os.path.exists(track_path):
            sleep(0.01)
        with open(track_path, 'rb') as f:
            done = False
            data = b''
            while len(data) < size and not done:
                sleep(0.01)
                f.seek(offset)
                data = f.read(size)
                if os.path.exists(track_path + '.done'):
                    done = True
        return data


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Mount Tidal as a local filesystem')
    parser.add_argument('mount', help='Mount point directory')
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    FUSE(Tidal(args.mount), args.mount, foreground=True, nothreads=True)
