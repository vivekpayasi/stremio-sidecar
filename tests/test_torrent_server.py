"""Run with the server's own Python (libtorrent is installed there):
    ~/.stremio-local-addon/venv/bin/python -m unittest discover -s tests
Skipped where libtorrent isn't installed. The engine's pure logic is run on stand-ins for libtorrent's handle.
"""
import http.client
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
os.environ['TORRENTD_CONFIG'] = os.devnull + '.none'   # the built-in defaults, not this Mac's torrentd.json

try:
    import torrent_server as ts
except ImportError as e:
    raise unittest.SkipTest(f'torrent_server needs libtorrent ({e})')
import storage  # noqa: E402
from storage import EXTERNAL_DIR, Places, bencode  # noqa: E402

MB = 1024 * 1024


class Handle:
    """What Torrent._apply uses of a libtorrent handle: the pieces it has, and the priorities and deadlines set."""
    def __init__(self, n, have=()):
        self.pieces = [i in set(have) for i in range(n)]
        self.prio = {}
        self.deadlines = {}

    def status(self, flags=None):
        return SimpleNamespace(pieces=self.pieces)

    def prioritize_pieces(self, changes):
        self.prio.update(dict(changes))

    def reset_piece_deadline(self, p):
        self.deadlines.pop(p, None)

    def set_piece_deadline(self, p, ms):
        self.deadlines[p] = ms


def torrent(have=(), n=300, plen=16 * MB):
    """A Torrent with 16 MB pieces: a stream's urgent part is 4 pieces, its window starts at 2 and grows to 28."""
    t = object.__new__(ts.Torrent)
    t.ti = SimpleNamespace(piece_length=lambda: plen)
    t.handle = Handle(n, have)
    t.readers, t.prio, t.deadlines, t.lock = {}, {}, set(), threading.RLock()
    return t


def reader(at, last, bg=False):
    return {'at': at, 'last': last, 'released': None, 'hi': at, 'urgent_to': at, 'bg': bg, 'short': not bg}


class Windows(unittest.TestCase):
    def test_a_stream_alone(self):
        t = torrent()
        t.readers['tv'] = reader(10, 200)
        t._apply()
        self.assertEqual(t.handle.prio, {10: 7, 11: 7, 12: 7})
        self.assertEqual(sorted(t.handle.deadlines), [10, 11, 12])

    def test_a_download_for_later_beside_a_stream(self):
        t = torrent()
        t.readers['tv'] = reader(10, 200)
        t.readers['later'] = reader(5, 100, bg=True)
        t._apply()
        want = {p: 1 for p in range(5, 34)}       # its whole window (28 ahead) at once, at the lowest priority
        want.update({10: 7, 11: 7, 12: 7})        # the stream's pieces keep their own priority...
        self.assertEqual(t.handle.prio, want)
        self.assertEqual(sorted(t.handle.deadlines), [10, 11, 12], '...and only the stream gets deadlines')

    def test_a_download_for_later_alone_ends_at_its_file(self):
        t = torrent()
        t.readers['later'] = reader(40, 50, bg=True)
        t._apply()
        self.assertEqual(t.handle.prio, {p: 1 for p in range(40, 51)})
        self.assertEqual(t.handle.deadlines, {})

    def test_a_stream_is_short_until_half_its_read_ahead_is_in(self):
        for have, last, short in ((range(10, 23), 200, True),     # 13 pieces in
                                  (range(10, 24), 200, False),    # 14 = half of 28
                                  (range(10, 16), 15, False),     # everything it asked for is in
                                  ((), 200, True)):
            t = torrent(have=have)
            t.readers['tv'] = reader(10, last)
            t._apply()
            self.assertEqual(t.readers['tv']['short'], short, (list(have)[-1:], last))


class StreamsShort(unittest.TestCase):
    def engine(self, *readers):
        t = SimpleNamespace(readers=dict(enumerate(readers)), lock=threading.RLock())
        return SimpleNamespace(torrents={'a' * 40: t})

    def test_only_live_streams_count(self):
        short = ts.Engine.streams_short
        self.assertTrue(short(self.engine(dict(reader(0, 9), short=True))))
        self.assertFalse(short(self.engine(dict(reader(0, 9), short=False))))
        self.assertFalse(short(self.engine(dict(reader(0, 9), short=True, released=time.time()))), 'a closed request')
        self.assertFalse(short(self.engine(dict(reader(0, 9, bg=True), short=True))), 'a download for later')
        self.assertFalse(short(SimpleNamespace(torrents={})))


class PieceRange(unittest.TestCase):
    def test_the_pieces_a_file_spans(self):
        fs = SimpleNamespace(file_offset=lambda i: [0, 10 * MB, 18 * MB][i], file_size=lambda i: [10 * MB, 8 * MB, 0][i])
        t = object.__new__(ts.Torrent)
        t.ti = SimpleNamespace(piece_length=lambda: 4 * MB, layout=lambda: fs)
        self.assertEqual([t.piece_range(i) for i in range(3)], [(0, 2), (2, 4), (4, 4)])


def engine(places, torrents=None, pinned=()):
    """An Engine without libtorrent: its places (as its places thread last saw them), the torrents in its session, and
    the downloads for later under way."""
    places.refresh()
    e = object.__new__(ts.Engine)
    e.places, e.cache, e.torrents = places, places.mac()['dir'], dict(torrents or {})
    e.pinned, e.moving, e.aborts, e.lock = (lambda: set(pinned)), set(), set(), threading.RLock()
    e.moved = threading.Condition(e.lock)
    e.drop = lambda t, reason, only_if_idle=False: e.dropped.append((t.ih, reason))
    e.dropped = []
    return e


def cached(base, ih, mb, used, done=0):
    """A torrent folder: an `mb` MB file, last used at `used`, `done` of its two pieces downloaded."""
    d = base / ih
    d.mkdir(parents=True)
    (d / 'Movie.mkv').write_bytes(b'x' * mb * MB)
    (d / '.last-used').write_text(str(used))
    info = {'name': 'Movie.mkv', 'piece length': mb * MB, 'length': 2 * mb * MB}
    (d / '.resume').write_bytes(bencode({'info': info, 'pieces': bytes([(0, 0b10000000, 0b11000000)[done]])}))
    return d


class TwoPlaces(unittest.TestCase):
    """This Mac's cache folder, and a drive (a temporary folder counted as mounted)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.mac, self.ssd = root / 'cache', root / 'Volumes' / 'Extreme SSD'
        self.mac.mkdir()
        self.ssd.mkdir(parents=True)
        self.ext = self.ssd / EXTERNAL_DIR
        self.connected = True
        self.config = {'cache_dir': str(self.mac), 'cache_budget_gb': 3 / 1024, 'external_volume': str(self.ssd),
                       'external_budget_gb': 5 / 1024, 'downloads_to': 'external', 'min_free_gb': 0}
        self.places = Places(self.config, None, mounted=lambda p: self.connected and Path(p) == self.ssd)
        self.patch = mock.patch.dict(ts.CONFIG, min_free_gb=0)
        self.patch.start()
        self.log = mock.patch.object(ts, 'log')
        self.log.start()

    def tearDown(self):
        self.log.stop()
        self.patch.stop()
        self.tmp.cleanup()


def until(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def evict(e):
    """Both evictions: this Mac's (housekeeping) and the drive's (the places thread)."""
    ts.Engine._evict(e)
    ts.Engine._evict(e, drives=True)


class Eviction(TwoPlaces):
    def test_each_place_keeps_to_its_own_budget(self):
        a, b = 'a' * 40, 'b' * 40
        cached(self.mac, a, 2, 1000)
        cached(self.mac, b, 2, 2000)
        c, d = cached(self.ext, 'c' * 40, 2, 500), cached(self.ext, 'd' * 40, 2, 600)
        evict(engine(self.places))
        self.assertFalse((self.mac / a).exists(), 'this Mac keeps 3 MB: its oldest goes')
        self.assertTrue((self.mac / b).exists())
        self.assertTrue(c.exists() and d.exists(), 'the drive keeps 5 MB: both of its 4 MB stay, older as they are')

    def test_the_torrent_of_an_unfinished_download_is_kept(self):
        for ih, used in (('a' * 40, 1000), ('b' * 40, 2000)):
            cached(self.mac, ih, 2, used)
        evict(engine(self.places, pinned={'a' * 40}))
        self.assertTrue((self.mac / ('a' * 40)).exists(), 'the oldest, but a download for later is under way')
        self.assertFalse((self.mac / ('b' * 40)).exists())

    def test_a_disconnected_drive_is_left_alone(self):
        cached(self.ext, 'c' * 40, 3, 500)
        cached(self.ext, 'd' * 40, 3, 600)
        self.connected = False
        evict(engine(self.places))
        self.assertEqual(len(list(self.ext.iterdir())), 2)

    def test_of_two_copies_the_one_with_less_downloaded_goes(self):
        # The drive was away, and the TV played the movie again from this Mac.
        self.config.update(cache_budget_gb=1, external_budget_gb=1)
        ih = 'e' * 40
        cached(self.mac, ih, 1, 1000, done=1)
        cached(self.ext, ih, 1, 500, done=2)
        evict(engine(self.places, torrents={ih: object()}))
        self.assertTrue((self.mac / ih).exists() and (self.ext / ih).exists(), 'not while it is in the session')
        evict(engine(self.places))
        self.assertFalse((self.mac / ih).exists())
        self.assertTrue((self.ext / ih).exists())

    def test_two_copies_each_with_pieces_the_other_lacks_both_stay(self):
        # A download for later on the drive, and other episodes played from this Mac while the drive was away.
        self.config.update(cache_budget_gb=1, external_budget_gb=1)
        ih = 'e' * 40
        cached(self.mac, ih, 1, 1000, done=1)
        d = cached(self.ext, ih, 1, 500, done=0)
        info = {'name': 'Movie.mkv', 'piece length': MB, 'length': 2 * MB}
        (d / '.resume').write_bytes(bencode({'info': info, 'pieces': bytes([0b01000000])}))
        evict(engine(self.places))
        self.assertTrue((self.mac / ih).exists() and (self.ext / ih).exists())

    def test_what_the_tv_takes_back_meanwhile_is_not_deleted(self):
        # The folders are listed, then the TV asks for one of them before its turn to go comes.
        ih = 'a' * 40
        cached(self.mac, ih, 2, 1000)
        cached(self.mac, 'b' * 40, 2, 2000)
        e = engine(self.places)
        real = ts.disk_usage
        with mock.patch.object(ts, 'disk_usage', lambda d: (e.torrents.setdefault(ih, object()), real(d))[1]):
            ts.Engine._evict(e)
        self.assertTrue((self.mac / ih).exists())

    def test_get_waits_while_a_folder_is_deleted(self):
        ih = 'a' * 40
        d = cached(self.mac, ih, 1, 1000)
        e = engine(self.places)
        seen = []
        real = ts.shutil.rmtree
        with mock.patch.object(ts.shutil, 'rmtree', lambda p, **k: (seen.append(set(e.moving)), real(p, **k))[1]):
            self.assertTrue(ts.Engine._delete_folder(e, d))
        self.assertEqual(seen, [{ih}])
        self.assertEqual(e.moving, set())
        e.torrents[ih] = object()
        cached(self.mac, ih, 1, 1000)
        self.assertFalse(ts.Engine._delete_folder(e, self.mac / ih), 'not while it is in the session')

    def test_a_move_cut_short_is_cleaned_up(self):
        (self.ext / ('f' * 40 + '.moving')).mkdir(parents=True)
        (self.ext / ('a' * 40 + '.moving')).mkdir()
        e = engine(self.places)
        e.moving.add('a' * 40)
        evict(e)
        self.assertEqual([p.name for p in self.ext.iterdir()], ['a' * 40 + '.moving'], 'the one still under way stays')


class DiskLow(TwoPlaces):
    def test_below_the_space_kept_free(self):
        e = engine(self.places)
        free = ts.shutil.disk_usage(self.mac).free / 1024 / MB
        with mock.patch.dict(ts.CONFIG, min_free_gb=free + 1):
            self.assertTrue(ts.Engine.disk_low(e))
        with mock.patch.dict(ts.CONFIG, min_free_gb=max(0, free - 1)):
            self.assertFalse(ts.Engine.disk_low(e))

    def test_the_disk_of_the_torrent(self):
        ih = 'a' * 40
        e = engine(self.places, torrents={ih: SimpleNamespace(save_path=self.ext / ih)})
        with mock.patch.object(ts.shutil, 'disk_usage', lambda p: SimpleNamespace(free=0 if self.ext in Path(p).parents else 10 ** 15)), \
                mock.patch.dict(ts.CONFIG, min_free_gb=3):
            self.assertTrue(ts.Engine.disk_low(e, ih))
            self.assertFalse(ts.Engine.disk_low(e, 'b' * 40), "not in the session: this Mac's disk")


class DriveGone(TwoPlaces):
    def test_torrents_on_a_drive_that_went_are_let_go(self):
        on_ssd, on_mac = SimpleNamespace(ih='a' * 40, save_path=self.ext / ('a' * 40), dropping=False), \
            SimpleNamespace(ih='b' * 40, save_path=self.mac / ('b' * 40), dropping=False)
        e = engine(self.places, torrents={'a' * 40: on_ssd, 'b' * 40: on_mac})
        ts.Engine._drop_unreachable(e)
        self.assertEqual(e.dropped, [])
        self.connected = False
        self.places.refresh()
        ts.Engine._drop_unreachable(e)
        self.assertEqual([ih for ih, _ in e.dropped], ['a' * 40])
        self.assertIn('Extreme SSD', e.dropped[0][1])


class Prepare(TwoPlaces):
    """Before a file downloads for later to a place, its torrent is moved there, and on a disk that can't skip empty
    space the file is written out to its full length."""
    IH = 'a' * 40

    def setUp(self):
        super().setUp()
        cached(self.mac, self.IH, 1, 1000, done=1)       # Movie.mkv: 2 MB long, its first 1 MB downloaded

    def test_a_torrent_on_this_mac_moves_to_the_drive(self):
        e = engine(self.places)
        self.assertEqual(ts.Engine.prepare(e, self.IH, 0, str(self.ext)), (True, None))
        self.assertFalse((self.mac / self.IH).exists())
        self.assertEqual((self.ext / self.IH / 'Movie.mkv').read_bytes(), b'x' * MB)
        self.assertEqual(ts.Engine.folder(e, self.IH), self.ext / self.IH)

    def test_on_a_disk_that_cant_skip_empty_space_the_file_is_written_out(self):
        e = engine(self.places)
        self.places.checks.clear()
        with mock.patch.object(storage, 'can_skip_empty_space', lambda d: False):
            self.assertEqual(ts.Engine.prepare(e, self.IH, 0, str(self.ext)), (True, None))
        f = self.ext / self.IH / 'Movie.mkv'
        self.assertEqual(f.stat().st_size, 2 * MB)
        self.assertEqual(f.read_bytes()[:MB], b'x' * MB)

    def test_a_torrent_whose_state_was_never_saved_gets_its_file_written_out(self):
        # Added minutes ago: its resume data reaches the disk only as it leaves the session.
        ih = 'c' * 40
        d = self.mac / ih
        d.mkdir()
        t = SimpleNamespace(ih=ih, save_path=d, dropping=False, live_readers=lambda: 0, ti=None, last_used=0)
        e = engine(self.places, torrents={ih: t})
        e.busy_files = lambda ih: set()

        def drop(t, reason, only_if_idle=False):
            info = {'name': 'New.2026.mkv', 'piece length': MB, 'length': 3 * MB}
            (t.save_path / '.resume').write_bytes(bencode({'info': info, 'pieces': b''}))
            del e.torrents[t.ih]
        e.drop = drop
        self.places.checks.clear()
        with mock.patch.object(storage, 'can_skip_empty_space', lambda d: False):
            self.assertEqual(ts.Engine.prepare(e, ih, 0, str(self.ext)), (True, None))
        self.assertEqual((self.ext / ih / 'New.2026.mkv').stat().st_size, 3 * MB)

    def test_already_there_nothing_happens(self):
        e = engine(self.places)
        self.assertEqual(ts.Engine.prepare(e, self.IH, 0, str(self.mac)), (True, None))
        self.assertTrue((self.mac / self.IH / 'Movie.mkv').exists())

    def test_the_folder_is_marked_while_it_moves(self):
        e = engine(self.places)
        seen = []
        real = ts.move_folder
        with mock.patch.object(ts, 'move_folder', lambda *a, **k: (seen.append(set(e.moving)), real(*a, **k))[1]):
            ts.Engine.prepare(e, self.IH, 0, str(self.ext))
        self.assertEqual(seen, [{self.IH}])
        self.assertEqual(e.moving, set())

    def test_a_torrent_being_read_waits(self):
        t = SimpleNamespace(ih=self.IH, save_path=self.mac / self.IH, live_readers=lambda: 1, dropping=False, last_used=0)
        e = engine(self.places, torrents={self.IH: t})
        e.busy_files = lambda ih: set()
        self.assertIsNone(ts.Engine.prepare(e, self.IH, 0, str(self.ext)))
        self.assertTrue((self.mac / self.IH).exists())

    def test_a_drive_that_isnt_there(self):
        self.connected = False
        ok, msg = ts.Engine.prepare(engine(self.places), self.IH, 0, str(self.ext))
        self.assertFalse(ok)
        self.assertIn('Extreme SSD', msg)
        self.assertTrue((self.mac / self.IH).exists())

    def test_a_better_copy_on_the_drive_is_kept(self):
        # The drive was away, the TV played the movie again from this Mac, and the drive is back while that copy is
        # still in the session.
        d = cached(self.ext, self.IH, 1, 500, done=2)
        t = SimpleNamespace(ih=self.IH, save_path=self.mac / self.IH, live_readers=lambda: 0, dropping=False, last_used=0, ti=None)
        e = engine(self.places, torrents={self.IH: t})
        e.busy_files = lambda ih: set()
        e.drop = lambda t, reason, only_if_idle=False: e.torrents.pop(t.ih)
        self.assertEqual(ts.Engine.prepare(e, self.IH, 0, str(self.ext)), (True, None))
        self.assertFalse((self.mac / self.IH).exists(), 'the copy with less goes')
        self.assertEqual(ts.resume_progress(d / '.resume')[0]['done'], 1.0, 'the complete one is untouched')

    def test_copies_that_each_hold_pieces_the_other_lacks_both_stay(self):
        # A piece downloaded for later on the drive; two others played from this Mac while the drive was away — so
        # this Mac's copy is the one with more, and the one a download for later starts from.
        info = {'name': 'Movie.mkv', 'piece length': MB, 'length': 3 * MB}
        (self.mac / self.IH / '.resume').write_bytes(bencode({'info': info, 'pieces': bytes([0b11000000])}))
        d = cached(self.ext, self.IH, 1, 500)
        (d / '.resume').write_bytes(bencode({'info': info, 'pieces': bytes([0b00100000])}))
        e = engine(self.places)
        self.assertEqual(ts.Engine.prepare(e, self.IH, 0, str(self.ext)), (True, None))
        self.assertTrue((self.mac / self.IH / '.resume').exists(), "this Mac's copy has its two pieces")
        self.assertEqual(storage._pieces(d), bytes([0b00100000]), "the drive's still has its own")

    def test_a_write_out_the_tv_cut_short_is_finished_next_time(self):
        e = engine(self.places)
        self.places.checks.clear()
        with mock.patch.object(storage, 'can_skip_empty_space', lambda d: False):
            with mock.patch.object(ts, 'allocate', lambda f, n, stop: (e.aborts.add(self.IH), False)[1]):
                self.assertIsNone(ts.Engine.prepare(e, self.IH, 0, str(self.ext)), 'the TV asked for it: try again later')
            f = self.ext / self.IH / 'Movie.mkv'
            self.assertLess(f.stat().st_size, 2 * MB)
            self.assertEqual(ts.Engine.prepare(e, self.IH, 0, str(self.ext)), (True, None))
        self.assertEqual(f.stat().st_size, 2 * MB)
        self.assertEqual([p.name for p in (self.ext / self.IH).iterdir() if p.name.startswith('.writing')], [])

    def test_a_torrent_out_of_the_session_but_read_moments_ago_is_not_moved(self):
        # Stremio takes a torrent out of the session when its player closes; the next episode may be a moment away.
        (self.mac / self.IH / '.last-used').write_text(str(int(time.time())))
        self.assertIsNone(ts.Engine.prepare(engine(self.places), self.IH, 0, str(self.ext)))
        self.assertTrue((self.mac / self.IH).exists())

    def test_a_torrent_read_moments_ago_is_not_moved(self):
        # Between two requests of the TV, or between an episode and the next: it is about to be read again.
        t = SimpleNamespace(ih=self.IH, save_path=self.mac / self.IH, live_readers=lambda: 0, dropping=False,
                            last_used=time.time() - 5, ti=None)
        e = engine(self.places, torrents={self.IH: t})
        e.busy_files = lambda ih: set()
        self.assertIsNone(ts.Engine.prepare(e, self.IH, 0, str(self.ext)))
        self.assertEqual(e.dropped, [])

    def test_the_tv_asking_for_it_stops_the_move(self):
        e = engine(self.places)
        out = []

        def slow(src, dest, stop=lambda: False):
            while not stop():
                time.sleep(0.01)
            return False, 'Stopped'
        with mock.patch.object(ts, 'move_folder', slow):
            th = threading.Thread(target=lambda: out.append(ts.Engine.prepare(e, self.IH, 0, str(self.ext))))
            th.start()
            self.assertTrue(until(lambda: self.IH in e.moving))
            with e.lock:
                ts.Engine._wait_for_folder(e, self.IH)       # what get() does first
            th.join(2)
        self.assertEqual(out, [None], 'the download for later tries again later')
        self.assertTrue((self.mac / self.IH / 'Movie.mkv').exists())
        self.assertEqual((e.moving, e.aborts), (set(), set()))

    def test_cancel_stops_the_move(self):
        e = engine(self.places)
        ok, msg = ts.Engine.prepare(e, self.IH, 0, str(self.ext), stop=lambda: True)
        self.assertFalse(ok)
        self.assertEqual(msg, 'Stopped')
        self.assertTrue((self.mac / self.IH / 'Movie.mkv').exists())

    def test_a_drive_no_longer_chosen(self):
        ok, msg = ts.Engine.prepare(engine(self.places), self.IH, 0, '/Volumes/Old Drive/' + EXTERNAL_DIR)
        self.assertFalse(ok)
        self.assertIn('Old Drive', msg)

    def test_no_file_is_deleted_from_a_torrent_being_moved(self):
        e = engine(self.places)
        e.busy_files = lambda ih: set()
        e.moving.add(self.IH)
        self.assertIsNone(ts.Engine.offline(e, self.IH, lambda: 'deleted'))

    def test_a_failed_move_keeps_the_torrent_here(self):
        e = engine(self.places)
        with mock.patch.object(ts, 'move_folder', lambda *a, **k: (False, 'Could not copy it there: No space left on device')):
            ok, msg = ts.Engine.prepare(e, self.IH, 0, str(self.ext))
        self.assertFalse(ok)
        self.assertIn('No space', msg)
        self.assertEqual(e.moving, set())


class ForLog(unittest.TestCase):
    def test_the_account_key_is_hidden(self):
        self.assertEqual(ts.for_log('/get-https?authKey=vPaL-Ms%3D&ipAddress=192.168.0.101'),
                         '/get-https?authKey=…&ipAddress=192.168.0.101')

    def test_any_key_or_token_is_hidden(self):
        self.assertEqual(ts.for_log('/x?a=1&api_key=s3cret&Token=t0k'), '/x?a=1&api_key=…&Token=…')

    def test_a_torrent_path_is_untouched(self):
        p = '/' + 'a' * 40 + '/0?tr=udp%3A%2F%2Ftracker.example%3A1337'
        self.assertEqual(ts.for_log(p), p)


class AddonHost(BaseHTTPRequestHandler):
    """A subtitle file at any path, except /video: 128 KB of zeros, in ranges."""
    def do_GET(self):
        if self.path == '/video':
            m = re.fullmatch(r'bytes=(\d+)-(\d+)', self.headers.get('Range') or '')
            body = bytes(int(m.group(2)) - int(m.group(1)) + 1 if m else 131072)
            self.send_response(206 if m else 200)
        else:
            body = b'1\n00:00:01,000 --> 00:00:02,500\nHello\n'
            self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


class StremioClosed(unittest.TestCase):
    """Stremio's server (the upstream) refuses connections, as it does while Stremio desktop is closed."""
    def setUp(self):
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        closed = s.getsockname()[1]
        s.close()
        patch = mock.patch.dict(ts.CONFIG, {'upstream': f'127.0.0.1:{closed}'})
        patch.start()
        self.addCleanup(patch.stop)
        self.subs = ThreadingHTTPServer(('127.0.0.1', 0), AddonHost)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ts.Handler)
        for srv in (self.subs, self.server):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)

    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        self.addCleanup(conn.close)
        conn.request('GET', path)
        r = conn.getresponse()
        return r.status, r.read()

    def test_an_addons_subtitles_still_reach_the_tv(self):
        src = urllib.parse.quote(f'http://127.0.0.1:{self.subs.server_port}/file/1962434148', safe='')
        status, body = self.get(f'/subtitles.vtt?from={src}')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'WEBVTT\n\n0\n00:00:01.000 --> 00:00:02.500\nHello\n\n')

    def test_the_videos_hash_for_subtitle_addons(self):
        src = urllib.parse.quote(f'http://127.0.0.1:{self.subs.server_port}/video', safe='')
        status, body = self.get(f'/opensubHash?videoUrl={src}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'error': None, 'result': {'size': 131072, 'hash': '0000000000020000'}})

    def test_other_routes_say_the_server_is_unreachable(self):
        self.assertEqual(self.get('/hlsv2/probe')[0], 502)


if __name__ == '__main__':
    unittest.main()
