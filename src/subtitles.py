"""Subtitles from an addon, for the TV while Stremio desktop is closed.

The TV never loads an addon's subtitle file itself: it asks the streaming server for
/subtitles.srt?from=<url> (or .vtt), and Stremio's server fetches the file, decodes it and writes its cues back out.
It also asks it for /opensubHash?videoUrl=<url>, the video's OpenSubtitles hash, which it passes to subtitle addons
so they can offer subtitles timed for that very release. With Stremio closed those routes have nobody to answer
them, so the torrent server answers them with this, the same way: the first .srt of a .zip, a .gz unpacked, cues
numbered from 0, '&' written as '&amp;'; the hash from the video's first and last 64 KB, read over HTTP.
"""
import gzip
import io
import os
import re
import ssl
import struct
import urllib.parse
import urllib.request
import zipfile

MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_S = 20
HASH_TIMEOUT_S = 60                 # a torrent's last piece may still have to be downloaded
HASH_CHUNK = 64 * 1024
USER_AGENT = 'stremio-libtorrent'   # subs5.strem.io (OpenSubtitles through Stremio) refuses urllib's own with a 403
# python.org's Python has no CA certificates until its "Install Certificates" step is run; macOS keeps a bundle here.
SYSTEM_CA = '/etc/ssl/cert.pem'
TIME = r'(?:(\d+):)?(\d{1,2}):(\d{1,2})[,.](\d{1,3})'
TIMING = re.compile(rf'^\s*{TIME}\s*-->\s*{TIME}')


def _context():
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats()['x509_ca'] and os.path.exists(SYSTEM_CA):
        ctx.load_verify_locations(SYSTEM_CA)
    return ctx


def _open(url, timeout, method='GET', headers=None):
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError('not a web address')
    req = urllib.request.Request(url, method=method, headers={'User-Agent': USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, context=_context(), timeout=timeout)


def fetch(url):
    """The file at an http(s) address, and the charset its server named (or None)."""
    with _open(url, TIMEOUT_S) as r:
        raw = r.read(MAX_BYTES + 1)
        charset = r.headers.get_content_charset()
    if len(raw) > MAX_BYTES:
        raise ValueError('larger than a subtitle file')
    return raw, charset


def unpack(raw, path):
    """The subtitle file itself: the first .srt in a .zip, a .gz unpacked, anything else as it is."""
    if path.endswith('zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                name = next((n for n in z.namelist() if n.endswith('srt')), None)
                if name is None:
                    raise ValueError('no .srt in the zip')
                return z.read(name)
        except zipfile.BadZipFile as e:
            raise ValueError(f'bad zip: {e}') from None
    if path.endswith('gz'):
        return gzip.decompress(raw)
    return raw


def decode(raw, charset=None):
    """Text from a subtitle file: by its byte-order mark, else the charset its server named, else UTF-8, else the
    Western Windows code page most older subtitle files were written in."""
    if raw.startswith(b'\xef\xbb\xbf'):
        return raw[3:].decode('utf-8', errors='replace')
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16', errors='replace')
    for enc in (charset, 'utf-8', 'cp1252'):
        if enc:
            try:
                return raw.decode(enc)
            except (LookupError, UnicodeDecodeError):
                pass
    return raw.decode('latin-1')


def _ms(h, m, s, frac):
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, '0'))


def cues(text):
    """[(start ms, end ms, text)] of an SRT (or WebVTT) file; anything that isn't a whole cue is skipped."""
    out = []
    for block in re.split(r'\n[ \t]*\n', text.replace('\r', '')):
        lines = block.strip('\n').split('\n')
        while len(lines) > 2 and '-->' not in lines[0]:
            lines.pop(0)          # the cue's number, or a WEBVTT header line
        m = TIMING.match(lines[0]) if len(lines) >= 2 else None
        body = '\n'.join(lines[1:]).strip('\n')
        if m and body:
            out.append((_ms(*m.groups()[:4]), _ms(*m.groups()[4:]), body))
    return out


def _clock(ms, sep):
    s, ms = divmod(ms, 1000)
    return f'{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}{sep}{ms:03d}'


def render(items, vtt, offset_ms=0):
    """The cues written out the way Stremio's server writes them."""
    sep = '.' if vtt else ','
    parts = ['WEBVTT\n\n'] if vtt else []
    for i, (start, end, body) in enumerate(items):
        start, end = max(0, start + offset_ms), max(0, end + offset_ms)
        parts.append(f'{i}\n{_clock(start, sep)} --> {_clock(end, sep)}\n{body.replace("&", "&amp;")}\n\n')
    return ''.join(parts).encode()


def subtitle_file(url, vtt=False, offset_ms=0):
    """What /subtitles.srt (or .vtt) ?from=url answers. Raises OSError when the file can't be fetched, ValueError when
    it isn't a subtitle file."""
    raw, charset = fetch(url)
    items = cues(decode(unpack(raw, urllib.parse.urlsplit(url).path), charset))
    if not items:
        raise ValueError('no subtitles in the file')
    return render(items, vtt, offset_ms)


def video_hash(url):
    """What /opensubHash?videoUrl=url answers: {'size', 'hash'}, the hash being OpenSubtitles' — the size plus every
    64-bit little-endian word of the first and last 64 KB, modulo 2^64, in 16 hex digits."""
    with _open(url, HASH_TIMEOUT_S, 'HEAD') as r:
        length, url = r.headers.get('Content-Length'), r.geturl()
    if not (length or '').isdigit():
        raise ValueError('the video server gave no size')
    size = int(length)
    if size < HASH_CHUNK:
        raise ValueError('too small to be a video')
    total = size
    for start in (0, size - HASH_CHUNK):
        with _open(url, HASH_TIMEOUT_S, headers={'Range': f'bytes={start}-{start + HASH_CHUNK - 1}'}) as r:
            data = r.read(HASH_CHUNK + 1)
        if len(data) != HASH_CHUNK:
            raise ValueError('the video server did not send the part asked for')
        total += sum(struct.unpack(f'<{HASH_CHUNK // 8}Q', data))
    return {'size': size, 'hash': f'{total % 2 ** 64:016x}'}
