"""Witcher 3 next-gen metadata.store (version 7) reader / writer / generator.

File layout (little-endian, packed, no alignment):
  u32 magic   0x4D545603  (bytes 03 56 54 4D = "\\x03VTM")
  u32 version 7           (classic 1.32 / WolvenKit = 6, different layout - not handled here)
  u32 maxFileSizeInBundle  = max(FileInfo.sizeInBundle) over all files (incl. buffers)
  u32 maxFileSizeInMemory  = max(FileInfo.sizeInMemory)
  VLQ  stringTableSize ; u8[stringTableSize]  NUL-terminated latin-1 strings, referenced by byte offset
  VLQ  nFileInfo   ; FileInfo[n]   32 bytes: u32 nameOff (full depot path), u32 zero,
                                     u32 sizeInBundle (zsize), u32 sizeInMemory (size), u32 firstEntry (FileEntry idx,
                                     head of chain = entry in the LAST bundle containing the file), u32 firstBuffer (index
                                     into Buffers[]), u32 numBuffers, u32 compression (same codes as bundle TOC)
  VLQ  nFileEntry  ; FileEntry[n]  24 bytes: u64 offsetInBundle, u32 sizeInBundle, u32 nextEntry (0 = end; points
                                     to the same file's entry in an EARLIER bundle), u32 fileID, u32 bundleID
  VLQ  nBundle     ; BundleInfo[n] 24 bytes: u64 burstSize (= bundle header size - dataStart; 0 for buffers*.bundle),
                                     u32 dataStart (bundle header +0x16), u32 nameOff (path relative to the content
                                     dir, e.g. "blob0.bundle" or "content0\\bundles\\blob.bundle"), u32 firstFileEntry,
                                     u32 numFileEntries   (FileEntries of a bundle are contiguous)
  VLQ  nBuffers    ; u32 fileID[n]  buffer files (*.N.buffer), grouped per owner in owner-fileID order
  VLQ  nDirs       ; Dir[n]         8 bytes: u32 nameOff (single path component), u32 parentDir (root: 0 -> itself)
  VLQ  nFileInit   ; FileInit[n]   12 bytes: u32 fileID, u32 dirID, u32 nameOff (points INTO the full path string,
                                     at the file-name part)   -- non-buffer files only, fileID order
  VLQ  nHash       ; Hash[n]       16 bytes: u64 FNV-1a-64(depot path), u32 fileID, u32 pad -- non-buffer files only,
                                     sorted ascending by hash. pad = 0 in the base game; REDkit writes 0/0xFFFFFFFF
                                     (see redkit_pad) - it is struct padding, not data.
  Element index 0 of FileInfo / FileEntry / BundleInfo is an all-zero dummy (IDs are 1-based).
  VLQ = REDengine compact int: byte0 = [sign:1][more:1][value:6], following bytes = [more:1][value:7].
        REDkit encodes 0 as 0x80 ("negative zero"); readers must treat it as 0.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import w3bundle  # noqa: E402

MAGIC = 0x4D545603
FI = struct.Struct('<IIIIIIII')
FE = struct.Struct('<QIIII')
BI = struct.Struct('<QIIII')
DIR = struct.Struct('<II')
FINIT = struct.Struct('<III')
HASH = struct.Struct('<QII')
# classic v6 (WolvenKit-era) differs only in these three record layouts; rows are kept in ON-DISK field order:
#   FileInfo  : nameOff, 0, zsize, size, firstEntry, compression, firstBuffer, numBuffers
#   FileEntry : fileID, bundleID, u32 offset, u32 zsize, u32 nextEntry   (20 bytes)
#   BundleInfo: nameOff, firstFileEntry, numFileEntries, dataBlockSize, dataBlockOffset, burstSize (all u32)
V6 = dict(FI=struct.Struct('<IIIIIIII'), FE=struct.Struct('<IIIII'), BI=struct.Struct('<IIIIII'))
V7 = dict(FI=FI, FE=FE, BI=BI)


def fnv1a64(s):
    h = 0xcbf29ce484222325
    for c in s.encode('latin1'):
        h ^= c
        h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h


def read_vlq(d, p):
    b = d[p]; p += 1
    neg = b & 0x80
    v = b & 0x3F
    if b & 0x40:
        sh = 6
        while True:
            b = d[p]; p += 1
            v |= (b & 0x7F) << sh
            sh += 7
            if not b & 0x80:
                break
    return (-v if neg else v), p


def write_vlq(v):
    if v == 0:
        return b'\x80'          # REDkit quirk (negative zero); a plain 0x00 would decode the same
    neg = v < 0
    v = abs(v)
    out = bytearray()
    b = v & 0x3F
    v >>= 6
    if neg:
        b |= 0x80
    if v:
        b |= 0x40
    out.append(b)
    while v:
        b = v & 0x7F
        v >>= 7
        if v:
            b |= 0x80
        out.append(b)
    return bytes(out)


class Store:
    def __init__(self):
        self.version = 7
        self.max_in_bundle = 0
        self.max_in_memory = 0
        self.strtab = b''
        self.files = []      # FileInfo tuples (index 0 = dummy)
        self.entries = []    # FileEntry tuples
        self.bundles = []    # BundleInfo tuples
        self.buffers = []    # u32
        self.dirs = []       # (nameOff, parent)
        self.fileinit = []   # (fileID, dirID, nameOff)
        self.hashes = []     # (hash, fileID, pad)

    def s(self, off):
        return self.strtab[off:self.strtab.index(b'\0', off)].decode('latin1')


def read_store(path_or_bytes):
    d = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, 'rb').read()
    st = Store()
    magic, st.version, st.max_in_bundle, st.max_in_memory = struct.unpack_from('<IIII', d, 0)
    if magic != MAGIC:
        raise ValueError('bad magic %08x' % magic)
    if st.version not in (6, 7):
        raise NotImplementedError('metadata.store version %d' % st.version)
    L = V7 if st.version == 7 else V6
    p = 16
    n, p = read_vlq(d, p); st.strtab = bytes(d[p:p + n]); p += n

    def table(S):
        nonlocal p
        n, p = read_vlq(d, p)
        out = [S.unpack_from(d, p + i * S.size) for i in range(n)]
        p += n * S.size
        return out
    st.files = table(L['FI'])
    st.entries = table(L['FE'])
    st.bundles = table(L['BI'])
    n, p = read_vlq(d, p)
    st.buffers = list(struct.unpack_from('<%dI' % n, d, p)); p += 4 * n
    st.dirs = table(DIR)
    st.fileinit = table(FINIT)
    st.hashes = table(HASH)
    if p != len(d):
        raise ValueError('trailing bytes: parsed %d of %d' % (p, len(d)))
    return st


def write_store(st):
    out = bytearray(struct.pack('<IIII', MAGIC, st.version, st.max_in_bundle, st.max_in_memory))
    out += write_vlq(len(st.strtab)) + st.strtab
    L = V7 if st.version == 7 else V6
    for S, rows in ((L['FI'], st.files), (L['FE'], st.entries), (L['BI'], st.bundles)):
        out += write_vlq(len(rows))
        for r in rows:
            out += S.pack(*r)
    out += write_vlq(len(st.buffers)) + struct.pack('<%dI' % len(st.buffers), *st.buffers)
    for S, rows in ((DIR, st.dirs), (FINIT, st.fileinit), (HASH, st.hashes)):
        out += write_vlq(len(rows))
        for r in rows:
            out += S.pack(*r)
    return bytes(out)


# ------------------------------------------------------------------------------ generation
def _is_buffer(path):
    # "<owner>.<N>.buffer"
    if not path.endswith('.buffer'):
        return None
    stem = path[:-len('.buffer')]
    owner, _, num = stem.rpartition('.')
    return (owner, int(num)) if num.isdigit() else None


def redkit_pad(file_id, has_buffers):
    """Reproduces the 4 padding bytes REDkit leaves after each Hash record (observed on 13 REDkit stores):
    0 everywhere when the mod has buffer files, else 0 for fileID 1 and 0xFFFFFFFF for the rest."""
    return 0 if (has_buffers or file_id == 1) else 0xFFFFFFFF


def generate(bundles, pad_mode='redkit'):
    """bundles: list of (nameRelativeToContentDir, bundlePath). Order = bundle order in the store
    (REDkit / the base cooker use alphabetical order: blob0.bundle, buffers0.bundle, ...).
    Returns a Store equivalent to what REDkit writes for those bundles."""
    st = Store()
    strtab = bytearray(b'\0')

    def add_str(s):
        off = len(strtab)
        strtab.extend(s.encode('latin1') + b'\0')
        return off

    files = [(0,) * 8]
    entries = [(0,) * 5]
    binfo = [(0,) * 5]
    path_to_id = {}
    paths = [None]
    for bname, bpath in bundles:
        b = w3bundle.read_bundle(bpath, load_payload=False)
        if b.fmt != 'ng':
            raise NotImplementedError('classic bundle %s' % bpath)
        name_off = add_str(bname)
        first_fe = len(entries)
        bid = len(binfo)
        # fileIDs / FileEntries follow the DATA order (ascending offset), not the TOC order (TOC is sorted by name)
        for e in sorted(b.entries, key=lambda x: x.offset):
            fid = path_to_id.get(e.name)
            if fid is None:
                fid = len(files)
                path_to_id[e.name] = fid
                paths.append(e.name)
                files.append([add_str(e.name), 0, e.zsize, e.size, 0, 0, 0, e.comp])
            prev_head = files[fid][4]
            feid = len(entries)
            entries.append((e.offset, e.zsize, prev_head, fid, bid))
            f = files[fid]
            f[2], f[3], f[4], f[7] = e.zsize, e.size, feid, e.comp
        burst = 0 if os.path.basename(bname).lower().startswith('buffers') else b.size_field - b.data_start
        binfo.append((burst, b.data_start, name_off, first_fe, len(entries) - first_fe))
    # buffers
    buf_of = {}
    for fid in range(1, len(paths)):
        bi = _is_buffer(paths[fid])
        if bi:
            buf_of.setdefault(bi[0], []).append((bi[1], fid))
    buffers = []
    for fid in range(1, len(paths)):
        if _is_buffer(paths[fid]):
            continue
        lst = sorted(buf_of.get(paths[fid], []))
        if lst:
            files[fid][5] = len(buffers)
            files[fid][6] = len(lst)
            buffers.extend(f for _, f in lst)
    # directories: build a tree whose children are kept in first-seen order (files visited in fileID order),
    # then number it depth-first pre-order; dir names go into the string table in that numbering order.
    children = {'': []}
    for fid in range(1, len(paths)):
        p = paths[fid]
        if _is_buffer(p):
            continue
        cur = ''
        for comp in p.split('\\')[:-1]:
            nxt = cur + '\\' + comp if cur else comp
            if nxt not in children:
                children[nxt] = []
                children[cur].append(nxt)
            cur = nxt
    dirs = [(add_str(''), 0)]
    dir_ids = {'': 0}
    stack = [(c, 0) for c in reversed(children[''])]
    while stack:
        d, parent = stack.pop()
        dir_ids[d] = len(dirs)
        dirs.append((add_str(d.rpartition('\\')[2]), parent))
        stack.extend((c, dir_ids[d]) for c in reversed(children[d]))
    fileinit = []
    for fid in range(1, len(paths)):
        p = paths[fid]
        if _is_buffer(p):
            continue
        dpath, _, fname = p.rpartition('\\')
        fileinit.append((fid, dir_ids[dpath], files[fid][0] + len(p) - len(fname)))
    has_buffers = bool(buffers)
    hashes = []
    for fid in range(1, len(paths)):
        if _is_buffer(paths[fid]):
            continue
        pad = redkit_pad(fid, has_buffers) if pad_mode == 'redkit' else 0
        hashes.append((fnv1a64(paths[fid].lower()), fid, pad))
    hashes.sort()
    st.strtab = bytes(strtab)
    st.files = [tuple(f) for f in files]
    st.entries = entries
    st.bundles = binfo
    st.buffers = buffers
    st.dirs = dirs
    st.fileinit = fileinit
    st.hashes = hashes
    st.max_in_bundle = max((f[2] for f in st.files), default=0)
    st.max_in_memory = max((f[3] for f in st.files), default=0)
    return st


def generate_for_content_dir(content_dir, pad_mode='redkit'):
    names = sorted(f for f in os.listdir(content_dir) if f.lower().endswith('.bundle'))
    return generate([(n, os.path.join(content_dir, n)) for n in names], pad_mode)


def dump(st, limit=50):
    print('version %d maxInBundle %d maxInMemory %d strtab %d bytes' % (st.version, st.max_in_bundle, st.max_in_memory, len(st.strtab)))
    if st.version == 6:   # print raw rows for the classic layout
        for i, b in enumerate(st.bundles[1:], 1):
            print('  bundle[%d] %s raw=%s' % (i, st.s(b[0]), b))
        for i, f in enumerate(st.files[1:limit + 1], 1):
            print('  file[%d] %s raw=%s' % (i, st.s(f[0]), f))
        for i, e in enumerate(st.entries[1:limit + 1], 1):
            print('  entry[%d] raw=%s' % (i, e))
        print('  buffers', st.buffers[:limit], 'dirs', len(st.dirs), 'fileinit', len(st.fileinit), 'hashes', len(st.hashes))
        return
    print('bundles:')
    for i, b in enumerate(st.bundles[1:], 1):
        print('  [%d] %s burst=%d dataStart=%d firstFE=%d numFE=%d' % (i, st.s(b[2]), b[0], b[1], b[3], b[4]))
    print('files: %d (+dummy)' % (len(st.files) - 1))
    for i, f in enumerate(st.files[1:limit + 1], 1):
        print('  [%d] %s zsize=%d size=%d firstEntry=%d firstBuf=%d numBuf=%d comp=%d' % (i, st.s(f[0]), f[2], f[3], f[4], f[5], f[6], f[7]))
    print('entries: %d (+dummy)' % (len(st.entries) - 1))
    for i, e in enumerate(st.entries[1:limit + 1], 1):
        print('  [%d] off=%d zsize=%d next=%d file=%d bundle=%d' % ((i,) + tuple(e)))
    print('buffers:', st.buffers[:limit])
    print('dirs: %d' % len(st.dirs))
    for i, d in enumerate(st.dirs[:limit]):
        print('  [%d] %r parent=%d' % (i, st.s(d[0]), d[1]))
    print('fileinit: %d' % len(st.fileinit))
    for x in st.fileinit[:limit]:
        print('  file=%d dir=%d name=%r' % (x[0], x[1], st.s(x[2])))
    print('hashes: %d' % len(st.hashes))
    for h in st.hashes[:limit]:
        print('  %016x file=%d pad=%08x' % h)


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'dump':
        dump(read_store(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 50)
    elif cmd == 'gen':   # gen <contentDir> <out metadata.store>
        data = write_store(generate_for_content_dir(sys.argv[2]))
        with open(sys.argv[3], 'wb') as f:
            f.write(data)
        print('wrote', sys.argv[3], len(data), 'bytes')
    else:
        raise SystemExit('usage: w3meta.py dump <metadata.store> [n] | gen <contentDir> <out.store>')
