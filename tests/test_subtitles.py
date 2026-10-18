"""Run with:  python3 -m unittest discover -s tests"""
import gzip
import io
import re
import sys
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from subtitles import cues, decode, render, subtitle_file, unpack, video_hash  # noqa: E402

SRT = ('1\r\n00:01:10,281 --> 00:01:12,083\r\nEver since we...\r\n\r\n'
       '2\r\n00:01:14,884 --> 00:01:17,687\r\nYou\'re the only person\r\nthat I\'ve ever...\r\n\r\n'
       'not a cue at all\r\n\r\n'
       '3\r\n00:01:18,521 --> 00:01:21,092\r\nTom & Jerry\r\n')


class Parsing(unittest.TestCase):
    def test_cues_in_milliseconds_with_their_lines(self):
        self.assertEqual(cues(SRT), [(70281, 72083, 'Ever since we...'),
                                     (74884, 77687, "You're the only person\nthat I've ever..."),
                                     (78521, 81092, 'Tom & Jerry')])

    def test_blank_lines_with_spaces_between_cues(self):
        self.assertEqual(len(cues('1\n00:00:01,000 --> 00:00:02,000\nA\n  \n2\n00:00:03,000 --> 00:00:04,000\nB\n')), 2)

    def test_a_cue_without_text_or_times_is_dropped(self):
        self.assertEqual(cues('1\n00:00:01,000 --> 00:00:02,000\n\n2\nxx --> yy\nB\n'), [])

    def test_webvtt_input(self):
        self.assertEqual(cues('WEBVTT\n\n00:00:01.500 --> 00:00:02.000 align:start\nHi\n'), [(1500, 2000, 'Hi')])


class Writing(unittest.TestCase):
    def test_srt_the_way_stremio_writes_it(self):
        self.assertEqual(render([(70281, 72083, 'Tom & Jerry')], vtt=False),
                         b'0\n00:01:10,281 --> 00:01:12,083\nTom &amp; Jerry\n\n')

    def test_vtt(self):
        self.assertEqual(render([(1500, 3661001, 'Hi')], vtt=True),
                         b'WEBVTT\n\n0\n00:00:01.500 --> 01:01:01.001\nHi\n\n')

    def test_offset_moves_every_cue_and_stops_at_zero(self):
        self.assertEqual(render([(1000, 3000, 'a')], vtt=False, offset_ms=-2000),
                         b'0\n00:00:00,000 --> 00:00:01,000\na\n\n')


class Decoding(unittest.TestCase):
    def test_utf8_with_and_without_bom(self):
        self.assertEqual(decode('café'.encode()), 'café')
        self.assertEqual(decode(b'\xef\xbb\xbf' + 'café'.encode()), 'café')

    def test_utf16_by_its_bom(self):
        self.assertEqual(decode('café'.encode('utf-16')), 'café')

    def test_a_legacy_western_file(self):
        self.assertEqual(decode('café “quoted”'.encode('cp1252')), 'café “quoted”')

    def test_the_charset_the_server_named(self):
        self.assertEqual(decode('Привет'.encode('cp1251'), 'windows-1251'), 'Привет')


class Unpacking(unittest.TestCase):
    def test_the_srt_inside_a_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('readme.nfo', b'x')
            z.writestr('Movie.srt', b'subs')
        self.assertEqual(unpack(buf.getvalue(), '/download/123.zip'), b'subs')

    def test_a_zip_without_one(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('readme.nfo', b'x')
        with self.assertRaises(ValueError):
            unpack(buf.getvalue(), '/download/123.zip')

    def test_gzip(self):
        self.assertEqual(unpack(gzip.compress(b'subs'), '/file.srt.gz'), b'subs')

    def test_anything_else_as_is(self):
        self.assertEqual(unpack(b'subs', '/file/1962434148'), b'subs')


VIDEO = b'\xff' * 65536 + bytes(65536)    # 128 KB: the first 64 KB all ones, the last all zeros


class Host(BaseHTTPRequestHandler):
    """An addon's subtitle host and a video server: /ok.srt answers SRT; /video serves VIDEO in ranges, /no-ranges
    only whole, /tiny is 1000 bytes of it; anything else 404. Records each request's User-Agent."""
    agents = []

    def video(self):
        data = VIDEO[:1000] if self.path == '/tiny' else VIDEO
        m = re.fullmatch(r'bytes=(\d+)-(\d+)', self.headers.get('Range') or '')
        if m and self.path != '/no-ranges':
            body = data[int(m.group(1)):int(m.group(2)) + 1]
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {m.group(1)}-{m.group(2)}/{len(data)}')
        else:
            body = data
            self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        Host.agents.append(self.headers.get('User-Agent'))
        if self.path in ('/video', '/no-ranges', '/tiny'):
            return self.video()
        body = SRT.encode() if self.path == '/ok.srt' else b'missing'
        self.send_response(200 if self.path == '/ok.srt' else 404)
        self.send_header('Content-Type', 'application/x-subrip; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


class Fetching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Host)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_an_addons_file_as_vtt(self):
        out = subtitle_file(self.base + '/ok.srt', vtt=True)
        self.assertTrue(out.startswith(b'WEBVTT\n\n0\n00:01:10.281 --> 00:01:12.083\nEver since we...\n\n'))
        self.assertIn(b'Tom &amp; Jerry', out)

    def test_not_as_python(self):
        # subs5.strem.io answers 403 to urllib's own User-Agent.
        subtitle_file(self.base + '/ok.srt')
        self.assertNotIn('Python', Host.agents[-1])

    def test_a_missing_file(self):
        with self.assertRaises(OSError):
            subtitle_file(self.base + '/gone.srt')

    def test_only_web_addresses(self):
        with self.assertRaises(ValueError):
            subtitle_file('file:///etc/passwd')
        with self.assertRaises(ValueError):
            video_hash('file:///etc/passwd')

    def test_a_videos_opensubtitles_hash(self):
        # The size, plus 8192 words of 2^64 - 1 (wrapping to -8192), plus 8192 zero words.
        self.assertEqual(video_hash(self.base + '/video'), {'size': 131072, 'hash': '000000000001e000'})

    def test_a_server_that_only_sends_whole_files(self):
        with self.assertRaises(ValueError):
            video_hash(self.base + '/no-ranges')

    def test_a_file_too_small_to_be_a_video(self):
        with self.assertRaises(ValueError):
            video_hash(self.base + '/tiny')


if __name__ == '__main__':
    unittest.main()
