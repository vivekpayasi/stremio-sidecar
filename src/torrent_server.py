#!/usr/bin/env python3
"""Stremio-compatible torrent streaming server backed by libtorrent.

Stremio's own streaming server runs its torrent engine (a fork of torrent-stream) in JavaScript on a
single thread. On big torrents with many small pieces that thread saturates, and once it does it stops
feeding the player even while the download keeps going. This server answers the torrent routes of the
same HTTP API with libtorrent instead, and passes every other route (settings, transcoding, subtitles,
...) through to Stremio's server untouched. Point a client's Server URL here instead of at :12470.

Nothing is downloaded until a player asks for it: each reader gets hard deadlines on the pieces just
ahead of it and a read-ahead window beyond that. Downloaded pieces stay on disk, so rewinds are served
from disk; inactive torrents are deleted oldest-first once the cache exceeds its budget.
"""
import bisect
import collections
import http.client
import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import libtorrent as lt

from dashboard import PAGE as DASHBOARD_PAGE, Dashboard, cached_progress, last_tv_ip, ui_allowed, ui_write_allowed
from prefetch import Prefetcher
from storage import EXTERNAL_DIR, Places, allocate, covers, file_paths, move_folder, resume_progress
from stream_facts import FFPROBE, FactStore, Prober, ffprobe_runner
from subtitles import subtitle_file, video_hash

HOME = Path.home()
INSTALL_DIR = HOME / '.stremio-local-addon'

CONFIG = {
    'https_port': 12472,
    'http_port': 11472,
    'upstream': '127.0.0.1:11470',     # Stremio's own streaming server, for every non-torrent route
    'cert_file': str(HOME / 'Library/Application Support/stremio-server/httpsCert.json'),
    'cache_dir': str(INSTALL_DIR / 'torrent-cache'),
    'cache_budget_gb': 15,             # inactive torrents beyond this are deleted, oldest first
    'min_free_gb': 3,                  # ... and so is anything inactive while the disk is this full
    # An external drive for downloads for later, chosen on the dashboard (see storage.py): its mount point, what the
    # cache keeps on it, and whether downloads for later go there while it's connected ('external') or stay here ('mac').
    'external_volume': None,
    'external_budget_gb': 50,
    'downloads_to': 'external',
    'start_window_mb': 16,             # what a reader asks for before anything ahead of it has arrived
    'urgent_mb': 64,                   # just ahead of a reader: piece deadlines, spaced as below
    'deadline_rate_mbps': 48,          # deadlines assume this speed, so pieces arrive in reading order
    'readahead_mb': 384,               # most a reader's window grows to beyond the urgent part
    'idle_drop_s': 180,                # a torrent nobody has read for this long leaves the session
    'piece_wait_s': 120,               # a reader gives up on a piece after this long
    'listen_port': 6891,
    'upload_limit_kbps': 1024,         # KB/s; 0 = unlimited
    # The torrent comes in over the same Wi-Fi the TV stream goes out on (twice: Mac -> router -> TV).
    # Uncapped it can pull 100+ Mbps and crowd out the stream on a weak link. Stremio caps at ~64 Mbps;
    # 48 is still well above a 4K WEB-DL's bitrate. 0 = unlimited.
    'download_limit_mbps': 48,
    'max_connections': 300,
    # Hide MP4 subtitle tracks whose samples are stored far from the video they belong to (e.g. all at
    # the end of the file). ExoPlayer reads every track's samples, selected or not, so such a track
    # makes it jump across the file for every subtitle line and re-read the video — endless buffering.
    'hide_misplaced_mp4_subtitles': True,
    # Once a file has been played, read its header with ffprobe and record what it really contains (codecs,
    # Dolby Vision profile, bitrate) for the Stream Advisor. Delayed so the player's own startup reads go first.
    'learn_from_plays': True,
    'probe_delay_s': 20,
    'ffprobe': FFPROBE,
    'facts_file': str(INSTALL_DIR / 'stream-facts.json'),
    # The dashboard at /ui shows what is being watched, so by default only this Mac can open it. True lets
    # any device on the network (a phone) open it too.
    'dashboard_lan': False,
    'dashboard_tv_adb': True,          # read the TV's state over ADB for the dashboard, when the TV allows it
}
CONFIG_FILE = Path(os.environ.get('TORRENTD_CONFIG', INSTALL_DIR / 'torrentd.json'))   # optional overrides, same keys
if CONFIG_FILE.exists():
    CONFIG.update(json.loads(CONFIG_FILE.read_text()))

# Added to whatever the stream supplies (Torrentio usually supplies none), so a bare info hash still
# finds peers quickly. The same public list Stremio's own server searches.
DEFAULT_TRACKERS = [
    'udp://tracker.opentrackr.org:1337/announce', 'udp://open.stealth.si:80/announce',
    'udp://tracker.torrent.eu.org:451/announce', 'udp://open.demonii.com:1337/announce',
    'udp://tracker.qu.ax:6969/announce', 'udp://exodus.desync.com:6969/announce',
    'udp://tracker.tryhackx.org:6969/announce', 'udp://tracker.theoks.net:6969/announce',
    'udp://explodie.org:6969/announce', 'udp://retracker01-msk-virt.corbina.net:80/announce',
    'udp://tracker.bittor.pw:1337/announce', 'udp://tracker.gmi.gd:6969/announce',
    'udp://tracker.corpscorp.online:80/announce', 'udp://tracker.ducks.party:1984/announce',
    'udp://tracker-udp.gbitt.info:80/announce', 'udp://tracker.0x7c0.com:6969/announce',
    'udp://tracker.dler.org:6969/announce', 'http://tracker2.dler.org:80/announce',
    'http://tracker.dler.com:6969/announce',
]
PIECE_CACHE_BYTES = 96 * 1024 * 1024    # recently served pieces, per torrent
MOVE_IDLE_S = 60     # a torrent read this recently isn't moved to another place: the TV may be about to read it again

IH = r'[0-9a-fA-F]{40}'
# Stremio's own list, plus the Blu-ray/AVCHD and iTunes containers (.m2ts, .mts, .m4v) its loose regex happens to match.
MEDIA_EXT = re.compile(r'\.(mkv|avi|mp4|m4v|wmv|vp8|mov|mpg|ts|m2ts|mts|m3u8|webm|flac|mp3|wav|wma|aac|ogg)$', re.I)
# Third path segments that belong to Stremio's own HLS/transcoding routes, not to a torrent stream.
UPSTREAM_SUFFIX = re.compile(r'^(?:(?:hls|master|stream|stream-[^/]*|stream-q-[^/]*|subs-[^/]*|mp4stream-q-[^/]*)\.m3u8'
                             r'|thumb\.jpg|dlna)$')
HOP_BY_HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
              'trailers', 'transfer-encoding', 'upgrade'}
# Status routes the TV checks to decide whether the streaming server is "online". Their last good
# answers are kept, so torrents still play while Stremio desktop (the upstream) is closed.
REMEMBERED = ('/settings', '/network-info', '/device-info', '/casting')
REMEMBERED_FILE = INSTALL_DIR / 'upstream-cache.json'
CHUNK = 256 * 1024
MB = 1024 * 1024
mimetypes.add_type('video/x-matroska', '.mkv')


def layout(ti):
    """The torrent's file_storage: layout() since libtorrent 2.1 (files() is deprecated there)."""
    return ti.layout() if hasattr(ti, 'layout') else ti.files()


LOG_LOCK = threading.Lock()


def _used_at(d):
    """When a cached torrent was last read, by its folder's .last-used (0 if it never was)."""
    try:
        return float((Path(d) / '.last-used').read_text())
    except (OSError, ValueError):
        return 0.0


def log(*a):
    line = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z ' + ' '.join(str(x) for x in a)
    with LOG_LOCK:     # request threads log concurrently; keep lines whole
        print(line, flush=True)


SECRET_PARAM = re.compile(r'([?&][^=&\s]*(?:key|token)[^=&\s]*=)[^&\s]*', re.I)


def for_log(path):
    """A request path as torrent.log may keep it: the TV's /get-https carries the account's authKey."""
    return SECRET_PARAM.sub(r'\1…', path)


def human(n):
    return f'{n / MB:.1f}MB' if n < 1024 * MB else f'{n / 1024 / MB:.2f}GB'


_own = {'at': 0.0, 'ips': {'127.0.0.1', '::1'}}
_own_lock = threading.Lock()


def own_ips():
    """This Mac's addresses: requests from them are Stremio desktop's server, not the TV. Called on request
    threads, so it never blocks them: one thread refreshes (every minute) while the rest use the last answer."""
    if time.time() - _own['at'] > 60 and _own_lock.acquire(blocking=False):
        try:
            out = subprocess.run(['ifconfig'], capture_output=True, text=True, timeout=2).stdout
            ips = {'127.0.0.1', '::1'}
            for line in out.splitlines():
                m = re.match(r'\s+inet6? ([0-9a-f.:]+)', line)
                if m:
                    ips.add(m.group(1).split('%')[0])
            if len(ips) > 2:
                _own['ips'] = ips
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            _own['at'] = time.time()
            _own_lock.release()
    return _own['ips']


def _tail_log(size=512 * 1024):
    try:
        with open(INSTALL_DIR / 'torrent.log', 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - size))
            return f.read().decode(errors='replace')
    except OSError:
        return ''


def phone_url():
    """The dashboard's address for other devices: the same Stremio hostname the TV uses, so HTTPS just works."""
    try:
        domain = json.loads(Path(CONFIG['cert_file']).read_text())['domain']
    except (OSError, ValueError, KeyError):
        return None
    # The Wi-Fi or Ethernet address, like the installer uses — not a VPN's or a virtual machine bridge's.
    lan = None
    for iface in ('en0', 'en1'):
        try:
            lan = subprocess.run(['ipconfig', 'getifaddr', iface], capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            lan = None
        if lan:
            break
    if not lan:
        return None
    return f'https://{lan.replace(".", "-")}.{domain.split(".", 1)[1]}:{CONFIG["https_port"]}/ui'


def write_atomic(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(data if isinstance(data, bytes) else data.encode())
    os.replace(tmp, path)


def disk_usage(path):
    """Bytes actually allocated under path — files are sparse, so st_size would overstate it."""
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_blocks * 512
            except OSError:
                pass
    return total


# ------------------------------------------------------------------------------------------ files

def parse_season_episode(path):
    """(season, [episodes]) from a file path, or (None, [])."""
    name = path.replace('\\', '/')
    m = re.search(r'[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})(?:[ ._-]?[Ee]?(\d{1,3}))?', name)
    if m:
        eps = [int(m.group(2))] + ([int(m.group(3))] if m.group(3) else [])
        return int(m.group(1)), eps
    m = re.search(r'(?<![\d])(\d{1,2})x(\d{2,3})(?![\d])', name)
    if m:
        return int(m.group(1)), [int(m.group(2))]
    m = re.search(r'season[ ._-]*(\d{1,2}).*?(?:episode|ep|e)[ ._-]*(\d{1,3})', name, re.I)
    if m:
        return int(m.group(1)), [int(m.group(2))]
    return None, []


def guess_file_idx(files, series=None):
    """Port of Stremio's GuessFileIdx: the largest media file, preferring the requested episode."""
    media = [(i, f) for i, f in enumerate(files) if MEDIA_EXT.search(f['path'])]
    if not media:
        return -1
    if series and series.get('season') and series.get('episode'):
        want_s, want_e = int(series['season']), int(series['episode'])
        ep = [(i, f) for i, f in media if parse_season_episode(f['path']) [0] == want_s
              and want_e in parse_season_episode(f['path'])[1]]
        media = ep or media
    return max(media, key=lambda x: x[1]['length'])[0]


def file_must_include(files, patterns):
    """Index of the first file whose name matches one of the patterns ('/re/flags' or plain regex)."""
    regs = []
    for p in patterns or []:
        m = re.fullmatch(r'/(.*)/(\w*)', p or '')
        try:
            regs.append(re.compile(m.group(1), re.I if 'i' in m.group(2) else 0) if m else re.compile(p))
        except re.error:
            pass
    for i, f in enumerate(files):
        if any(r.search(f['name']) for r in regs):
            return i
    return None


def parse_sources(sources):
    """Stremio peer sources ('tracker:udp://...', 'dht:<hash>') -> tracker URLs. DHT is always on."""
    out = []
    for s in sources or []:
        if isinstance(s, str) and s.startswith('tracker:'):
            out.append(s[len('tracker:'):])
        elif isinstance(s, str) and re.match(r'^(udp|https?|wss?)://', s):
            out.append(s)
    return out


# ------------------------------------------------------------------------------------------- mp4

MP4_EXT = re.compile(r'\.(mp4|m4v|mov)$', re.I)
TEXT_HANDLERS = (b'text', b'sbtl', b'subt', b'clcp')
MISPLACED_BYTES = 64 * MB


def mp4_boxes(b, pos, end):
    """(type, start, header length, end) for each box in b[pos:end]."""
    while pos + 8 <= end:
        size, typ = struct.unpack_from('>I4s', b, pos)
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size, hdr = struct.unpack_from('>Q', b, pos + 8)[0], 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield typ, pos, hdr, pos + size
        pos += size


def mp4_child(b, start, end, path):
    for typ, s, h, e in mp4_boxes(b, start, end):
        if typ == path[0]:
            return (s, h, e) if len(path) == 1 else mp4_child(b, s + h, e, path[1:])
    return None


def mp4_track(b, s, h, e):
    """Handler type and [(chunk start time in s, file offset)] of one 'trak' box."""
    mdia = mp4_child(b, s + h, e, [b'mdia'])
    if not mdia:
        return None
    ms, mh, me = mdia
    hdlr, mdhd = mp4_child(b, ms + mh, me, [b'hdlr']), mp4_child(b, ms + mh, me, [b'mdhd'])
    stbl = mp4_child(b, ms + mh, me, [b'minf', b'stbl'])
    if not (hdlr and mdhd and stbl):
        return None
    handler = b[hdlr[0] + hdlr[1] + 8: hdlr[0] + hdlr[1] + 12]
    p = mdhd[0] + mdhd[1]
    timescale = struct.unpack_from('>I', b, p + 4 + (16 if b[p] == 1 else 8))[0] or 1
    bs, bh, be = stbl

    def table(name):
        box = mp4_child(b, bs + bh, be, [name])
        if not box:
            return None
        q = box[0] + box[1]
        return q + 8, struct.unpack_from('>I', b, q + 4)[0]

    stts, stsc = table(b'stts'), table(b'stsc')
    co, wide = table(b'stco'), False
    if co is None:
        co, wide = table(b'co64'), True
    if not (stts and stsc and co):
        return None
    offsets = [struct.unpack_from('>Q' if wide else '>I', b, co[0] + i * (8 if wide else 4))[0] for i in range(co[1])]
    runs = [struct.unpack_from('>III', b, stsc[0] + i * 12)[:2] for i in range(stsc[1])]
    deltas = [struct.unpack_from('>II', b, stts[0] + i * 8) for i in range(stts[1])]
    chunks, t, di, left = [], 0, 0, deltas[0][0] if deltas else 0
    for ci in range(len(offsets)):
        while len(runs) > 1 and runs[1][0] <= ci + 1:
            runs.pop(0)
        chunks.append((t / timescale, offsets[ci]))
        n = runs[0][1] if runs else 1
        while n > 0 and di < len(deltas):
            take = min(n, left)
            t += take * deltas[di][1]
            n -= take
            left -= take
            if left == 0:
                di += 1
                left = deltas[di][0] if di < len(deltas) else 0
    return {'handler': handler, 'box_start': s, 'chunks': chunks}


def find_misplaced_mp4_subtitles(read, size):
    """[(file offset of a 'trak' type field, description)] for subtitle tracks stored away from their
    video. read(offset, length) -> bytes or None."""
    pos = 0
    while pos + 8 <= size:
        hdr = read(pos, min(16, size - pos))
        if not hdr or len(hdr) < 8:
            return []
        box, typ = struct.unpack_from('>I4s', hdr, 0)
        hlen = 8
        if box == 1:
            box, hlen = struct.unpack_from('>Q', hdr, 8)[0], 16
        elif box == 0:
            box = size - pos
        if box < hlen:
            return []
        if typ == b'moov':
            if box > 256 * MB:
                return []
            moov = read(pos, box)
            if not moov:
                return []
            tracks = [mp4_track(moov, s, h, e) for t2, s, h, e in mp4_boxes(moov, hlen, len(moov)) if t2 == b'trak']
            tracks = [t for t in tracks if t]
            videos = [t for t in tracks if t['handler'] == b'vide' and t['chunks']]
            if not videos:
                return []
            video = max(videos, key=lambda t: len(t['chunks']))
            vt, vo = [c[0] for c in video['chunks']], [c[1] for c in video['chunks']]
            found = []
            for n, t in enumerate(tracks):
                if t['handler'] not in TEXT_HANDLERS or not t['chunks']:
                    continue
                far = sum(1 for tm, off in t['chunks']
                          if abs(off - vo[max(0, bisect.bisect_right(vt, tm) - 1)]) > MISPLACED_BYTES)
                if far > max(3, len(t['chunks']) // 10):
                    found.append((pos + t['box_start'] + 4,
                                  f'track {n + 1} ({t["handler"].decode(errors="replace")}, {far}/{len(t["chunks"])} '
                                  f'chunks stored >64 MB from their video)'))
            return found
        pos += box
    return []


# ----------------------------------------------------------------------------------------- engine

class Torrent:
    def __init__(self, engine, ih, handle, save_path):
        self.engine, self.ih, self.handle, self.save_path = engine, ih, handle, Path(save_path)
        self.cond = threading.Condition()
        self.ti = None
        self.readers = {}         # reader id -> {'at', 'last', 'released', 'hi', 'urgent_to', 'bg', 'short'}
        self.prio = {}            # piece -> priority we set (only pieces we raised above 0)
        self.deadlines = set()    # pieces we gave a deadline
        self.piece_cache = {}     # piece -> bytes, insertion-ordered (oldest first)
        self.piece_waits = {}     # piece -> [Event, bytes], for reads in flight
        self.cache_lock = threading.Lock()
        self.last_used = time.time()
        self.last_announce = 0.0
        self.dropping = False
        self.error = None         # set by file/torrent error alerts; readers fail fast while it is
        self.mp4_patches = {}     # file index -> [(file offset, 4 replacement bytes)]
        self.mp4_lock = threading.Lock()
        # For the dashboard, the TV's requests only: where it is reading each file, and how its requests went.
        self.positions = {}       # file index -> (byte offset, time)
        self.open_requests = collections.Counter()   # file index -> the TV's requests being served now (under lock)
        self.reading = collections.Counter()         # file index -> any client's requests being served now (under lock)
        self.requests = collections.deque(maxlen=80) # recorded when they start; 'sent' grows as bytes go out
        self.stalls = collections.deque(maxlen=40)   # a piece that kept the TV waiting, when it happened
        self.waiting = {}         # reader -> (file index, since, mid-stream?): pieces being waited for right now
        self.lock = threading.RLock()
        self.touch()
        if handle.status().has_metadata:
            # Resumed from the cache: the saved piece priorities would keep downloading with no reader.
            self.on_metadata()
            handle.prioritize_pieces([0] * self.ti.num_pieces())

    def touch(self):
        self.last_used = time.time()
        try:
            (self.save_path / '.last-used').write_text(str(int(self.last_used)))
        except OSError:
            pass

    def on_metadata(self):
        with self.cond:
            self.ti = self.handle.torrent_file()
            self.cond.notify_all()

    def wait_metadata(self, timeout):
        end = time.time() + timeout
        with self.cond:
            while self.ti is None and time.time() < end:
                self.cond.wait(min(1.0, end - time.time()))
                # Polled as well: metadata arriving before this torrent was registered has no alert left.
                if self.ti is None and self.handle.status().has_metadata:
                    self.ti = self.handle.torrent_file()
        return self.ti is not None

    def files(self):
        fs = layout(self.ti)
        return [{'name': fs.file_name(i), 'path': fs.file_path(i), 'length': fs.file_size(i),
                 'offset': fs.file_offset(i)} for i in range(fs.num_files())]

    def piece_range(self, idx):
        """The first and last piece of a file."""
        fs, plen = layout(self.ti), self.ti.piece_length()
        offset, size = fs.file_offset(idx), fs.file_size(idx)
        return offset // plen, (offset + max(size, 1) - 1) // plen

    def have_piece(self, p):
        return self.handle.have_piece(p)

    def ensure_file_wanted(self, idx):
        # Torrents are added with every file set to "don't download" so that metadata arriving cannot
        # start a full download. A file at priority 0 is stored in a part file, though, so the file being
        # read must be raised above 0. Changing a file priority makes libtorrent recompute every piece
        # priority in the torrent from the file priorities — later, on its disk thread — which would set
        # every piece of every opened file to 4 (a full download of each episode opened so far). So wait
        # for that recompute, then put back the complete vector of what readers actually want.
        with self.lock:
            if self.handle.file_priority(idx) != 0:
                return
            self.handle.file_priority(idx, 4)
            end = time.time() + 5
            while self.handle.get_file_priorities()[idx] == 0 and time.time() < end:
                time.sleep(0.02)
            self.handle.prioritize_pieces([self.prio.get(p, 0) for p in range(self.ti.num_pieces())])

    def read_bytes(self, idx, start, length, timeout=60):
        """File bytes [start, start+length), downloading what is missing; None on timeout."""
        fs, plen = layout(self.ti), self.ti.piece_length()
        foff, end = fs.file_offset(idx), start + length - 1
        first, last = (foff + start) // plen, (foff + end) // plen
        rid, out = object(), bytearray()
        self.ensure_file_wanted(idx)
        try:
            for p in range(first, last + 1):
                self.want(rid, p, last)
                if not self.wait_piece(p, timeout):
                    return None
                data = self.piece_data(p)
                if data is None:
                    return None
                out += data[max(0, foff + start - p * plen): min(len(data), foff + end + 1 - p * plen)]
        finally:
            self.release(rid)
        return bytes(out)

    def patches_for(self, idx, name, size):
        """Byte patches to apply when serving this file (computed once per file)."""
        if not CONFIG['hide_misplaced_mp4_subtitles'] or not MP4_EXT.search(name):
            return []
        with self.mp4_lock:
            if idx not in self.mp4_patches:
                patches = []
                try:
                    for off, why in find_misplaced_mp4_subtitles(lambda o, n: self.read_bytes(idx, o, n), size):
                        patches.append((off, b'free'))
                        log(f'mp4 {self.ih[:8]}/{idx}: hiding {why} — ExoPlayer would otherwise keep jumping to it')
                except Exception as e:
                    log(f'mp4 {self.ih[:8]}/{idx}: could not inspect ({type(e).__name__}: {e}); serving unchanged')
                self.mp4_patches[idx] = patches
            return self.mp4_patches[idx]

    def want(self, rid, piece, last_piece, background=False):
        """A reader is now at `piece` and will read no further than `last_piece`. A background reader is a download
        for later (prefetch.py): the lowest priority and no deadlines."""
        with self.lock:
            prev = self.readers.get(rid)
            if prev and prev['at'] == piece and prev['released'] is None:
                return
            fresh = not self.readers
            self.readers[rid] = {'at': piece, 'last': last_piece, 'released': None, 'hi': piece, 'urgent_to': piece,
                                 'bg': background, 'short': not background}
            self._apply()
            # With nothing wanted, our last announce told trackers we had nothing left to download,
            # so they answered with few or no seeds. Now that pieces are wanted, ask again.
            if fresh and time.time() - self.last_announce > 60:
                self.last_announce = time.time()
                self.handle.force_reannounce()
                self.handle.force_dht_announce()

    def release(self, rid):
        with self.lock:
            r = self.readers.get(rid)
            if r:
                # Kept briefly: players drop and reopen connections around the same spot all the time.
                r['released'] = time.time()
        self.touch()

    def piece_data(self, p, timeout=60):
        """A verified piece's bytes, through libtorrent. Reading the file directly is not safe:
        have_piece() turns true once a piece passes its hash check, which can be before the
        disk thread has written it out."""
        with self.cache_lock:
            data = self.piece_cache.get(p)
            if data is not None:
                return data
            slot = self.piece_waits.get(p)
            issue = slot is None
            if issue:
                slot = self.piece_waits[p] = [threading.Event(), None]
        if issue:
            self.handle.read_piece(p)
        if not slot[0].wait(timeout):
            with self.cache_lock:
                if self.piece_waits.get(p) is slot:
                    del self.piece_waits[p]
            return None
        return slot[1]

    def on_piece_read(self, p, data):
        with self.cache_lock:
            if data is not None:
                self.piece_cache[p] = data
                total = sum(len(v) for v in self.piece_cache.values())
                for old in list(self.piece_cache):
                    if total <= PIECE_CACHE_BYTES or old == p:
                        break
                    total -= len(self.piece_cache.pop(old))
            slot = self.piece_waits.pop(p, None)
        if slot:
            slot[1] = data
            slot[0].set()

    def expire_readers(self, linger=30):
        with self.lock:
            dead = [k for k, r in self.readers.items()
                    if r['released'] is not None and time.time() - r['released'] > linger]
            for k in dead:
                del self.readers[k]
            if dead or self.readers:
                self._apply()

    def live_readers(self):
        with self.lock:
            return sum(1 for r in self.readers.values() if r['released'] is None)

    def _apply(self):
        """Turn reader positions into piece priorities and deadlines.

        Each reader's window starts small and grows with the run of downloaded pieces right ahead of
        it. While the window is small, libtorrent has few pieces to choose from and duplicates block
        requests across peers (end game), so the first pieces arrive fast; spreading every peer over a
        large window from the start leaves the piece being read stuck behind a slow peer's blocks.
        Called whenever a reader moves and once a second, since a player with a full buffer stops
        moving while its window should keep growing."""
        plen = self.ti.piece_length()
        urgent_n = max(2, CONFIG['urgent_mb'] * MB // plen)
        start_n = max(2, CONFIG['start_window_mb'] * MB // plen)
        max_n = urgent_n + max(0, CONFIG['readahead_mb'] * MB // plen)
        spacing = max(50, plen * 8 * 1000 // (CONFIG['deadline_rate_mbps'] * 1_000_000))
        have = self.handle.status(lt.status_flags_t.query_pieces).pieces if self.readers else []
        want, urgent = {}, {}
        for r in self.readers.values():
            if r['released'] is not None:
                # A closed connection's window would keep downloading for a position the player has left
                # — after a few seeks that is several hundred MB competing with the one being watched.
                # It lingers only so the torrent counts as in use; a reconnect rebuilds its own window.
                continue
            at, last = r['at'], r['last']
            run = 0
            while at + run <= last and run < max_n and have[at + run]:
                run += 1
            if r['bg']:
                # A download for later: its whole window from the start, at the lowest priority and with no deadlines.
                # Sequential download picks pieces below priority 7 in file order, so this alone doesn't put a stream's
                # read-ahead first — only its urgent part (deadlines, 7). The download also stops altogether while a
                # stream is short of buffer (Engine.streams_short).
                r['hi'], r['urgent_to'] = min(last, at + max_n), at - 1
                for p in range(at, r['hi'] + 1):
                    want.setdefault(p, 1)
                continue
            # Short of buffer: less than half the read-ahead is in right ahead of the reader, with more of what it
            # asked for still to come. Downloads for later hold back meanwhile (Engine.streams_short).
            r['short'] = at + run <= last and run < max_n // 2
            hi = min(last, at + min(max_n, start_n + 2 * run))
            urgent_to = min(hi, at + urgent_n)
            r['hi'], r['urgent_to'] = hi, urgent_to
            for p in range(at, hi + 1):
                pr = 7 if p <= urgent_to else 5
                if want.get(p, 0) < pr:
                    want[p] = pr
            for p in range(at, urgent_to + 1):
                if not have[p]:
                    urgent[p] = min(urgent.get(p, 1 << 30), (p - at) * spacing)
        # reset_piece_deadline() leaves the piece at priority 1, so clear deadlines first and then set
        # every cleared piece's priority explicitly along with the rest.
        cleared = self.deadlines - urgent.keys()
        for p in cleared:
            self.handle.reset_piece_deadline(p)
        changes = {p: pr for p, pr in want.items() if self.prio.get(p) != pr}
        changes.update({p: 0 for p in self.prio if p not in want})
        changes.update({p: want.get(p, 0) for p in cleared})
        if changes:
            self.handle.prioritize_pieces(list(changes.items()))
            for p, pr in changes.items():
                if pr:
                    self.prio[p] = pr
                else:
                    self.prio.pop(p, None)
        for p, ms in urgent.items():
            if p not in self.deadlines:
                self.handle.set_piece_deadline(p, ms)
        self.deadlines = set(urgent)

    def wait_piece(self, p, timeout):
        if self.handle.have_piece(p):
            return True
        end = time.time() + timeout
        with self.cond:
            while not self.handle.have_piece(p):
                left = end - time.time()
                if left <= 0 or self.error:
                    return False
                self.cond.wait(min(1.0, left))
        return True

    def snapshot(self, idx):
        """One file as the dashboard needs it (see dashboard.summarize_session)."""
        fs, plen = layout(self.ti), self.ti.piece_length()
        size, offset = fs.file_size(idx), fs.file_offset(idx)
        first, last = self.piece_range(idx)
        s = self.handle.status(lt.status_flags_t.query_pieces)
        try:
            peers = self.handle.get_peer_info()
            sending = sum(1 for p in peers if not p.flags & lt.peer_info.remote_choked)
            seeds = sum(1 for p in peers if p.flags & lt.peer_info.seed)
        except Exception:
            peers, sending, seeds = [], 0, 0
        pos, at = self.positions.get(idx, (0, self.last_used))
        patches = self.mp4_patches.get(idx)
        now = time.time()
        waits = [(now - since, mid) for i, since, mid in list(self.waiting.values()) if i == idx]
        return {
            'ih': self.ih, 'idx': idx, 'torrent': self.ti.name(), 'file': fs.file_name(idx), 'size': size,
            'piece_len': plen, 'file_offset': offset, 'first_piece': first, 'pos_byte': pos, 'pos_at': at,
            'have': list(s.pieces[first:last + 1]), 'download_bps': s.download_payload_rate,
            'peers': {'connected': len(peers), 'sending': sending, 'seeds': seeds},
            'mp4_hidden': None if patches is None else len(patches),
            'live': self.open_requests[idx] > 0 or time.time() - at < 20,
            'requests': [dict(r) for r in list(self.requests) if r['idx'] == idx],
            'stalls': [st for st in list(self.stalls) if st['idx'] == idx],
            'waiting_s': round(max(w for w, _ in waits), 1) if waits else 0,
            'waiting_mid': any(mid for _, mid in waits),
        }

    def debug(self):
        """Where readers are, and what libtorrent is fetching right now."""
        q = self.handle.get_download_queue()
        partial = sorted((d['piece_index'], sum(1 for b in d['blocks'] if b['state'] == 3), len(d['blocks']),
                          len({str(b.get('peer')) for b in d['blocks'] if b['state'] in (1, 2)}))
                         for d in q)
        have = self.handle.status(lt.status_flags_t.query_pieces).pieces
        readers = []
        for r in list(self.readers.values()):
            missing = [i - r['at'] for i in range(r['at'], r['hi'] + 1) if not have[i]]
            readers.append({'at': r['at'], 'window': r['hi'] - r['at'] + 1, 'urgent': r['urgent_to'] - r['at'] + 1,
                            'missing_in_window': len(missing), 'first_missing': missing[:12],
                            'released': r['released'] is not None, 'background': r['bg']})
        peers = self.handle.get_peer_info()
        lt_prios = self.handle.get_piece_priorities()
        wanted = [i for i, v in enumerate(lt_prios) if v > 0 and not have[i]]
        return {
            'libtorrent_wants': {'missing_pieces_wanted': len(wanted),
                                 'outside_reader_windows': sum(1 for i in wanted if i not in self.prio)},
            'peers': {'connected': len(peers),
                      'sending_to_us': sum(1 for p in peers if not p.flags & lt.peer_info.remote_choked),
                      'we_are_interested': sum(1 for p in peers if p.flags & lt.peer_info.interesting),
                      'seeds': sum(1 for p in peers if p.flags & lt.peer_info.seed)},
            'readers': readers, 'deadlines': len(self.deadlines), 'prioritized': len(self.prio),
            'downloading': [{'piece': p, 'blocks_done': d, 'blocks': n, 'peers': k} for p, d, n, k in partial],
        }

    def stats(self, idx=None):
        """Same shape as Stremio's getStatistics. The Android TV app's core deserializes it strictly:
        every field below is required, counters must be integers, and the stream fields must be filled
        even when the requested file index is unknown."""
        s = self.handle.status()
        try:
            unchoked = sum(1 for p in self.handle.get_peer_info() if not p.flags & lt.peer_info.remote_choked)
        except Exception:
            unchoked = s.num_seeds
        files = self.files() if self.ti else []
        trackers = [t['url'] for t in self.handle.trackers()]
        out = {
            'infoHash': self.ih, 'name': (self.ti.name() if self.ti else s.name) or '',
            'files': files, 'sources': [],
            'opts': {'connections': CONFIG['max_connections'], 'dht': True, 'growler': {'flood': 0, 'pulse': 0},
                     'handshakeTimeout': 20000, 'path': str(self.save_path),
                     'peerSearch': {'min': 40, 'max': 150,
                                    'sources': [f'dht:{self.ih}'] + [f'tracker:{t}' for t in trackers]},
                     'swarmCap': {}, 'timeout': 4000, 'tracker': True, 'virtual': True,
                     'engine': 'libtorrent ' + lt.__version__},
            'downloadSpeed': float(s.download_payload_rate), 'uploadSpeed': float(s.upload_payload_rate),
            'downloaded': int(s.all_time_download), 'uploaded': int(s.all_time_upload),
            'unchoked': int(unchoked), 'peers': int(s.num_peers), 'queued': int(s.connect_candidates),
            'unique': int(s.list_peers), 'connectionTries': 0, 'peerSearchRunning': True,
            'swarmConnections': int(s.num_peers), 'swarmPaused': False, 'swarmSize': int(s.list_peers),
            'streamLen': 0, 'streamName': '', 'streamProgress': 0.0,
        }
        if self.ti:
            if idx is None or not 0 <= idx < len(files):
                idx = guess_file_idx(files, {})
            if 0 <= idx < len(files):
                size = files[idx]['length']
                done = self.handle.file_progress(lt.torrent_handle.piece_granularity)[idx]
                out.update(streamLen=size, streamName=files[idx]['name'],
                           streamProgress=float(done / size) if size else 0.0)
        return out


class Engine:
    def __init__(self):
        self.places = Places(CONFIG, CONFIG_FILE)
        self.cache = Path(CONFIG['cache_dir'])   # this Mac's: where a torrent the TV streams is cached, unless it is elsewhere
        self.cache.mkdir(parents=True, exist_ok=True)
        port = CONFIG['listen_port']
        self.ses = lt.session({
            'listen_interfaces': f'0.0.0.0:{port},[::]:{port}',
            'enable_dht': True, 'enable_lsd': True, 'enable_upnp': True, 'enable_natpmp': True,
            'dht_bootstrap_nodes': 'dht.libtorrent.org:25401,router.bittorrent.com:6881,'
                                   'router.utorrent.com:6881,dht.transmissionbt.com:6881',
            'alert_mask': (lt.alert_category.error | lt.alert_category.status
                           | lt.alert_category.storage | lt.alert_category.piece_progress),
            'alert_queue_size': 20000,
            'connections_limit': CONFIG['max_connections'],
            'upload_rate_limit': CONFIG['upload_limit_kbps'] * 1024,
            'download_rate_limit': CONFIG['download_limit_mbps'] * 1_000_000 // 8,
            # Between reads a torrent can have nothing left to download; by default libtorrent then
            # treats it as finished and drops its seeds, and the next seek waits for them to come back.
            'close_redundant_connections': False,
            'mixed_mode_algorithm': 0,          # prefer_tcp: don't throttle TCP peers in favour of uTP
            # All trackers share one tier, and by default only the first working one is announced to.
            'announce_to_all_trackers': True, 'announce_to_all_tiers': True,
            # Streaming needs the pieces right ahead of the reader first, with several peers sharing each
            # one. By default each peer takes a whole piece (or a whole 4 MiB extent) for itself, so with
            # big pieces the reader waits on whichever peer happens to be slowest.
            'piece_extent_affinity': False, 'whole_pieces_threshold': 0,
            # A block stuck at a slow peer holds up the piece the player is waiting for: give up on it
            # sooner, and let it be requested from a second peer instead of waiting it out.
            'piece_timeout': 5, 'request_timeout': 10, 'strict_end_game_mode': False,
            # Startup time is mostly how fast enough peers that will send to us are found; connect faster.
            'connection_speed': 100,
        })
        self.torrents = {}
        self.pinned = lambda: set()   # info hashes eviction must keep: those with a download for later to finish
        self.lock = threading.RLock()
        self.moving = set()           # info hashes whose folder is being moved to another place, or deleted
        self.aborts = set()           # ... whose move the TV wants stopped: it asked for the torrent
        self.moved = threading.Condition(self.lock)
        self.running = True
        self.last_resume_save = time.time()
        self.places_due = threading.Event()
        self.places.kick = self.places_due.set
        threading.Thread(target=self._alerts, name='alerts', daemon=True).start()
        threading.Thread(target=self._housekeeping, name='housekeeping', daemon=True).start()
        threading.Thread(target=self._watch_places, name='places', daemon=True).start()

    def get(self, ih, trackers=(), ti=None):
        """The torrent for an info hash, added to the session if needed (from the cache's resume data,
        a .torrent's info `ti`, or a magnet link)."""
        ih = ih.lower()
        with self.lock:
            self._wait_for_folder(ih)
            t = self.torrents.get(ih)
            if t and t.dropping:
                t.dropping = False    # wanted again before it left: the pending resume save just won't remove it
                log(f'torrent {ih[:8]}: wanted again, staying in the session')
            if t:
                for tr in trackers:
                    t.handle.add_tracker({'url': tr})
                t.touch()
                return t
            d = self.folder(ih)
            d.mkdir(parents=True, exist_ok=True)
            atp, resumed = None, False
            if (d / '.resume').exists():
                try:
                    atp, resumed = lt.read_resume_data((d / '.resume').read_bytes()), True
                except Exception as e:
                    log(f'torrent {ih[:8]}: resume data unreadable ({e}), starting from the magnet')
            if atp is None and ti is not None:
                atp = lt.add_torrent_params()
                atp.ti = ti
            if atp is None:
                atp = lt.parse_magnet_uri('magnet:?xt=urn:btih:' + ih)
            atp.save_path = str(d)
            atp.trackers = list(dict.fromkeys(list(atp.trackers) + list(trackers) + DEFAULT_TRACKERS))
            # default_dont_download on every add — resume data saved before metadata arrived carries no
            # file priorities, and would otherwise download the whole torrent. Sequential: the window
            # fills in order from the reader rather than rarest-first. Never paused, never auto-managed,
            # and never left in upload mode (libtorrent's reaction to a full disk, which it only undoes
            # for auto-managed torrents and which resume data would carry across restarts).
            atp.flags = ((atp.flags | lt.torrent_flags.default_dont_download | lt.torrent_flags.sequential_download)
                         & ~lt.torrent_flags.auto_managed & ~lt.torrent_flags.paused
                         & ~lt.torrent_flags.upload_mode)
            h = self.ses.add_torrent(atp)
            t = Torrent(self, ih, h, d)
            self.torrents[ih] = t
            log(f'torrent {ih[:8]}: added ({"resumed from cache" if resumed else ".torrent" if ti else "magnet"}, '
                f'{len(atp.trackers)} trackers)')
            return t

    def find(self, ih):
        return self.torrents.get(ih.lower())

    def _wait_for_folder(self, ih):
        """(Under self.lock.) While a torrent's folder is being moved or deleted, wait — a move is asked to stop, and
        the torrent is then read from where it was."""
        while ih in self.moving:
            self.aborts.add(ih)
            self.moved.wait()

    def folder(self, ih):
        """Where a torrent's files are: its folder in whichever place holds it, else a new one on this Mac."""
        return self.places.locate(ih) or self.cache / ih.lower()

    def place_ok(self, place_dir):
        p = self.places.at(place_dir)
        return bool(p and p['available'])

    def file_progress(self, ih):
        """[{name, length, done}] for each file of a torrent in the session, live from libtorrent."""
        t = self.torrents.get(ih)
        if not t or t.ti is None:
            return None
        try:
            done = t.handle.file_progress(lt.torrent_handle.piece_granularity)
        except RuntimeError:
            return None
        return [{'idx': i, 'name': f['name'], 'length': f['length'], 'done': round(done[i] / f['length'], 4) if f['length'] else 0}
                for i, f in enumerate(t.files())]

    def busy_files(self, ih):
        """Indexes of the files of a torrent that some client is reading right now."""
        t = self.torrents.get(ih)
        if not t:
            return set()
        with t.lock:
            return {i for i, n in t.reading.items() if n > 0}

    def streams_short(self):
        """Whether some stream is short of buffer right now (see Torrent._apply). Downloads for later wait until
        none is."""
        for t in list(self.torrents.values()):
            with t.lock:
                if any(r['released'] is None and not r['bg'] and r['short'] for r in t.readers.values()):
                    return True
        return False

    def disk_low(self, ih=None):
        """Whether the disk holding a torrent (this Mac's, for one not in the session) is down to the space always kept
        free. Downloads for later wait until it isn't."""
        t = self.torrents.get(ih) if ih else None
        try:
            return shutil.disk_usage(t.save_path if t else self.cache).free < CONFIG['min_free_gb'] * 1024 * MB
        except OSError:
            return True           # its drive went away

    def _leave(self, ih, reason, timeout=10):
        """Take a torrent nobody is reading out of the session (its state saved): whether it is out."""
        t = self.torrents.get(ih)
        if t:
            if t.live_readers() or self.busy_files(ih):
                return False
            self.drop(t, reason)
            end = time.time() + timeout
            while ih in self.torrents and time.time() < end:
                time.sleep(0.1)
        return ih not in self.torrents

    def offline(self, ih, fn, timeout=10):
        """Run fn() with the torrent out of the session and kept out — to edit its saved state on disk. A
        torrent nobody is reading is taken out first (its state saved); one being read is left alone (None)."""
        if not self._leave(ih, 'taken out to delete a file from the dashboard', timeout):
            return None
        with self.lock:          # get() takes this lock, so the torrent can't come back mid-edit
            return None if ih in self.torrents or ih in self.moving else fn()

    def prepare(self, ih, idx, place_dir, note=lambda message: None, stop=lambda: False):
        """Ready a torrent for one of its files to download for later into the place with folder `place_dir`: moved
        there if it is elsewhere and, on a disk that can't skip empty space, that file then written out to its full
        length (storage.py). Returns (ok, message), or None to be asked again later: while something reads the torrent
        or read it moments ago (a move would pull it from under the TV), or when the TV asked for it mid-move — the
        move stops and the torrent stays where it was. `note` hears of each step that takes a while; stop() is the
        download for later being cancelled."""
        self.places.refresh()          # here, off the streaming path: an up-to-date look before anything moves
        place = self.places.at(place_dir)
        if not place or not place['available']:
            return False, f'{place["name"] if place else Path(place_dir).parent.name} isn\'t connected'
        t = self.torrents.get(ih)
        src = t.save_path if t else self.folder(ih)
        at = self.places.place_of(src)
        here = at is not None and at['dir'] == place['dir']
        if here and not (src / f'.writing-out-{idx}').exists():     # a write-out the TV cut short is finished below
            return True, None
        if time.time() - (t.last_used if t is not None else _used_at(src)) < MOVE_IDLE_S:
            return None
        if not self._leave(ih, f'taken out to move it to {place["name"]}' if not here else 'taken out to write a file out'):
            return None
        with self.lock:
            if ih in self.torrents or ih in self.moving:
                return None
            self.moving.add(ih)
        halt = lambda: ih in self.aborts or stop()  # noqa: E731
        try:
            d = place['dir'] / ih
            if here:
                d = src
            elif d.is_dir() and covers(d, src):
                # The drive was away and the TV played the torrent again from here: the copy there has it all already.
                log(f'torrent {ih[:8]}: keeping the copy on {place["name"]}, which has all this Mac\'s has; deleting this Mac\'s')
                shutil.rmtree(src, ignore_errors=True)
            elif d.is_dir() and not covers(src, d):
                # Each copy holds pieces the other lacks (a download for later there, episodes played here): both stay,
                # and the download goes on in the copy the torrent plays from.
                log(f'torrent {ih[:8]}: copies here and on {place["name"]} each hold pieces the other lacks; both stay')
                return True, None
            else:
                log(f'torrent {ih[:8]}: moving it to {place["name"]} for a download for later')
                note(f'Moving it to {place["name"]}')
                ok, msg = move_folder(src, place['dir'], stop=halt)
                if not ok:
                    if msg == 'Stopped':
                        log(f'torrent {ih[:8]}: move to {place["name"]} stopped, it stays where it was')
                        return (False, 'Stopped') if stop() else None
                    return False, msg
            self.places.note(ih, place)
            files, paths = resume_progress(d / '.resume'), file_paths(d)
            f = paths[idx] if 0 <= idx < len(paths) else None
            length = files[idx]['length'] if 0 <= idx < len(files) else 0
            if not place['sparse'] and f is not None and length > 0:
                mark = d / f'.writing-out-{idx}'      # until it is written out: a write-out cut short is finished next time
                mark.touch()
                log(f'torrent {ih[:8]}: writing out {f.name} ({human(length)}) on {place["name"]}, which can\'t skip empty space')
                note(f'Setting aside {length / 1024 / MB:.1f} GB on {place["name"]}')
                if not allocate(f, length, stop=halt):
                    return (False, 'Stopped') if stop() else None
                mark.unlink(missing_ok=True)
            return True, None
        except OSError as e:
            return False, f'Could not write the file out on {place["name"]}: {e.strerror or e}'
        finally:
            with self.lock:
                self.moving.discard(ih)
                self.aborts.discard(ih)
                self.moved.notify_all()

    def _delete_folder(self, d):
        """Delete a cached torrent's folder, unless the torrent is (back) in the session or being moved — checked under
        the lock, and get() waits until the folder is gone. Whether it was deleted."""
        ih = d.name
        with self.lock:
            if ih in self.torrents or ih in self.moving:
                return False
            self.moving.add(ih)
        try:
            shutil.rmtree(d, ignore_errors=True)
        finally:
            with self.lock:
                self.moving.discard(ih)
                self.aborts.discard(ih)
                self.moved.notify_all()
        return True

    def dashboard_sessions(self):
        """Snapshots of every file the TV has read in a torrent still in the session."""
        out = []
        for t in list(self.torrents.values()):
            if t.ti is None or t.dropping:
                continue
            # The files read in the last two hours, at most four per torrent (a season pack can have dozens).
            recent = sorted(((at, i) for i, (_, at) in list(t.positions.items()) if time.time() - at < 7200), reverse=True)
            for _, idx in recent[:4]:
                try:
                    out.append(t.snapshot(idx))
                except Exception as e:
                    log(f'dashboard: snapshot of {t.ih[:8]}/{idx} failed: {e}')
        return out

    def drop(self, t, reason, only_if_idle=False):
        with self.lock:
            if t.dropping or (only_if_idle and (t.live_readers()
                                                or time.time() - t.last_used <= CONFIG['idle_drop_s'])):
                return
            t.dropping = True
            log(f'torrent {t.ih[:8]}: leaving the session ({reason}); downloaded pieces stay cached')
            if t.ti is None:
                # No metadata yet: resume data would hold neither the info dict nor file priorities.
                return self._remove(t)
            try:
                t.handle.save_resume_data(lt.torrent_handle.save_info_dict)   # removed once it is written
            except Exception:
                self._remove(t)

    def _remove(self, t):
        """Take a dropping torrent out of the session, unless it was wanted again in the meantime."""
        with self.lock:
            if not t.dropping or self.torrents.get(t.ih) is not t:
                return
            del self.torrents[t.ih]
            try:
                self.ses.remove_torrent(t.handle)
            except Exception:
                pass

    def _alerts(self):
        while self.running:
            self.ses.wait_for_alert(1000)
            for a in self.ses.pop_alerts():
                try:
                    self._on_alert(a)
                except Exception as e:
                    log('alert handling failed:', type(a).__name__, e)

    def _on_alert(self, a):
        h = getattr(a, 'handle', None)
        t = self.torrents.get(str(h.info_hash())) if h is not None and h.is_valid() else None
        if isinstance(a, lt.piece_finished_alert):
            if t:
                with t.cond:
                    t.cond.notify_all()
        elif isinstance(a, lt.read_piece_alert):
            if t:
                t.on_piece_read(a.piece, None if a.error.value() else bytes(a.buffer))
        elif isinstance(a, lt.metadata_received_alert):
            if t:
                t.on_metadata()
                log(f'torrent {t.ih[:8]}: metadata received — {t.ti.name()} '
                    f'({t.ti.num_files()} files, {t.ti.num_pieces()} pieces of {human(t.ti.piece_length())})')
        elif isinstance(a, lt.save_resume_data_alert):
            ih = str(a.handle.info_hash())
            t = self.torrents.get(ih)
            try:
                write_atomic((t.save_path if t else self.folder(ih)) / '.resume', lt.write_resume_data_buf(a.params))
            except Exception as e:
                log(f'torrent {ih[:8]}: could not write resume data: {e}')
            if t:
                self._remove(t)        # no-op unless it is dropping
        elif isinstance(a, lt.save_resume_data_failed_alert):
            if t:
                self._remove(t)
        elif isinstance(a, (lt.file_error_alert, lt.torrent_error_alert)):
            # Disk full or another I/O error: libtorrent pauses the torrent or puts it in upload mode.
            # Readers give up at once instead of waiting out their timeout; housekeeping retries.
            log('libtorrent:', a.message())
            if t:
                t.error = a.message()
                with t.cond:
                    t.cond.notify_all()
        elif isinstance(a, (lt.metadata_failed_alert, lt.listen_failed_alert, lt.fastresume_rejected_alert)):
            log('libtorrent:', a.message())

    def _housekeeping(self):
        tick = 0
        while self.running:
            time.sleep(1)
            tick += 1
            for t in list(self.torrents.values()):
                if t.readers and t.ti:
                    try:
                        t.expire_readers()
                    except Exception as e:
                        log(f'torrent {t.ih[:8]}: window refresh failed: {e}')
            if tick % 5:
                continue
            try:
                now = time.time()
                for t in list(self.torrents.values()):
                    if t.live_readers():
                        t.touch()      # 'last used' stays current during a long read (the dashboard's in-use check)
                    self.drop(t, f'unused for {CONFIG["idle_drop_s"]}s', only_if_idle=True)
                if now - self.last_resume_save > 300:
                    self.last_resume_save = now
                    for t in list(self.torrents.values()):
                        if t.ti and t.handle.need_save_resume_data():
                            t.handle.save_resume_data(lt.torrent_handle.save_info_dict)
                self._evict()
                self._recover()
            except Exception as e:
                log('housekeeping failed:', e)

    def _watch_places(self):
        """Every 5 s (or when a setting changed), on a thread of its own: look at the places — a drive can block for a
        while, and streaming must not wait on it — let go of the torrents on a drive that went, and keep the drive
        within its budget."""
        while self.running:
            try:
                self.places.refresh()
                self._drop_unreachable()
                self._evict(drives=True)
            except Exception as e:
                log('places failed:', e)
            self.places_due.wait(5)
            self.places_due.clear()

    def _drop_unreachable(self):
        """Let go of the torrents on a drive that was disconnected: played again, they start over on this Mac."""
        for t in list(self.torrents.values()):
            if not t.dropping and self.places.place_of(t.save_path) is None:
                drive = t.save_path.parent.parent.name if t.save_path.parent.name == EXTERNAL_DIR else 'its drive'
                self.drop(t, f'{drive} was disconnected')

    def _recover(self):
        """Retry torrents stopped by a disk error once there is enough free space again."""
        min_free = CONFIG['min_free_gb'] * 1024 * MB
        for t in list(self.torrents.values()):
            stuck = t.error or (t.handle.flags() & lt.torrent_flags.upload_mode)
            if not stuck:
                continue
            try:
                free = shutil.disk_usage(t.save_path).free
            except OSError:
                continue          # its drive went away: _drop_unreachable lets go of it
            if free < min_free:
                if time.time() - getattr(t, 'warned_full', 0) > 300:
                    t.warned_full = time.time()
                    log(f'torrent {t.ih[:8]}: stopped ({t.error or "upload mode"}); only {human(free)} free — '
                        f'nothing inactive left to delete. Free up disk space or lower cache_budget_gb.')
                continue
            t.handle.clear_error()
            t.handle.unset_flags(lt.torrent_flags.upload_mode)
            t.handle.resume()
            log(f'torrent {t.ih[:8]}: resumed after "{t.error or "upload mode"}" ({human(free)} free)')
            t.error = None

    def _evict(self, drives=False):
        """Keep each place within its budget: this Mac's from housekeeping, the drive's (and copies in both places)
        from the places thread, so a slow drive never holds up streaming. Never what is in the session, still
        downloading for later, or on its way to another place."""
        keep = set(self.torrents) | self.pinned() | self.moving
        places = self.places.available()
        if drives and len(places) > 1:
            self._dedupe(places, keep)
        for place in places:
            if (place['id'] != 'mac') == drives:
                self._evict_place(place, keep)

    def _dedupe(self, places, keep):
        """A torrent with a folder in both places (the drive was away, and the TV played it again from this Mac): a
        copy goes once the one it plays from holds every piece it holds. Copies that each hold pieces the other lacks —
        a download for later on the drive, other episodes played here — both stay, for the budgets to settle."""
        try:
            names = {d.name for d in places[0]['dir'].iterdir() if re.fullmatch(IH, d.name)} - keep
        except OSError:
            return
        for ih in names:
            copies = self.places.copies(ih)
            best = self.places.locate(ih)
            if len(copies) < 2 or best is None:
                continue
            for p, d in copies:
                if d != best and covers(best, d) and self._delete_folder(d):
                    log(f'cache: deleted {ih[:8]} on {p["name"]}, a copy with nothing the one in '
                        f'{best.parent.parent.name if best.parent.name == EXTERNAL_DIR else "this Mac"} lacks')

    def _evict_place(self, place, keep):
        budget = place['budget']
        min_free = CONFIG['min_free_gb'] * 1024 * MB
        where = '' if place['id'] == 'mac' else f' on {place["name"]}'
        entries = []
        try:
            folders = list(place['dir'].iterdir())
        except OSError:
            return
        for d in folders:
            if d.name.endswith('.moving') and d.name[:-len('.moving')] not in self.moving:
                shutil.rmtree(d, ignore_errors=True)      # a move cut short by a restart
                continue
            if not d.is_dir() or not re.fullmatch(IH, d.name):
                continue
            try:
                used = float((d / '.last-used').read_text())
            except (OSError, ValueError):
                used = d.stat().st_mtime
            entries.append((used, d))
        sizes = {d: disk_usage(d) for _, d in entries}
        total = sum(sizes.values())
        free = shutil.disk_usage(place['dir']).free
        for used, d in sorted(entries):
            if total <= budget and free >= min_free:
                break
            if d.name in keep or not self._delete_folder(d):
                continue
            total -= sizes[d]
            free += sizes[d]
            log(f'cache: deleted {d.name[:8]}{where} ({human(sizes[d])}, last used '
                f'{time.strftime("%b %d %H:%M", time.localtime(used))}) — cache {human(total)}, free {human(free)}')

    def health(self):
        return {
            'ok': True, 'engine': 'libtorrent ' + lt.__version__,
            'torrents': [{
                'infoHash': t.ih, 'name': t.ti.name() if t.ti else None, 'readers': t.live_readers(),
                'peers': (s := t.handle.status()).num_peers, 'downloadSpeed': s.download_payload_rate,
                'uploadSpeed': s.upload_payload_rate,
            } for t in list(self.torrents.values())],
            'cache': {'dir': str(self.cache), 'bytes': disk_usage(self.cache),
                      'budget': CONFIG['cache_budget_gb'] * 1024 * MB,
                      'free': shutil.disk_usage(self.cache).free},
            'places': [{'id': p['id'], 'name': p['name'], 'dir': str(p['dir']), 'budget': p['budget'],
                        'available': p['available'], 'why': p['why'], 'sparse': p['sparse']} for p in self.places.all()],
            'downloads_to': self.places.destination()['id'],
        }

    def shutdown(self):
        pending = [t for t in self.torrents.values() if t.ti and not t.dropping]
        for t in pending:
            self.drop(t, 'shutting down')
        end = time.time() + 5
        while self.torrents and time.time() < end:
            time.sleep(0.1)
        self.running = False


# ------------------------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'stremio-libtorrent'
    timeout = 300
    engine = None   # set at startup
    prober = None   # set at startup when ffprobe is available
    dashboard = None
    tv_ip = None    # the device streaming from us, else the last one that polled us like a Stremio client does
    tv_streamed_at = 0.0

    def log_message(self, fmt, *args):
        pass

    def log_request(self, code='-', size='-'):
        # Every request from another device (the TV), so a client problem shows up in torrent.log. The
        # dashboard's own polling (from a phone) would drown it out.
        if not self.client_address[0].startswith(('127.', '::1')) and not self.path.startswith('/ui/api/'):
            log(f'{self.client_address[0]} {self.command} {for_log(self.path)[:160]} -> {code}')

    def end_headers(self):
        super().end_headers()
        self.headers_sent = True

    def do_GET(self):
        self.route()

    do_POST = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = do_GET

    def route(self):
        u = urllib.parse.urlsplit(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        self.t0, self.sent, self.headers_sent = time.time(), 0, False
        try:
            if path.startswith('/ui') and self.command == 'OPTIONS':
                # A cross-origin preflight: the dashboard never allows one (and it must not reach Stremio's server).
                return self.send_bytes(403, 'text/plain', b'')
            if path in ('/ui', '/ui/', '/ui/api/state'):
                return self.ui(path)
            if path in self.UI_CHANGES and self.command == 'POST':
                return self.ui_change(path)
            # Another device running Stremio (a phone) polls like the TV does; the one streaming wins for 10 min.
            if ((path.endswith('/stats.json') or path in REMEMBERED) and time.time() - Handler.tv_streamed_at > 600
                    and self.client_address[0] not in own_ips()):
                Handler.tv_ip = self.client_address[0]
            if path == '/torrentd/health':
                return self.json(self.engine.health())
            if path == '/torrentd/cached':
                # For the advisor on this Mac: what is already downloaded says what has been watched.
                if not self.client_address[0].startswith('127.'):
                    return self.send_bytes(403, 'text/plain', b'')
                return self.send_bytes(200, 'application/json', json.dumps(cached_progress(
                    [p['dir'] for p in self.engine.places.available()], set(self.engine.torrents),
                    self.engine.file_progress)).encode())
            m = re.fullmatch(rf'/torrentd/debug/({IH})', path)
            if m:
                t = self.engine.find(m.group(1))
                return self.json(t.debug() if t else None)
            m = re.fullmatch(rf'/torrentd/probe/({IH})/(\d+)', path)
            if m:
                return self.probe(m.group(1).lower(), int(m.group(2)), (q.get('name') or [''])[0])
            if path == '/torrentd/facts':
                return self.json(self.prober.store.all() if self.prober else None)
            if self.command == 'OPTIONS' and re.match(rf'^/{IH}(/|$)', path):
                return self.cors_preflight()
            m = re.fullmatch(rf'/({IH})/create', path)
            if m:
                return self.create(m.group(1), self.body_json())
            if path == '/create':
                return self.create_from_file(self.body_json())
            m = re.fullmatch(rf'/({IH})/stats\.json', path)
            if m:
                t = self.engine.find(m.group(1))
                return self.json(t.stats() if t else None)
            m = re.fullmatch(rf'/({IH})/([^/]+)/stats\.json', path)
            if m:
                t = self.engine.find(m.group(1))
                idx = int(m.group(2)) if re.fullmatch(r'-?\d+', m.group(2)) else None
                return self.json(t.stats(idx) if t else None)
            if path == '/stats.json':
                return self.json({t.ih: t.stats() for t in list(self.engine.torrents.values())})
            m = re.fullmatch(rf'/({IH})/remove', path)
            if m:
                t = self.engine.find(m.group(1))
                if t:
                    self.engine.drop(t, 'removed by client')
                return self.json({})
            if path == '/removeAll':
                for t in list(self.engine.torrents.values()):
                    self.engine.drop(t, 'removed by client')
                return self.json({})
            m = re.fullmatch(rf'/({IH})/([^/]+)(?:/([^/]*))?', path)
            if m and self.command in ('GET', 'HEAD') and not (m.group(3) and UPSTREAM_SUFFIX.match(m.group(3))):
                return self.stream(m.group(1), urllib.parse.unquote(m.group(2)), q)
            return self.proxy()
        except OSError:
            self.close_connection = True        # the client went away (broken pipe, reset, TLS, timeout)
        except Exception as e:
            log(f'{self.command} {for_log(self.path)} failed: {type(e).__name__}: {e}\n' + traceback.format_exc().rstrip())
            self.close_connection = True
            if not self.headers_sent:           # otherwise an error page would land inside the body
                try:
                    self.send_error(500)
                except Exception:
                    pass

    # ---- helpers

    def read_body(self):
        """The request body, Content-Length or chunked — always consumed, so keep-alive stays in sync."""
        if 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower():
            out = bytearray()
            while True:
                size = int(self.rfile.readline().split(b';')[0].strip() or b'0', 16)
                if size == 0:
                    while self.rfile.readline() not in (b'\r\n', b'\n', b''):
                        pass                     # trailers
                    return bytes(out)
                out += self.rfile.read(size)
                self.rfile.readline()
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else b''

    def body_json(self):
        raw = self.read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode(errors='replace')).items()}

    def cors(self):
        if self.headers.get('Origin'):
            self.send_header('Access-Control-Allow-Origin', '*')

    def cors_preflight(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', self.headers.get('Access-Control-Request-Headers') or 'Range')
        self.send_header('Access-Control-Max-Age', '1728000')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.cors()
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    # ---- dashboard

    UI_CHANGES = ('/ui/api/cache/delete', '/ui/api/cache/clear', '/ui/api/cache/delete-file', '/ui/api/prefetch',
                  '/ui/api/prefetch/cancel', '/ui/api/storage')

    @staticmethod
    def cert_suffix():
        try:
            return '.' + json.loads(Path(CONFIG['cert_file']).read_text())['domain'].split('.', 1)[1]
        except (OSError, ValueError, KeyError, IndexError):
            return None

    def ui_change(self, path):
        body = self.body_json()
        if not ui_write_allowed(self.client_address[0], self.headers.get('Host'), self.headers.get('Sec-Fetch-Site'),
                                self.headers.get('X-Dashboard'), CONFIG['dashboard_lan'], own_ips(), self.cert_suffix()):
            return self.send_bytes(403, 'application/json', b'{"ok": false, "message": "Not allowed"}')
        if not self.dashboard:
            return self.send_bytes(503, 'application/json', b'{"ok": false, "message": "dashboard not running"}')
        d, ih, idx = self.dashboard, body.get('ih'), body.get('idx')
        out = {'/ui/api/cache/delete': lambda: d.delete(ih), '/ui/api/cache/clear': d.clear,
               '/ui/api/cache/delete-file': lambda: d.delete_file(ih, idx),
               '/ui/api/prefetch': lambda: d.prefetch(ih, idx),
               '/ui/api/prefetch/cancel': lambda: d.cancel_prefetch(ih, idx),
               '/ui/api/storage': lambda: d.configure(body)}[path]()
        return self.send_bytes(200, 'application/json; charset=utf-8', json.dumps(out).encode())

    def ui(self, path):
        suffix = self.cert_suffix()
        if not ui_allowed(self.client_address[0], self.headers.get('Host'), self.headers.get('Sec-Fetch-Site'),
                          CONFIG['dashboard_lan'], own_ips(), suffix):
            body = ('<!doctype html><meta charset="utf-8"><title>Dashboard</title><p>The dashboard only opens on the Mac '
                    'running the server. To open it from other devices on your network, add <code>"dashboard_lan": true'
                    '</code> to ~/.stremio-local-addon/torrentd.json and restart the torrent server.').encode()
            return self.send_bytes(403, 'text/html; charset=utf-8', body)
        if path == '/ui/api/state':
            # Not self.json(): that would add a CORS header and let any web page read what is being watched.
            if not self.dashboard:
                return self.send_bytes(503, 'application/json', b'{"error": "dashboard not running"}')
            return self.send_bytes(200, 'application/json; charset=utf-8', json.dumps(self.dashboard.state()).encode())
        try:
            page = DASHBOARD_PAGE.read_bytes()
        except OSError:
            return self.send_bytes(404, 'text/plain', b'dashboard.html is missing - reinstall')
        return self.send_bytes(200, 'text/html; charset=utf-8', page, {
            'Content-Security-Policy': "default-src 'self'; img-src 'self' https://images.metahub.space; "
                                       "style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'",
            'Referrer-Policy': 'no-referrer'})

    def send_bytes(self, status, ctype, body, headers=None):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    # ---- torrent routes

    def mp4_hidden(self, ih, idx):
        """How many subtitle tracks the server hides in this MP4 (see patches_for)."""
        t = self.engine.find(ih)
        return {'mp4SubsHidden': len(t.mp4_patches.get(idx, [])) if t else 0}

    def probe(self, ih, idx, name):
        """Ask for a file's facts before anyone plays it (the advisor's optional background probe)."""
        if not self.prober:
            return self.json({'error': 'ffprobe not available'}, 503)
        known = self.prober.store.get(f'{ih}:{idx}') is not None
        queued = not known and self.prober.schedule(ih, idx, name, extra=lambda: self.mp4_hidden(ih, idx))
        return self.json({'known': known, 'queued': queued})

    def create(self, ih, body):
        sources = parse_sources((body.get('peerSearch') or {}).get('sources'))
        torrent = body.get('torrent') or body.get('stream') or {}
        if isinstance(torrent, dict):
            sources += [a for a in torrent.get('announce') or [] if isinstance(a, str)]
        t = self.engine.get(ih, sources)
        if not t.wait_metadata(60):
            log(f'create {ih[:8]}: no metadata after 60s')
            return self.json(t.stats())
        st = t.stats()
        idx = file_must_include(st['files'], body.get('fileMustInclude'))
        if idx is not None:
            st['guessedFileIdx'] = idx
        elif isinstance(body.get('guessFileIdx'), dict):
            st['guessedFileIdx'] = guess_file_idx(st['files'], body['guessFileIdx'])
        log(f'create {ih[:8]}: {t.ti.name()} ({len(st["files"])} files)'
            + (f', guessed file {st["guessedFileIdx"]}' if 'guessedFileIdx' in st else ''))
        return self.json(st)

    def create_from_file(self, body):
        blob, src = body.get('blob'), body.get('from')
        if isinstance(blob, str):
            data = bytes.fromhex(blob)
        elif isinstance(src, str) and src.startswith('http'):
            # python.org builds ship without a CA bundle; curl uses the system trust store.
            data = subprocess.run(['curl', '-sfL', '-m', '30', src], capture_output=True, check=True).stdout
        elif isinstance(src, str):
            data = Path(src).read_bytes()
        else:
            return self.json({'error': 'no torrent'}, 500)
        ti = lt.torrent_info(lt.bdecode(data))
        trackers = [t['url'] if isinstance(t, dict) else t.url for t in ti.trackers()]
        t = self.engine.get(str(ti.info_hash()), trackers, ti=ti)
        t.wait_metadata(5)
        return self.json(t.stats())

    def stream(self, ih, idx_raw, q):
        t = self.engine.get(ih, parse_sources(q.get('tr')))
        if not t.wait_metadata(60):
            self.close_connection = True
            return self.send_error(504, 'no torrent metadata')
        files = t.files()
        idx = int(idx_raw) if re.fullmatch(r'-?\d+', idx_raw) else None
        must = file_must_include(files, q.get('f'))
        if must is not None:
            idx = must
        elif idx is None:
            idx = next((i for i, f in enumerate(files) if f['name'] == idx_raw), None)
            if idx is None:
                return self.send_error(500, 'invalid file index or name')
        elif idx == -1:
            idx = guess_file_idx(files, {})
        if not 0 <= idx < len(files):
            return self.send_error(500, f'torrent has no file {idx}')
        f = files[idx]
        if q.get('external'):
            self.send_response(307)
            self.send_header('Location', f'/{ih}/{urllib.parse.quote(f["name"])}'
                             + ('?download=1' if q.get('download') else ''))
            self.send_header('Content-Length', '0')
            return self.end_headers()
        if self.prober and CONFIG['learn_from_plays']:
            # A no-op once the file is known; ffprobe's own requests come back through here too.
            try:
                self.prober.schedule(ih, idx, f['name'], aliases=[f'{ih.lower()}:-1'] if idx_raw == '-1' else (),
                                     delay=CONFIG['probe_delay_s'], extra=lambda: self.mp4_hidden(ih, idx))
            except Exception as e:      # learning is optional; playback is not
                log(f'facts {ih[:8]}/{idx}: could not schedule a probe — {type(e).__name__}: {e}')

        size = f['length']
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        if rng:
            m = re.fullmatch(r'bytes=(\d*)-(\d*)', rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                m = None
            elif not m.group(1):
                start = max(0, size - int(m.group(2)))
            else:
                start = int(m.group(1))
                end = min(size - 1, int(m.group(2))) if m.group(2) else size - 1
            if m is None or start > end or start >= size:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                return self.end_headers()
        self.send_response(206 if rng else 200)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Type', mimetypes.guess_type(f['name'])[0] or 'application/octet-stream')
        self.send_header('Cache-Control', 'max-age=0, no-cache')
        self.send_header('Content-Length', str(end - start + 1))
        if rng:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('transferMode.dlna.org', 'Streaming')
        self.send_header('contentFeatures.dlna.org', 'DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000')
        if q.get('download'):
            ascii_name = f['name'].encode('ascii', 'replace').decode().replace('"', "'")
            self.send_header('Content-Disposition', f'attachment; filename="{ascii_name}"; '
                             f"filename*=UTF-8''{urllib.parse.quote(f['name'])}")
        self.cors()
        self.end_headers()
        if self.command == 'HEAD':
            return
        player = self.client_address[0] not in own_ips()      # the TV, not ffprobe or Stremio desktop
        index_read = size > 256 * MB and start > size - 64 * MB   # the player reading the file's index, not playing
        record = None
        if player:
            Handler.tv_ip, Handler.tv_streamed_at = self.client_address[0], time.time()
            with t.lock:
                t.open_requests[idx] += 1
            if not index_read or idx not in t.positions:
                t.positions[idx] = (0 if index_read else start, time.time())
            record = {'at': round(self.t0, 2), 'idx': idx, 'start': start, 'sent': 0, 'took': 0, 'wait': 0, 'write': 0, 'ok': None}
            t.requests.append(record)
        with t.lock:
            t.reading[idx] += 1
        try:
            ok = self.send_range(t, idx, start, end, t.patches_for(idx, f['name'], size),
                                 track=player and not index_read, record=record)
        finally:
            with t.lock:
                t.reading[idx] -= 1
                if player:
                    t.open_requests[idx] -= 1
        took = time.time() - self.t0
        tm = getattr(self, 'timing', {})
        if record:
            record.update(sent=self.sent, took=round(took, 2), wait=round(tm.get('wait', 0), 2),
                          write=round(tm.get('write', 0), 2), ok=ok)
        log(f'{self.command} {ih[:8]}/{idx} {rng or "full"} -> {human(self.sent)} in {took:.1f}s'
            + (f' (waiting for pieces {tm["wait"]:.1f}s, reading {tm["read"]:.1f}s, sending {tm["write"]:.1f}s)' if tm else '')
            + ('' if ok else ' (ended early)'))
        if not ok:
            self.close_connection = True

    def send_range(self, t, idx, start, end, patches=(), track=False, record=None):
        """Write file bytes [start, end] as their pieces arrive. False if the reader stopped early. With
        `track`, the position is kept for the dashboard as the bytes go out."""
        fs = layout(t.ti)
        plen = t.ti.piece_length()
        foff = fs.file_offset(idx)
        last_piece = (foff + end) // plen
        rid = object()
        t.ensure_file_wanted(idx)
        pos = start
        self.timing = timing = {'wait': 0.0, 'read': 0.0, 'write': 0.0}
        try:
            while pos <= end:
                piece = (foff + pos) // plen
                t0 = time.time()
                t.want(rid, piece, last_piece)
                tw = time.time()
                mid = pos > start        # this request had data flowing before this piece held it up
                if record is not None:
                    t.waiting[rid] = (idx, tw, mid)
                try:
                    ok = t.wait_piece(piece, CONFIG['piece_wait_s'])
                finally:
                    t.waiting.pop(rid, None)
                t1 = time.time()
                timing['wait'] += t1 - t0
                if record is not None and t1 - tw >= 3:
                    t.stalls.append({'at': round(t1, 2), 'idx': idx, 'pos': pos, 'wait': round(t1 - tw, 1), 'mid': mid})
                if not ok:
                    log(f'stream {t.ih[:8]}/{idx}: piece {piece} not available after {CONFIG["piece_wait_s"]}s')
                    return False
                data = t.piece_data(piece)
                t2 = time.time()
                timing['read'] += t2 - t1
                if data is None:
                    log(f'stream {t.ih[:8]}/{idx}: could not read piece {piece}')
                    return False
                a = foff + pos - piece * plen                  # where we are inside the piece
                b = min(len(data), foff + end + 1 - piece * plen)
                view = memoryview(data)[a:b]
                for off, rb in patches:        # e.g. a misplaced subtitle 'trak' relabelled 'free'
                    if off < pos + len(view) and off + len(rb) > pos:
                        view = memoryview(bytearray(view))
                        for i, ch in enumerate(rb):
                            if pos <= off + i < pos + len(view):
                                view[off + i - pos] = ch
                for i in range(0, len(view), CHUNK):
                    self.wfile.write(view[i:i + CHUNK])
                timing['write'] += time.time() - t2
                pos += b - a
                self.sent += b - a
                if track:
                    t.positions[idx] = (pos, time.time())
                if record is not None:
                    record['sent'] = self.sent
            return True
        except OSError:
            return False   # the player closed the connection — normal on every seek
        except RuntimeError as e:
            log(f'stream {t.ih[:8]}/{idx}: stopped — {e}')   # e.g. torrent removed under the reader
            return False
        finally:
            t.release(rid)

    # ---- everything else: Stremio's own server

    remembered = None   # path -> (status, content-type, body), loaded lazily

    @classmethod
    def _remembered(cls):
        if cls.remembered is None:
            try:
                cls.remembered = {k: (v[0], v[1], v[2].encode()) for k, v in json.loads(REMEMBERED_FILE.read_text()).items()}
            except (OSError, ValueError):
                cls.remembered = {}
        return cls.remembered

    def proxy(self):
        body = self.read_body() or None
        host, port = CONFIG['upstream'].split(':')
        key = urllib.parse.urlsplit(self.path).path if self.command == 'GET' else None
        if key not in REMEMBERED:
            key = None
        # The status routes are how the TV decides whether the server is online: don't hang them.
        conn = http.client.HTTPConnection(host, int(port), timeout=5 if key else 600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        if body is not None:
            headers['Content-Length'] = str(len(body))
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            r = conn.getresponse()
        except OSError as e:
            conn.close()
            path = urllib.parse.urlsplit(self.path).path
            if path in ('/subtitles.srt', '/subtitles.vtt') and self.command in ('GET', 'HEAD'):
                return self.subtitles(path.endswith('.vtt'))
            if path == '/opensubHash' and self.command in ('GET', 'HEAD'):
                return self.opensub_hash()
            saved = self._remembered().get(key) if key else None
            if saved:
                # Stremio desktop is closed: answer from the last time it was up, so the TV keeps
                # treating the streaming server as online and torrents keep working.
                status, ctype, data = saved
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.cors()
                self.end_headers()
                return self.wfile.write(data)
            return self.json({'error': f'Stremio streaming server unreachable: {e}'}, 502)
        if key and r.status == 200:
            data = r.read()
            conn.close()
            mem = self._remembered()
            ctype = r.getheader('Content-Type') or 'application/json; charset=utf-8'
            if mem.get(key, (None, None, None))[2] != data:
                mem[key] = (200, ctype, data)
                try:
                    write_atomic(REMEMBERED_FILE, json.dumps({k: [v[0], v[1], v[2].decode(errors='replace')]
                                                              for k, v in mem.items()}))
                except OSError:
                    pass
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.cors()
            self.end_headers()
            return self.wfile.write(data)
        try:
            self.send_response_only(r.status, r.reason)
            length = r.getheader('Content-Length')
            for k, v in r.getheaders():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            if self.path.startswith('/settings') and r.getheader('Cache-Control') is None:
                self.send_header('Cache-Control', 'no-store')
            if length is None and self.command != 'HEAD' and r.status not in (204, 304):
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.end_headers()
            if self.command != 'HEAD':
                while True:
                    chunk = r.read1(64 * 1024)      # forward as it arrives (HLS segments, slow transcodes)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except http.client.HTTPException:
            self.close_connection = True           # upstream ended mid-body
        finally:
            conn.close()

    def subtitles(self, vtt):
        """/subtitles.srt|vtt?from=<url>&offset=<ms> while Stremio desktop is closed: an addon's subtitle file, which
        the TV only ever loads through the streaming server, converted here as Stremio's server would (subtitles.py)."""
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        url = (q.get('from') or [''])[0]
        try:
            offset = int((q.get('offset') or ['0'])[0])
        except ValueError:
            offset = 0
        try:
            body = subtitle_file(url, vtt, offset)
        except (OSError, ValueError) as e:
            log(f'subtitles from {for_log(url)[:160]} failed: {e}')
            return self.json({'error': f'subtitles: {e}'}, 502)
        return self.send_bytes(200, 'text/vtt; charset=utf-8' if vtt else 'application/x-subrip; charset=utf-8', body,
                               {'Access-Control-Allow-Origin': '*'})

    def opensub_hash(self):
        """/opensubHash?videoUrl=<url> while Stremio desktop is closed: the video's OpenSubtitles hash, which the TV
        passes to subtitle addons so they can offer subtitles timed for this very release (subtitles.py). A torrent's
        own URL reads back through this server, from this Mac's address, so it doesn't count as the TV playing."""
        url = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('videoUrl') or [''])[0]
        try:
            return self.json({'error': None, 'result': video_hash(url)})
        except (OSError, ValueError) as e:
            log(f'opensubHash of {for_log(url)[:160]} failed: {e}')
            return self.json({'error': str(e)}, 500)


class FastBind:
    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse-DNS lookup that can take ~30 s.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class TLSServer(FastBind, ThreadingHTTPServer):
    """TLS handshakes happen on the request thread, so one slow client cannot stall accept()."""
    daemon_threads = True
    ssl_context = None

    def finish_request(self, request, client_address):
        request.settimeout(15)
        try:
            request = self.ssl_context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as e:
            if not client_address[0].startswith(('127.', '::1')):
                log(f'{client_address[0]} TLS handshake failed: {e}')
            request.close()
            return
        request.settimeout(None)
        try:
            super().finish_request(request, client_address)
        finally:
            request.close()


class PlainServer(FastBind, ThreadingHTTPServer):
    daemon_threads = True


def tls_context():
    """Loads Stremio's certificate into TLSServer.ssl_context and keeps it current."""
    tls_dir = INSTALL_DIR / 'tls'
    tls_dir.mkdir(mode=0o700, exist_ok=True)
    state = {'mtime': None}

    def load():
        mtime = os.stat(CONFIG['cert_file']).st_mtime
        if mtime == state['mtime']:
            return
        c = json.loads(Path(CONFIG['cert_file']).read_text())
        for name, pem in (('cert.pem', c['cert']), ('key.pem', c['key'])):
            p = tls_dir / name
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as fh:
                fh.write(pem)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_dir / 'cert.pem', tls_dir / 'key.pem')
        TLSServer.ssl_context = ctx       # new handshakes use it; ones in progress keep the old one
        log(f'cert {"loaded" if state["mtime"] is None else "reloaded"}: {c["domain"]} (expires {c.get("notAfter")})')
        state['mtime'] = mtime

    def watch():
        while True:
            time.sleep(60)
            try:
                load()
            except Exception as e:
                log('cert reload failed:', e)

    load()
    threading.Thread(target=watch, name='cert-watch', daemon=True).start()


def main():
    engine = Engine()
    Handler.engine = engine
    if os.access(CONFIG['ffprobe'], os.X_OK):
        Handler.prober = Prober(FactStore(CONFIG['facts_file']), f'http://127.0.0.1:{CONFIG["http_port"]}', log,
                                runner=ffprobe_runner(CONFIG['ffprobe']))
    else:
        log(f'stream facts off — no ffprobe at {CONFIG["ffprobe"]}')
    prefetcher = Prefetcher(engine, log)
    engine.pinned = prefetcher.pinned
    Handler.dashboard = Dashboard(
        sessions=engine.dashboard_sessions,
        facts=lambda key: Handler.prober.store.get(key) if Handler.prober else None,
        config=CONFIG, install_dir=INSTALL_DIR, log=log, phone_url=phone_url,
        in_session=lambda: set(engine.torrents), live_progress=engine.file_progress,
        busy_files=engine.busy_files, offline=engine.offline, prefetch=prefetcher, places=engine.places,
        held=lambda: engine.moving | prefetcher.pinned(),
        tv_ip=lambda: Handler.tv_ip or last_tv_ip(_tail_log(), own_ips()))
    servers = []
    plain = PlainServer(('0.0.0.0', CONFIG['http_port']), Handler)
    servers.append(plain)
    try:
        tls_context()
        tls = TLSServer(('0.0.0.0', CONFIG['https_port']), Handler)
        servers.append(tls)
    except Exception as e:
        log(f'HTTPS disabled — could not load {CONFIG["cert_file"]}: {e}')
    for s in servers:
        threading.Thread(target=s.serve_forever, name=f'http-{s.server_address[1]}', daemon=True).start()
    log(f'libtorrent {lt.__version__} — listening on '
        + ', '.join(f'{"https" if isinstance(s, TLSServer) else "http"}:{s.server_address[1]}' for s in servers)
        + f'; other routes -> {CONFIG["upstream"]}; cache {CONFIG["cache_dir"]} (budget {CONFIG["cache_budget_gb"]} GB)'
        + (f'; downloads for later to {ext["name"]} ({"connected" if ext["available"] else ext["why"].lower()}, '
           f'budget {CONFIG["external_budget_gb"]} GB)' if (ext := engine.places.external()) else '')
        + f'; download cap {CONFIG["download_limit_mbps"] or "none"} Mbps'
        + ('; learning stream facts from plays' if Handler.prober and CONFIG['learn_from_plays'] else ''))

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(1):
        pass
    log('stopping: saving resume data')
    for s in servers:
        s.shutdown()
    prefetcher.stop()
    engine.shutdown()


if __name__ == '__main__':
    sys.exit(main())
