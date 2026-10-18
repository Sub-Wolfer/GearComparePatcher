"""Witcher 3 (next-gen 4.x/5.x) POTATO70 bundle reader / writer.

Next-gen layout ("v5", 304-byte TOC entries) -- all integers little-endian, packed:

  Header (32 bytes)
    0x00  char[8]  magic "POTATO70"
    0x08  u64      bundleSize  = align(end of last entry's data, A)  (A = 16 normally;
                               files cooked with A=1 have unaligned data, see *_startup.bundle)
    0x10  u32      tocSize     = numEntries * 304
    0x14  u16      formatVersion = 5   (classic WolvenKit bundles have 3 here and 320-byte entries)
    0x16  u32      dataStart   = 0x20 + tocSize  (first data byte; always 16-aligned since 304 = 19*16)
    0x1A  u8[6]    zero
  TOC entry (304 bytes) x numEntries, at 0x20
    +0x000 char[256] depot path, latin-1, backslashes, NUL padded (e.g. gameplay\\gui_new\\swf\\worldmap\\panel_worldmap.redswf)
    +0x100 u8[16]    content hash (base-game cooker; algorithm unknown, NOT md5/sha of data or name); REDkit mods write zeros
    +0x110 u64       data offset (absolute file offset; high dword only used by >4 GB bundles)
    +0x118 u32       size   (uncompressed)
    +0x11C u32       zsize  (stored bytes)
    +0x120 u32       crc32 (zlib.crc32) of the UNCOMPRESSED data
    +0x124 u32       compression: 0 none, 1 zlib (deflate w/ zlib header; base game = level 9),
                     2 snappy, 3 doboz, 4 lz4, 5 lz4hc  (4/5 = raw LZ4 *block*, no frame header)
    +0x128 u8[8]     zero
  Data: each entry's stored bytes at its offset; REDkit places them at 16-byte aligned offsets with
  zero padding between entries and does NOT pad after the last entry (bundleSize still counts it).

Classic layout (WolvenKit 0.x / 1.32-era, still loaded by next-gen, e.g. modLightRewrite):
  header 0x14 = 03 00 01 00 00 13 13 13 13 13 13 13 ; TOC entries 320 bytes:
    name[256], hash[16], u32 0, u32 size, u32 zsize, u32 offset, u64 timestamp, u8[16] 0, u32 crc32, u32 comp
"""
import json
import os
import struct
import zlib

try:
    from . import lz4block  # type: ignore
except ImportError:  # run as a plain script (python -I does not put the script dir on sys.path)
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import lz4block  # type: ignore

MAGIC = b'POTATO70'
NG_ENTRY = 304
CLASSIC_ENTRY = 320
COMP_NONE, COMP_ZLIB, COMP_SNAPPY, COMP_DOBOZ, COMP_LZ4, COMP_LZ4HC = range(6)
COMP_NAMES = {0: 'none', 1: 'zlib', 2: 'snappy', 3: 'doboz', 4: 'lz4', 5: 'lz4hc'}


def align(v, a):
    return v if a <= 1 else (v + a - 1) // a * a


class Entry:
    __slots__ = ('name', 'hash16', 'offset', 'size', 'zsize', 'crc', 'comp', 'tail', 'payload', 'timestamp')

    def __init__(self, name, comp=COMP_NONE, hash16=b'\0' * 16, offset=0, size=0, zsize=0, crc=0,
                 tail=b'\0' * 8, payload=None, timestamp=0):
        self.name = name; self.comp = comp; self.hash16 = hash16; self.offset = offset
        self.size = size; self.zsize = zsize; self.crc = crc; self.tail = tail; self.payload = payload
        self.timestamp = timestamp

    def data(self):
        """Uncompressed data, CRC-verified."""
        z = self.payload
        if self.comp == COMP_NONE:
            u = bytes(z)
        elif self.comp == COMP_ZLIB:
            u = zlib.decompress(z)
        elif self.comp in (COMP_LZ4, COMP_LZ4HC):
            u = lz4block.decompress(z, self.size)
        else:
            raise NotImplementedError('compression %d (%s) not implemented' % (self.comp, COMP_NAMES.get(self.comp)))
        if len(u) != self.size:
            raise ValueError('%s: size %d != %d' % (self.name, len(u), self.size))
        if zlib.crc32(u) != self.crc:
            raise ValueError('%s: crc mismatch' % self.name)
        return u


class Bundle:
    def __init__(self):
        self.fmt = 'ng'
        self.entries = []          # TOC order
        self.size_field = 0
        self.data_start = 0
        self.header_raw = b''
        self.file_size = 0

    @property
    def data_order(self):
        return sorted(range(len(self.entries)), key=lambda i: self.entries[i].offset)


def read_bundle(path, load_payload=True):
    b = Bundle()
    with open(path, 'rb') as f:
        hdr = f.read(32)
        if hdr[:8] != MAGIC:
            raise ValueError('not a POTATO70 bundle: %r' % hdr[:8])
        b.header_raw = hdr
        b.size_field, tocsize, ver = struct.unpack_from('<QIH', hdr, 8)
        b.file_size = os.fstat(f.fileno()).st_size
        toc = f.read(tocsize)
        if ver == 5:
            b.fmt = 'ng'
            b.data_start = struct.unpack_from('<I', hdr, 0x16)[0]
            if b.data_start != 32 + tocsize or tocsize % NG_ENTRY or any(hdr[0x1A:0x20]):
                raise ValueError('unexpected v5 header fields: %s' % hdr.hex())
            for i in range(tocsize // NG_ENTRY):
                e = toc[i * NG_ENTRY:(i + 1) * NG_ENTRY]
                name = e[:256].split(b'\0')[0].decode('latin1')
                off, size, zsize, crc, comp = struct.unpack_from('<QIIII', e, 0x110)
                b.entries.append(Entry(name, comp, e[0x100:0x110], off, size, zsize, crc, e[0x128:0x130]))
        else:
            b.fmt = 'classic'
            b.data_start = 32 + tocsize
            for i in range(tocsize // CLASSIC_ENTRY):
                e = toc[i * CLASSIC_ENTRY:(i + 1) * CLASSIC_ENTRY]
                name = e[:256].split(b'\0')[0].decode('latin1')
                _z, size, zsize, off, ts = struct.unpack_from('<IIIIQ', e, 0x110)
                crc, comp = struct.unpack_from('<II', e, 0x138)
                b.entries.append(Entry(name, comp, e[0x100:0x110], off, size, zsize, crc, e[0x128:0x138], timestamp=ts))
        if load_payload:
            for en in b.entries:
                f.seek(en.offset)
                en.payload = f.read(en.zsize)
    return b


def compress(data, comp, level=9):
    if comp == COMP_NONE:
        return bytes(data)
    if comp == COMP_ZLIB:
        c = zlib.compressobj(level, zlib.DEFLATED, 15, 8)
        return c.compress(data) + c.flush()
    if comp in (COMP_LZ4, COMP_LZ4HC):
        return lz4block.compress(data)
    raise NotImplementedError('compression %d' % comp)


GAP_FILL_COOKER = b'AlignmentUnused'  # base-game cooker fills alignment gaps with this (truncated); REDkit uses zeros


def _gap(n, fill):
    return (fill * (n // len(fill) + 1))[:n] if fill else bytes(n)


def build_bundle_bytes(entries, data_order=None, alignment=16, pad_end=False, pad_file_to=0, gap_fill=None):
    """entries: list of Entry in TOC order with .payload (stored bytes), .size, .crc, .comp set.
    Offsets are (re)assigned here. Returns the bundle file bytes (next-gen v5 format)."""
    n = len(entries)
    tocsize = n * NG_ENTRY
    data_start = 32 + tocsize
    order = list(range(n)) if data_order is None else list(data_order)
    assert sorted(order) == list(range(n))
    body = bytearray()
    cur = data_start
    for i in order:
        e = entries[i]
        off = align(cur, alignment)
        body += _gap(off - cur, gap_fill)
        e.offset = off
        e.zsize = len(e.payload)
        body += e.payload
        cur = off + e.zsize
    size_field = align(cur, alignment) if n else data_start
    if pad_end:
        body += _gap(size_field - cur, gap_fill)
    hdr = MAGIC + struct.pack('<QIHI', size_field, tocsize, 5, data_start) + b'\0' * 6
    toc = bytearray()
    for e in entries:
        nm = e.name.encode('latin1')
        if len(nm) >= 256:
            raise ValueError('path too long (>255): %s' % e.name)
        toc += nm + b'\0' * (256 - len(nm))
        toc += (e.hash16 or b'\0' * 16)
        toc += struct.pack('<QIIII', e.offset, e.size, e.zsize, e.crc, e.comp)
        toc += (e.tail or b'\0' * 8)
    out = bytes(hdr) + bytes(toc) + bytes(body)
    if pad_file_to and len(out) % pad_file_to:
        out += b'\0' * (pad_file_to - len(out) % pad_file_to)
    return out


def make_entry(name, data, comp=COMP_ZLIB, payload=None):
    """Create an Entry from uncompressed data (payload = pre-compressed bytes to pass through)."""
    e = Entry(name.replace('/', '\\'), comp)
    e.size = len(data)
    e.crc = zlib.crc32(data)
    e.payload = payload if payload is not None else compress(data, comp)
    return e


# ---------------------------------------------------------------- extract / rebuild ---------
def detect_layout(b):
    """Infer writer parameters (alignment, end padding) from an existing bundle."""
    offs = [e.offset for e in b.entries]
    alignment = 16 if all(o % 16 == 0 for o in offs) else 1
    end = max((e.offset + e.zsize for e in b.entries), default=b.data_start)
    pad_end = b.file_size >= align(end, alignment) and b.file_size == b.size_field
    pad_file_to = 0
    if b.file_size > max(b.size_field, end):
        for p in (4096, 2048, 1024, 512):
            if b.file_size % p == 0:
                pad_file_to = p; break
    return dict(alignment=alignment, pad_end=pad_end, pad_file_to=pad_file_to, gap_fill=None)


def extract(bundle_path, outdir, keep_payload_for=(COMP_LZ4, COMP_LZ4HC, COMP_SNAPPY, COMP_DOBOZ)):
    """Extract every entry (uncompressed) to outdir/<depot path>, plus outdir/_manifest.json.
    Payloads of codecs we cannot re-encode byte-identically are kept in outdir/_payload/<i>.bin."""
    b = read_bundle(bundle_path)
    if b.fmt != 'ng':
        raise NotImplementedError('classic bundle rebuild not implemented (reader only)')
    man = dict(source=os.path.abspath(bundle_path), layout=detect_layout(b), data_order=b.data_order, entries=[])
    # detect gap filler from the first non-empty gap
    with open(bundle_path, 'rb') as f:
        spans = sorted((e.offset, e.offset + e.zsize) for e in b.entries)
        for (s0, e0), (s1, e1) in zip(spans, spans[1:]):
            if s1 > e0:
                f.seek(e0); g = f.read(s1 - e0)
                if any(g):
                    man['layout']['gap_fill'] = g.decode('latin1') if g == _gap(len(g), GAP_FILL_COOKER) else None
                break
    for i, e in enumerate(b.entries):
        u = e.data()
        p = os.path.join(outdir, *e.name.split('\\'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as f:
            f.write(u)
        me = dict(name=e.name, comp=e.comp, size=e.size, zsize=e.zsize, crc=e.crc)
        if any(e.hash16):
            me['hash16'] = e.hash16.hex()
        if any(e.tail):
            me['tail'] = e.tail.hex()
        if e.comp in keep_payload_for:
            pp = os.path.join(outdir, '_payload', '%d.bin' % i)
            os.makedirs(os.path.dirname(pp), exist_ok=True)
            with open(pp, 'wb') as f:
                f.write(e.payload)
            me['payload'] = '_payload/%d.bin' % i
        man['entries'].append(me)
    with open(os.path.join(outdir, '_manifest.json'), 'w') as f:
        json.dump(man, f, indent=1)
    return man


def rebuild(extract_dir, out_path=None, recompress_lz4=False):
    with open(os.path.join(extract_dir, '_manifest.json')) as f:
        man = json.load(f)
    entries = []
    for me in man['entries']:
        with open(os.path.join(extract_dir, *me['name'].split('\\')), 'rb') as f:
            data = f.read()
        payload = None
        if me.get('payload') and not recompress_lz4:
            with open(os.path.join(extract_dir, me['payload']), 'rb') as f:
                payload = f.read()
        e = make_entry(me['name'], data, me['comp'], payload)
        if payload is not None and me['comp'] in (COMP_LZ4, COMP_LZ4HC):
            if lz4block.decompress(payload, len(data)) != data:
                raise ValueError('payload does not decode to the extracted file: %s' % me['name'])
        if me.get('hash16'):
            e.hash16 = bytes.fromhex(me['hash16'])
        if me.get('tail'):
            e.tail = bytes.fromhex(me['tail'])
        entries.append(e)
    lay = man['layout']
    gf = lay.get('gap_fill')
    out = build_bundle_bytes(entries, man['data_order'], lay['alignment'], lay['pad_end'], lay['pad_file_to'],
                             GAP_FILL_COOKER if gf else None)
    if out_path:
        with open(out_path, 'wb') as f:
            f.write(out)
    return out


def dump(path):
    b = read_bundle(path, load_payload=False)
    print('%s: fmt=%s fileSize=%d sizeField=%d dataStart=%d entries=%d layout=%s' % (
        path, b.fmt, b.file_size, b.size_field, b.data_start, len(b.entries), detect_layout(b)))
    for i, e in enumerate(b.entries):
        print('  [%3d] off=%-10d size=%-9d zsize=%-9d crc=%08x comp=%d(%s)%s %s' % (
            i, e.offset, e.size, e.zsize, e.crc, e.comp, COMP_NAMES.get(e.comp, '?'),
            ' hash=' + e.hash16.hex() if any(e.hash16) else '', e.name))


if __name__ == '__main__':
    import sys
    cmd = sys.argv[1]
    if cmd == 'dump':
        dump(sys.argv[2])
    elif cmd == 'extract':
        m = extract(sys.argv[2], sys.argv[3]); print('extracted', len(m['entries']), 'entries ->', sys.argv[3])
    elif cmd == 'rebuild':
        out = rebuild(sys.argv[2], sys.argv[3], recompress_lz4='--recompress' in sys.argv)
        print('wrote', sys.argv[3], len(out))
    else:
        raise SystemExit('usage: w3bundle.py dump <bundle> | extract <bundle> <dir> | rebuild <dir> <out.bundle> [--recompress]')
