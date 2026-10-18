"""Minimal CR2W (Witcher 3, v162-164) reader/rewrapper for CSwfResource (.redswf).

Usage:
  python -I cr2w.py info  <file.redswf>
  python -I cr2w.py rewrap <file.redswf> <new.swf(FWS or CFX/GFX)> <out.redswf> [--level N]
  python -I cr2w.py verify <file.redswf>      (re-wrap its own SWF, byte-compare)
"""
import struct, sys, zlib

HDR = struct.Struct('<4sIIQIIIII')  # magic ver flags ts build fileSize bufferSize crc numChunks  (40 bytes)
CHUNK = struct.Struct('<HHIIIII')    # className flags parent size offset template crc (24 bytes)


def fnv1a32(b):
    h = 0x811C9DC5
    for c in b:
        h ^= c
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


class CR2W:
    def __init__(self, data):
        self.data = data
        (self.magic, self.version, self.flags, self.ts, self.build, self.fileSize,
         self.bufferSize, self.crc, self.numChunks) = HDR.unpack_from(data, 0)
        assert self.magic == b'CR2W', self.magic
        self.tables = [list(struct.unpack_from('<III', data, 40 + 12 * i)) for i in range(10)]
        so, ss, _ = self.tables[0]
        self.strtab = data[so:so + ss]
        no, nc, _ = self.tables[1]
        self.names = []
        for i in range(nc):
            off, h = struct.unpack_from('<II', data, no + 8 * i)
            s = self.strtab[off:self.strtab.index(b'\0', off)]
            self.names.append((s.decode('latin1'), h))
        co, cc, _ = self.tables[4]
        self.chunks = [list(CHUNK.unpack_from(data, co + 24 * i)) for i in range(cc)]
        self.data_start = co + 24 * cc

    def table_bytes(self, i, entsize):
        off, cnt, _ = self.tables[i]
        return self.data[off:off + (cnt if i == 0 else cnt * entsize)]

    def chunk_data(self, i):
        c = self.chunks[i]
        return self.data[c[4]:c[4] + c[3]]

    # ---- CSwfResource payload -------------------------------------------------
    def swf_layout(self):
        """Return (props_end, buf_len_off, buf_off, buf_len, tail) inside chunk 0."""
        cd = self.chunk_data(0)
        p = 1  # leading 0 byte
        while True:
            nm, = struct.unpack_from('<H', cd, p)
            if nm == 0:
                p += 2
                break
            ty, sz = struct.unpack_from('<HI', cd, p + 2)
            p += 4 + sz  # name(2)+type(2)+size field counts itself (4)
        blen, = struct.unpack_from('<I', cd, p)
        return p, p, p + 4, blen, cd[p + 4 + blen:]


def checks(w):
    d = w.data
    out = []
    names = ['strings', 'names', 'imports', 'props', 'chunks', 'buffers', 'embedded']
    sizes = [1, 8, 8, 16, 24, 24, 24]
    for i, (nm, es) in enumerate(zip(names, sizes)):
        off, cnt, crc = w.tables[i]
        if not off and not cnt:
            continue
        tb = w.table_bytes(i, es)
        out.append(('table %s crc32' % nm, hex(crc), hex(zlib.crc32(tb))))
    for i, c in enumerate(w.chunks):
        out.append(('chunk %d crc32(data)' % i, hex(c[6]), hex(zlib.crc32(w.chunk_data(i)))))
    bad = [s for s, h in w.names if s and fnv1a32(s.encode('latin1') + bytes(1)) != h]
    out.append(('names: hash = FNV1a32(name + NUL), mismatches', 0, len(bad)))
    hdr0 = bytearray(d[:160])
    struct.pack_into('<I', hdr0, 32, 0xDEADBEEF)
    out.append(('header crc32(first 160 bytes, crc field=0xDEADBEEF)', hex(w.crc), hex(zlib.crc32(bytes(hdr0)))))
    out.append(('fileSize==len', w.fileSize, len(d)))
    out.append(('bufferSize==len', w.bufferSize, len(d)))
    return out


def rewrap(orig, newswf, level=9, keep_header_len=None):
    """Replace the SWF DataBuffer inside chunk 0, fix sizes/offsets/CRCs.
    newswf may be FWS (will be compressed to CFX) or already CFX/GFX bytes."""
    w = CR2W(orig)
    cd = w.chunk_data(0)
    props_end, lenoff, bufoff, blen, tail = w.swf_layout()
    old = cd[bufoff:bufoff + blen]
    if newswf[:3] == b'FWS':
        body = newswf[8:]
        hdr_len = keep_header_len if keep_header_len is not None else len(newswf)
        co = zlib.compressobj(level)
        comp = co.compress(body) + co.flush()
        payload = b'CFX' + newswf[3:4] + struct.pack('<I', hdr_len) + comp
    else:
        payload = newswf
    newcd = cd[:lenoff] + struct.pack('<I', len(payload)) + payload + tail
    delta = len(newcd) - len(cd)
    out = bytearray(orig[:w.data_start])
    # chunk table rewrite
    co_, cc, _ = w.tables[4]
    chunks = [list(c) for c in w.chunks]
    chunks[0][3] = len(newcd)
    chunks[0][6] = zlib.crc32(newcd)
    for c in chunks[1:]:
        c[4] += delta
    ctab = b''.join(CHUNK.pack(*c) for c in chunks)
    out[co_:co_ + len(ctab)] = ctab
    struct.pack_into('<III', out, 40 + 12 * 4, co_, cc, zlib.crc32(ctab))
    body = bytes(out) + newcd + orig[w.chunks[0][4] + w.chunks[0][3]:]
    out = bytearray(body)
    struct.pack_into('<I', out, 24, len(out))  # fileSize
    struct.pack_into('<I', out, 28, len(out))  # bufferSize (== fileSize when no buffers table)
    struct.pack_into('<I', out, 32, 0xDEADBEEF)
    struct.pack_into('<I', out, 32, zlib.crc32(bytes(out[:160])))  # header crc
    return bytes(out), old


if __name__ == '__main__':
    cmd = sys.argv[1]
    orig = open(sys.argv[2], 'rb').read()
    w = CR2W(orig)
    if cmd == 'info':
        print('version', w.version, 'flags', w.flags, 'fileSize', w.fileSize, 'bufferSize', w.bufferSize,
              'crc', hex(w.crc), 'numChunks', w.numChunks, 'len', len(orig))
        for i, t in enumerate(w.tables):
            print('table', i, t)
        for i, c in enumerate(w.chunks):
            print('chunk', i, w.names[c[0]][0], c)
        print('names', [n for n, _ in w.names])
        pe, lo, bo, bl, tail = w.swf_layout()
        cd = w.chunk_data(0)
        print('chunk0: props end @%d, SWF buffer len u32 @%d (file %d) = %d, data @%d, tail=%r' % (
            pe, lo, w.chunks[0][4] + lo, bl, bo, tail))
        sig = cd[bo:bo + 8]
        print('SWF sig', sig[:3], 'ver', sig[3], 'hdr length', struct.unpack('<I', sig[4:8])[0])
        dobj = zlib.decompressobj()
        body = dobj.decompress(cd[bo + 8:bo + bl])
        print('zlib body', len(body), '(+8 =', len(body) + 8, ') unused', len(dobj.unused_data), 'eof', dobj.eof)
        for c in checks(w):
            print('  check', c, 'OK' if len(c) == 3 and c[1] == c[2] else '')
    elif cmd == 'verify':
        pe, lo, bo, bl, tail = w.swf_layout()
        cd = w.chunk_data(0)
        cfx = cd[bo:bo + bl]
        # 1) pass-through (same compressed bytes) must be identical
        out, _ = rewrap(orig, cfx)
        print('pass-through rewrap identical:', out == orig)
        # 2) decompress -> recompress at each level, see if any reproduces the original zlib stream
        body = zlib.decompress(cfx[8:])
        fws = b'FWS' + cfx[3:8] + body
        hl = struct.unpack('<I', cfx[4:8])[0]
        print('CFX header length %d, decompressed+8 %d -> %d trailing junk bytes after End tag' % (hl, len(fws), len(fws) - hl))
        for lvl in (6, 9):
            out, _ = rewrap(orig, fws, level=lvl, keep_header_len=hl)
            print('decompress->zlib level %d->rewrap (keep header len): size %d identical %s' % (lvl, len(out), out == orig))
        out, _ = rewrap(orig, fws[:hl], level=9)
        print('rewrap with trailer dropped (proper SWF only): size %d' % len(out))
        w2 = CR2W(out)
        for c in checks(w2):
            print('  recheck', c, 'OK' if len(c) == 3 and c[1] == c[2] else '')
        open(sys.argv[3] if len(sys.argv) > 3 else 'rewrapped.redswf', 'wb').write(out)
    elif cmd == 'rewrap':
        new = open(sys.argv[3], 'rb').read()
        out, _ = rewrap(orig, new)
        open(sys.argv[4], 'wb').write(out)
        print('wrote', sys.argv[4], len(out))
