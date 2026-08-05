#!/usr/bin/env python
import tidalapi
import requests
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
CACHE_PATH = Path("/var/cache/tidalfs")
BASE_DIRS = ['.', '..']
ABC = list('ABCDEFGHIJKLMNOPQRSTUVWXYZ')

TRACKS_CACHE = {}
ALBUMS_CACHE = {}
DIRS_CACHE = {}
LINKS_CACHE = {}
_PREFETCH_STARTED = set()
_UPGRADING = set()
_UPGRADING_LOCK = threading.Lock()
_DIRS_FILL_LOCK = threading.Lock()
_DIRS_FILLING = {}  # path → threading.Event for in-flight fetches

MIN_FREE_BYTES = 1_500_000_000  # 1.5 GB — stop downloads if disk gets this low

# 0.05-second AAC silence — used as stub base for fast metadata scanning
SILENCE_M4A_BYTES = bytes.fromhex(
    '0000001c667479704d344120000002004d34412069736f6d69736f3200000008667265650000'
    '00296d646174de02004c61766336322e31312e313030000230400e0118200701182007011820'
    '070000030a6d6f6f760000006c6d766864000000000000000000000000000003e80000003200'
    '0100000100000000000000000000000001000000000000000000000000000000010000000000'
    '0000000000000000004000000000000000000000000000000000000000000000000000000000'
    '000002000002357472616b0000005c746b686400000003000000000000000000000001000000'
    '0000000032000000000000000000000001010000000001000000000000000000000000000000'
    '0100000000000000000000000000004000000000000000000000000000002465647473000000'
    '1c656c73740000000000000001000000320000040000010000000001ad6d646961000000206d'
    '6468640000000000000000000000000000ac4400000c9d55c400000000002d68646c72000000'
    '0000000000736f756e000000000000000000000000536f756e6448616e646c65720000000158'
    '6d696e6600000010736d686400000000000000000000002464696e660000001c647265660000'
    '0000000000010000000c75726c20000000010000011c7374626c0000006a7374736400000000'
    '000000010000005a6d703461000000000000000100000000000000000001001000000000ac44'
    '0000000000366573647300000000038080802500010004808080174015000000000177000000'
    '0e150580808005120856e5000680808001020000002073747473000000000000000200000003'
    '00000400000000010000009d0000001c73747363000000000000000100000001000000040000'
    '0001000000247374737a00000000000000000000000400000015000000040000000400000004'
    '000000147374636f00000000000000010000002c0000001a7367706401000000726f6c6c0000'
    '000200000001ffff0000001c7362677000000000726f6c6c0000000100000004000000010000'
    '006175647461000000596d657461000000000000002168646c7200000000000000006d646972'
    '6170706c0000000000000000000000002c696c737400000024a9746f6f0000001c6461746100'
    '000001000000004c61766636322e332e313030'
)
STUB_SIZE_THRESHOLD = 50_000  # bytes — stubs are ~3-5KB, real tracks are several MB

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
        offset = 0
        while True:
            batch = _favorites(session).albums(limit=500, offset=offset)
            if not batch:
                break
            for album in batch:
                name = album.name.replace('/', '-')
                LINKS_CACHE[f'{path}/{name}'] = f'{root}/.albums/{album.id}'
                dirs.append(name)
            if len(batch) < 500:
                break
            offset += 500
        return BASE_DIRS + dirs

    if path == '/Favorites/Tracks':
        all_tracks = []
        files = []
        offset = 0
        while True:
            batch = _favorites(session).tracks(limit=500, offset=offset)
            if not batch:
                break
            for track in batch:
                name = f"{track.name.replace('/', '-')} ({track.artist.name}).m4a"
                TRACKS_CACHE[track.id] = track
                LINKS_CACHE[f'{path}/{name}'] = f'{root}/.tracks/{track.id}.m4a'
                files.append(name)
                all_tracks.append(track)
            if len(batch) < 500:
                break
            offset += 500
        if path not in _PREFETCH_STARTED:
            _PREFETCH_STARTED.add(path)
            threading.Thread(target=_prefetch_tracks, args=(session, all_tracks), daemon=True).start()
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


def _create_stub(track, track_path):
    """Write a tiny tagged M4A stub so scanners can extract metadata without downloading."""
    done = track_path + '.done'
    if os.path.exists(done):
        return
    try:
        with open(track_path, 'wb') as f:
            f.write(SILENCE_M4A_BYTES)
        _tag_track(track, track_path)
        Path(done).touch()
        logging.info('stub created track=%s', track.id)
    except Exception as e:
        logging.warning('stub creation failed track=%s: %s', track.id, e)
        try:
            os.remove(track_path)
        except OSError:
            pass


def _retry_api(fn, track_id, label):
    for attempt in range(8):
        try:
            return fn()
        except Exception as e:
            if attempt == 7:
                raise
            retry_after = None
            cause = getattr(e, '__cause__', None)
            if cause is not None:
                resp = getattr(cause, 'response', None)
                if resp is not None:
                    retry_after = resp.headers.get('Retry-After')
            wait = float(retry_after) if retry_after else min(2 ** attempt, 60)
            logging.warning('%s rate-limited, waiting %.0fs (attempt %d) track=%s', label, wait, attempt + 1, track_id)
            sleep(wait)


def _download_track(session, track_id, track_path):
    done = track_path + '.done'
    err = track_path + '.error'
    dl_path = track_path + '.dl'  # temp download target — atomic rename on success

    if os.path.exists(err):
        return
    # Already a real file
    if os.path.exists(done):
        try:
            if os.path.getsize(track_path) >= STUB_SIZE_THRESHOLD:
                return
        except OSError:
            pass
    # Another thread is already downloading this track
    if os.path.exists(dl_path):
        return

    with DOWNLOAD_SEM:
        # Re-check after acquiring semaphore
        if os.path.exists(err) or os.path.exists(dl_path):
            return
        if os.path.exists(done):
            try:
                if os.path.getsize(track_path) >= STUB_SIZE_THRESHOLD:
                    return
            except OSError:
                pass
        if not _has_disk_space():
            logging.warning('low disk space, skipping download track=%s', track_id)
            return
        try:
            track = TRACKS_CACHE.get(int(track_id))
            if not track:
                track = _retry_api(lambda: session.track(track_id=track_id), track_id, 'get_track')
                TRACKS_CACHE[int(track_id)] = track
            url = _retry_api(lambda: track.get_url(), track_id, 'get_url')
            Path(dl_path).touch()  # signal download in progress
            with requests.get(url, stream=True) as r:
                with open(dl_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            _tag_track(track, dl_path)
            os.rename(dl_path, track_path)  # atomic: stub (or nothing) → real file
            Path(done).touch()
            logging.info('downloaded track=%s', track_id)
        except Exception as e:
            logging.error('download failed track=%s: %s', track_id, e)
            try:
                os.remove(dl_path)
            except OSError:
                pass
            # Only mark error if no stub/file to fall back to; otherwise leave stub intact
            if not (os.path.exists(done) and os.path.exists(track_path)):
                Path(err).touch()


def _upgrade_stub(session, track_id, track_path):
    """Replace a stub with the real downloaded track (background)."""
    with _UPGRADING_LOCK:
        if track_id in _UPGRADING:
            return
        _UPGRADING.add(track_id)
    try:
        _download_track(session, track_id, track_path)
    finally:
        with _UPGRADING_LOCK:
            _UPGRADING.discard(track_id)


def _has_disk_space():
    st = os.statvfs(str(CACHE_PATH))
    return st.f_bavail * st.f_frsize >= MIN_FREE_BYTES


def _prefetch_tracks(session, tracks):
    # Only create stubs (tiny tagged M4A files). Real downloads happen lazily on playback.
    for track in tracks:
        track_path = str(CACHE_PATH / f'{track.id}.m4a')
        _create_stub(track, track_path)
    logging.warning('prefetch done: %d stubs ready', len(tracks))


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
                track_path = str(CACHE_PATH / path.split('/')[-1])
                if os.path.exists(track_path):
                    try:
                        base['st_size'] = os.path.getsize(track_path)
                    except OSError:
                        base['st_size'] = 50_000_000
                else:
                    base['st_size'] = 50_000_000
            else:
                base['st_mode'] = stat.S_IFLNK | 0o444
        elif path in LINKS_CACHE or '/Search/' in path:
            base['st_mode'] = stat.S_IFLNK | 0o444
        return base

    def readdir(self, path, fh):
        try:
            if path not in DIRS_CACHE:
                # Dedup concurrent fetches: only one thread fetches per path
                with _DIRS_FILL_LOCK:
                    if path not in DIRS_CACHE:
                        if path in _DIRS_FILLING:
                            event = _DIRS_FILLING[path]
                        else:
                            event = threading.Event()
                            _DIRS_FILLING[path] = event
                            event = None  # this thread owns the fetch
                if event is None:
                    try:
                        DIRS_CACHE[path] = get_entries_for_path(path, self.session, self.root)
                    finally:
                        with _DIRS_FILL_LOCK:
                            ev = _DIRS_FILLING.pop(path, None)
                        if ev:
                            ev.set()
                else:
                    event.wait()
            for i, name in enumerate(DIRS_CACHE.get(path, BASE_DIRS), 1):
                yield (name, {}, i)
        except Exception as e:
            logging.error('readdir failed %s: %s', path, e)
            yield ('.', {}, 1)
            yield ('..', {}, 2)

    def readlink(self, path):
        return LINKS_CACHE.get(path, path)

    def read(self, path, size, offset, fh):
        if not path.endswith('.m4a'):
            return b''
        filename = path.split('/')[-1]
        if filename.startswith('._'):
            return b''
        track_id = filename[:-4]
        track_path = str(CACHE_PATH / f'{track_id}.m4a')
        done = track_path + '.done'
        err = track_path + '.error'

        # If file is ready, serve it
        if os.path.exists(done) and not os.path.exists(err):
            try:
                file_size = os.path.getsize(track_path)
            except OSError:
                file_size = 0
            if file_size >= STUB_SIZE_THRESHOLD:
                # Real file — serve immediately
                with open(track_path, 'rb') as f:
                    f.seek(offset)
                    return f.read(size)
            elif file_size > 0:
                # Stub — serve immediately and trigger upgrade in background
                with _UPGRADING_LOCK:
                    is_upgrading = track_id in _UPGRADING
                if not is_upgrading:
                    threading.Thread(
                        target=_upgrade_stub,
                        args=(self.session, track_id, track_path),
                        daemon=True,
                    ).start()
                try:
                    with open(track_path, 'rb') as f:
                        f.seek(offset)
                        return f.read(size)
                except OSError:
                    pass
                # File gone (upgrade in progress) — fall through to wait

        # Not ready — trigger download and wait for done or error
        threading.Thread(
            target=_download_track,
            args=(self.session, track_id, track_path),
            daemon=True,
        ).start()
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
    CACHE_PATH.mkdir(parents=True, exist_ok=True)

    FUSE(Tidal(args.mount), args.mount, foreground=True, nothreads=False, allow_other=True)
