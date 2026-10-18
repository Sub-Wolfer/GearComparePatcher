"""Pure-Python LZ4 *block* format codec (no frame header), as used by
Witcher 3 bundles (compression types 4 = LZ4, 5 = LZ4HC; both decode the same).
decompress(src, usize) -> bytes ; compress(data) -> bytes (greedy hash-chain, valid
LZ4 block, NOT byte-identical to liblz4 HC output)."""

def decompress(src, usize):
    src = memoryview(src)
    dst = bytearray()
    i = 0
    n = len(src)
    while i < n:
        tok = src[i]; i += 1
        ll = tok >> 4
        if ll == 15:
            while True:
                b = src[i]; i += 1
                ll += b
                if b != 255:
                    break
        if ll:
            dst += src[i:i + ll]; i += ll
        if i >= n:
            break  # last sequence: literals only
        off = src[i] | (src[i + 1] << 8); i += 2
        if off == 0:
            raise ValueError('lz4: zero offset at %d' % i)
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]; i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if start < 0:
            raise ValueError('lz4: offset before start')
        if off >= ml:
            dst += dst[start:start + ml]
        else:  # overlapping copy (run)
            chunk = dst[start:start + off]
            reps, rem = divmod(ml, off)
            dst += chunk * reps + chunk[:rem]
    if usize is not None and len(dst) != usize:
        raise ValueError('lz4: size mismatch %d != %d' % (len(dst), usize))
    return bytes(dst)


def _write_len(out, v):
    while v >= 255:
        out.append(255); v -= 255
    out.append(v)


def compress(data):
    """Greedy LZ4 block compressor honoring the LZ4 end-of-block rules
    (last 5 bytes literals, last match starts >= 12 bytes before end)."""
    data = bytes(data)
    n = len(data)
    out = bytearray()
    MINMATCH = 4
    MFLIMIT = 12
    LASTLITERALS = 5
    table = {}
    anchor = 0
    i = 0
    limit = n - MFLIMIT
    while i < limit:
        key = data[i:i + 4]
        cand = table.get(key)
        table[key] = i
        if cand is None or i - cand > 0xFFFF:
            i += 1
            continue
        # extend match
        ml = 4
        maxml = n - LASTLITERALS - i
        while ml < maxml and data[cand + ml] == data[i + ml]:
            ml += 1
        if ml < MINMATCH:
            i += 1
            continue
        lit = i - anchor
        tok_l = 15 if lit >= 15 else lit
        mlc = ml - 4
        tok_m = 15 if mlc >= 15 else mlc
        out.append((tok_l << 4) | tok_m)
        if lit >= 15:
            _write_len(out, lit - 15)
        out += data[anchor:i]
        off = i - cand
        out.append(off & 0xFF); out.append(off >> 8)
        if mlc >= 15:
            _write_len(out, mlc - 15)
        # index a few positions inside the match
        end = i + ml
        j = i + 1
        while j < end and j < limit:
            table[data[j:j + 4]] = j
            j += 2 if ml > 64 else 1
        i = end
        anchor = i
    lit = n - anchor
    tok_l = 15 if lit >= 15 else lit
    out.append(tok_l << 4)
    if lit >= 15:
        _write_len(out, lit - 15)
    out += data[anchor:]
    return bytes(out)
