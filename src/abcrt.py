"""Round-trip AVM2 (ABC) reader/writer + full disassembler/assembler.

  python -I abcrt.py roundtrip <file.swf(FWS)>         parse every DoABC -> serialize -> compare; also
                                                       decode/encode every method body's code
  python -I abcrt.py classes <file.swf> [substr..]     dump class headers + traits
  python -I abcrt.py dis <file.swf> Class::method ...  disassemble (Class::* for all methods of a class)
  python -I abcrt.py grep <file.swf> text ...          list methods whose disassembly contains all texts
"""
import struct, sys

# --------------------------------------------------------------------------- primitive IO
class R:
    def __init__(s, b, p=0):
        s.b = b; s.p = p
    def u8(s):
        v = s.b[s.p]; s.p += 1; return v
    def u16(s):
        v = struct.unpack_from('<H', s.b, s.p)[0]; s.p += 2; return v
    def u32(s):
        v = struct.unpack_from('<I', s.b, s.p)[0]; s.p += 4; return v
    def u30(s):
        r = 0; sh = 0
        for _ in range(5):
            c = s.b[s.p]; s.p += 1
            r |= (c & 0x7f) << sh
            if not c & 0x80:
                break
            sh += 7
        return r & 0xFFFFFFFF
    def s32(s):
        v = s.u30()
        return v - 0x100000000 if v & 0x80000000 else v
    def s24(s):
        v = s.b[s.p] | (s.b[s.p + 1] << 8) | (s.b[s.p + 2] << 16); s.p += 3
        return v - 0x1000000 if v & 0x800000 else v
    def d64(s):
        v = s.b[s.p:s.p + 8]; s.p += 8; return v  # keep raw bytes (exact round trip, NaN payloads)
    def raw(s, n):
        v = s.b[s.p:s.p + n]; s.p += n; return bytes(v)


class W:
    def __init__(s):
        s.a = bytearray()
    def u8(s, v): s.a.append(v & 0xff)
    def u16(s, v): s.a += struct.pack('<H', v)
    def u32(s, v): s.a += struct.pack('<I', v)
    def u30(s, v):
        v &= 0xFFFFFFFF
        while True:
            c = v & 0x7f; v >>= 7
            if v:
                s.a.append(c | 0x80)
            else:
                s.a.append(c); break
    def s32(s, v): s.u30(v & 0xFFFFFFFF)
    def s24(s, v):
        v &= 0xFFFFFF; s.a += bytes((v & 0xff, (v >> 8) & 0xff, v >> 16))
    def raw(s, b): s.a += b


# --------------------------------------------------------------------------- ABC model
class Trait:
    __slots__ = ('name', 'kind', 'attr', 'slot_id', 'type_name', 'vindex', 'vkind', 'disp_id', 'method',
                 'classi', 'function', 'metadata')
    def __init__(s):
        s.metadata = []; s.vkind = None


class ABC:
    def __init__(s, data):
        r = R(data)
        s.minor = r.u16(); s.major = r.u16()
        n = r.u30(); s.ints = [0] + [r.s32() for _ in range(max(0, n - 1))]
        n = r.u30(); s.uints = [0] + [r.u30() for _ in range(max(0, n - 1))]
        n = r.u30(); s.doubles = [b''] + [r.d64() for _ in range(max(0, n - 1))]
        n = r.u30(); s.strings = [None]
        for _ in range(max(0, n - 1)):
            l = r.u30(); s.strings.append(r.raw(l))
        n = r.u30(); s.nss = [None] + [(r.u8(), r.u30()) for _ in range(max(0, n - 1))]
        n = r.u30(); s.nssets = [None]
        for _ in range(max(0, n - 1)):
            c = r.u30(); s.nssets.append([r.u30() for _ in range(c)])
        n = r.u30(); s.mns = [None]
        for _ in range(max(0, n - 1)):
            k = r.u8()
            if k in (0x07, 0x0D): s.mns.append((k, r.u30(), r.u30()))
            elif k in (0x0F, 0x10): s.mns.append((k, r.u30()))
            elif k in (0x11, 0x12): s.mns.append((k,))
            elif k in (0x09, 0x0E): s.mns.append((k, r.u30(), r.u30()))
            elif k in (0x1B, 0x1C): s.mns.append((k, r.u30()))
            elif k == 0x1D:
                q = r.u30(); c = r.u30(); s.mns.append((k, q, [r.u30() for _ in range(c)]))
            else:
                raise ValueError('multiname kind %x' % k)
        n = r.u30(); s.methods = []
        for _ in range(n):
            m = {}
            pc = r.u30(); m['ret'] = r.u30(); m['params'] = [r.u30() for _ in range(pc)]
            m['name'] = r.u30(); m['flags'] = r.u8()
            m['options'] = [(r.u30(), r.u8()) for _ in range(r.u30())] if m['flags'] & 0x08 else None
            m['pnames'] = [r.u30() for _ in range(pc)] if m['flags'] & 0x80 else None
            s.methods.append(m)
        n = r.u30(); s.metadata = []
        for _ in range(n):
            nm = r.u30(); c = r.u30()
            s.metadata.append((nm, [r.u30() for _ in range(2 * c)]))
        nc = r.u30(); s.instances = []
        for _ in range(nc):
            i = {'name': r.u30(), 'super': r.u30(), 'flags': r.u8()}
            i['protns'] = r.u30() if i['flags'] & 0x08 else None
            i['interfaces'] = [r.u30() for _ in range(r.u30())]
            i['iinit'] = r.u30(); i['traits'] = s._traits(r)
            s.instances.append(i)
        s.classes = [{'cinit': r.u30(), 'traits': s._traits(r)} for _ in range(nc)]
        s.scripts = [{'init': r.u30(), 'traits': s._traits(r)} for _ in range(r.u30())]
        s.bodies = []
        for _ in range(r.u30()):
            b = {'method': r.u30(), 'max_stack': r.u30(), 'locals': r.u30(), 'init_scope': r.u30(),
                 'max_scope': r.u30()}
            b['code'] = r.raw(r.u30())
            b['exceptions'] = [[r.u30() for _ in range(5)] for _ in range(r.u30())]
            b['traits'] = s._traits(r)
            s.bodies.append(b)
        assert r.p == len(data), (r.p, len(data))
        s.body_of = {b['method']: b for b in s.bodies}

    def _traits(s, r):
        out = []
        for _ in range(r.u30()):
            t = Trait(); t.name = r.u30(); k = r.u8(); t.kind = k & 0x0f; t.attr = k >> 4
            if t.kind in (0, 6):
                t.slot_id = r.u30(); t.type_name = r.u30(); t.vindex = r.u30()
                if t.vindex: t.vkind = r.u8()
            elif t.kind in (1, 2, 3):
                t.disp_id = r.u30(); t.method = r.u30()
            elif t.kind == 4:
                t.slot_id = r.u30(); t.classi = r.u30()
            elif t.kind == 5:
                t.slot_id = r.u30(); t.function = r.u30()
            else:
                raise ValueError('trait kind %d' % t.kind)
            if t.attr & 0x04:
                t.metadata = [r.u30() for _ in range(r.u30())]
            out.append(t)
        return out

    # ------------------------------------------------------------------ serialize
    def serialize(s):
        w = W(); w.u16(s.minor); w.u16(s.major)
        def cnt(lst): w.u30(len(lst) if len(lst) > 1 else 0)
        cnt(s.ints); [w.s32(v) for v in s.ints[1:]]
        cnt(s.uints); [w.u30(v) for v in s.uints[1:]]
        cnt(s.doubles); [w.raw(v) for v in s.doubles[1:]]
        cnt(s.strings)
        for v in s.strings[1:]: w.u30(len(v)); w.raw(v)
        cnt(s.nss)
        for k, n in s.nss[1:]: w.u8(k); w.u30(n)
        cnt(s.nssets)
        for st in s.nssets[1:]: w.u30(len(st)); [w.u30(x) for x in st]
        cnt(s.mns)
        for m in s.mns[1:]:
            w.u8(m[0])
            if m[0] == 0x1D:
                w.u30(m[1]); w.u30(len(m[2])); [w.u30(x) for x in m[2]]
            else:
                [w.u30(x) for x in m[1:]]
        w.u30(len(s.methods))
        for m in s.methods:
            w.u30(len(m['params'])); w.u30(m['ret']); [w.u30(x) for x in m['params']]
            w.u30(m['name']); w.u8(m['flags'])
            if m['flags'] & 0x08:
                w.u30(len(m['options']))
                for v, k in m['options']: w.u30(v); w.u8(k)
            if m['flags'] & 0x80: [w.u30(x) for x in m['pnames']]
        w.u30(len(s.metadata))
        for nm, items in s.metadata: w.u30(nm); w.u30(len(items) // 2); [w.u30(x) for x in items]
        w.u30(len(s.instances))
        for i in s.instances:
            w.u30(i['name']); w.u30(i['super']); w.u8(i['flags'])
            if i['flags'] & 0x08: w.u30(i['protns'])
            w.u30(len(i['interfaces'])); [w.u30(x) for x in i['interfaces']]
            w.u30(i['iinit']); s._wtraits(w, i['traits'])
        for c in s.classes: w.u30(c['cinit']); s._wtraits(w, c['traits'])
        w.u30(len(s.scripts))
        for sc in s.scripts: w.u30(sc['init']); s._wtraits(w, sc['traits'])
        w.u30(len(s.bodies))
        for b in s.bodies:
            for k in ('method', 'max_stack', 'locals', 'init_scope', 'max_scope'): w.u30(b[k])
            w.u30(len(b['code'])); w.raw(b['code'])
            w.u30(len(b['exceptions']))
            for e in b['exceptions']: [w.u30(x) for x in e]
            s._wtraits(w, b['traits'])
        return bytes(w.a)

    def _wtraits(s, w, traits):
        w.u30(len(traits))
        for t in traits:
            w.u30(t.name); w.u8(t.kind | (t.attr << 4))
            if t.kind in (0, 6):
                w.u30(t.slot_id); w.u30(t.type_name); w.u30(t.vindex)
                if t.vindex: w.u8(t.vkind)
            elif t.kind in (1, 2, 3): w.u30(t.disp_id); w.u30(t.method)
            elif t.kind == 4: w.u30(t.slot_id); w.u30(t.classi)
            elif t.kind == 5: w.u30(t.slot_id); w.u30(t.function)
            if t.attr & 0x04: w.u30(len(t.metadata)); [w.u30(x) for x in t.metadata]

    # ------------------------------------------------------------------ pool helpers
    def str(s, i):
        return '' if not i else s.strings[i].decode('utf8', 'replace')

    NSK = {0x08: 'ns', 0x16: 'pkg', 0x17: 'pkgint', 0x18: 'prot', 0x19: 'expl', 0x1A: 'sprot', 0x05: 'priv'}

    def nsname(s, i):
        if not i: return '*'
        k, n = s.nss[i]
        return '%s:%s' % (s.NSK.get(k, hex(k)), s.str(n))

    def mn(s, i, full=False):
        if not i: return '*'
        m = s.mns[i]; k = m[0]
        if k in (0x07, 0x0D):
            if full: return '%s::%s' % (s.nsname(m[1]), s.str(m[2]))
            return s.str(m[2])
        if k in (0x09, 0x0E): return s.str(m[1])
        if k in (0x0F, 0x10): return 'rtq:' + s.str(m[1])
        if k in (0x1B, 0x1C): return '[late]'
        if k in (0x11, 0x12): return '[rtqnl]'
        if k == 0x1D: return '%s.<%s>' % (s.mn(m[1]), ','.join(s.mn(x) for x in m[2]))
        return '?'

    def const(s, vindex, vkind):
        if not vindex and vkind is None: return None
        if vkind == 0x01: return repr(s.str(vindex))
        if vkind == 0x03: return s.ints[vindex]
        if vkind == 0x04: return s.uints[vindex]
        if vkind == 0x06: return struct.unpack('<d', s.doubles[vindex])[0]
        if vkind == 0x0A: return False
        if vkind == 0x0B: return True
        if vkind == 0x0C: return 'null'
        if vkind == 0x00: return 'undefined'
        return '%s(k%x)' % (vindex, vkind)

    def owners(s):
        own = {}
        for ci, (i, c) in enumerate(zip(s.instances, s.classes)):
            cn = s.mn(i['name'])
            own[i['iinit']] = cn + '::<init>'
            own[c['cinit']] = cn + '::<cinit>'
            for t in i['traits']:
                if t.kind in (1, 2, 3):
                    own[t.method] = cn + '::' + s.mn(t.name) + {1: '', 2: ' get', 3: ' set'}[t.kind]
            for t in c['traits']:
                if t.kind in (1, 2, 3):
                    own[t.method] = cn + '::static ' + s.mn(t.name) + {1: '', 2: ' get', 3: ' set'}[t.kind]
        for sc in s.scripts:
            own.setdefault(sc['init'], '<script init>')
        return own


# --------------------------------------------------------------------------- opcodes
OPS = {}
def _op(code, name, *ops):
    OPS[code] = (name, ops)
for c, n in [(0x01, 'bkpt'), (0x02, 'nop'), (0x03, 'throw'), (0x07, 'dxnslate'), (0x09, 'label'),
             (0x1c, 'pushwith'), (0x1d, 'popscope'), (0x1e, 'nextname'), (0x1f, 'hasnext'), (0x20, 'pushnull'),
             (0x21, 'pushundefined'), (0x23, 'nextvalue'), (0x26, 'pushtrue'), (0x27, 'pushfalse'),
             (0x28, 'pushnan'), (0x29, 'pop'), (0x2a, 'dup'), (0x2b, 'swap'), (0x30, 'pushscope'),
             (0x35, 'li8'), (0x36, 'li16'), (0x37, 'li32'), (0x38, 'lf32'), (0x39, 'lf64'), (0x3a, 'si8'),
             (0x3b, 'si16'), (0x3c, 'si32'), (0x3d, 'sf32'), (0x3e, 'sf64'), (0x47, 'returnvoid'),
             (0x48, 'returnvalue'), (0x50, 'sxi1'), (0x51, 'sxi8'), (0x52, 'sxi16'), (0x57, 'newactivation'),
             (0x64, 'getglobalscope'), (0x70, 'convert_s'), (0x71, 'esc_xelem'), (0x72, 'esc_xattr'),
             (0x73, 'convert_i'), (0x74, 'convert_u'), (0x75, 'convert_d'), (0x76, 'convert_b'),
             (0x77, 'convert_o'), (0x78, 'checkfilter'), (0x81, 'coerce_b'), (0x82, 'coerce_a'),
             (0x83, 'coerce_i'), (0x84, 'coerce_d'), (0x85, 'coerce_s'), (0x87, 'astypelate'),
             (0x88, 'coerce_u'), (0x89, 'coerce_o'), (0x90, 'negate'), (0x91, 'increment'), (0x93, 'decrement'),
             (0x95, 'typeof'), (0x96, 'not'), (0x97, 'bitnot'), (0xa0, 'add'), (0xa1, 'subtract'),
             (0xa2, 'multiply'), (0xa3, 'divide'), (0xa4, 'modulo'), (0xa5, 'lshift'), (0xa6, 'rshift'),
             (0xa7, 'urshift'), (0xa8, 'bitand'), (0xa9, 'bitor'), (0xaa, 'bitxor'), (0xab, 'equals'),
             (0xac, 'strictequals'), (0xad, 'lessthan'), (0xae, 'lessequals'), (0xaf, 'greaterthan'),
             (0xb0, 'greaterequals'), (0xb1, 'instanceof'), (0xb3, 'istypelate'), (0xb4, 'in'),
             (0xc0, 'increment_i'), (0xc1, 'decrement_i'), (0xc4, 'negate_i'), (0xc5, 'add_i'),
             (0xc6, 'subtract_i'), (0xc7, 'multiply_i'), (0xd0, 'getlocal0'), (0xd1, 'getlocal1'),
             (0xd2, 'getlocal2'), (0xd3, 'getlocal3'), (0xd4, 'setlocal0'), (0xd5, 'setlocal1'),
             (0xd6, 'setlocal2'), (0xd7, 'setlocal3'), (0xf3, 'timestamp')]:
    _op(c, n)
for c, n, k in [(0x04, 'getsuper', 'mn'), (0x05, 'setsuper', 'mn'), (0x06, 'dxns', 'str'), (0x08, 'kill', 'u30'),
                (0x25, 'pushshort', 'u30'), (0x2c, 'pushstring', 'str'), (0x2d, 'pushint', 'int'),
                (0x2e, 'pushuint', 'uint'), (0x2f, 'pushdouble', 'dbl'), (0x31, 'pushnamespace', 'ns'),
                (0x40, 'newfunction', 'meth'), (0x41, 'call', 'u30'), (0x42, 'construct', 'u30'),
                (0x49, 'constructsuper', 'u30'), (0x53, 'applytype', 'u30'), (0x55, 'newobject', 'u30'),
                (0x56, 'newarray', 'u30'), (0x58, 'newclass', 'cls'), (0x59, 'getdescendants', 'mn'),
                (0x5a, 'newcatch', 'u30'), (0x5d, 'findpropstrict', 'mn'), (0x5e, 'findproperty', 'mn'),
                (0x5f, 'finddef', 'mn'), (0x60, 'getlex', 'mn'), (0x61, 'setproperty', 'mn'),
                (0x62, 'getlocal', 'u30'), (0x63, 'setlocal', 'u30'), (0x66, 'getproperty', 'mn'),
                (0x67, 'getouterscope', 'u30'), (0x68, 'initproperty', 'mn'), (0x6a, 'deleteproperty', 'mn'),
                (0x6c, 'getslot', 'u30'), (0x6d, 'setslot', 'u30'), (0x6e, 'getglobalslot', 'u30'),
                (0x6f, 'setglobalslot', 'u30'), (0x80, 'coerce', 'mn'), (0x86, 'astype', 'mn'),
                (0x92, 'inclocal', 'u30'), (0x94, 'declocal', 'u30'), (0xb2, 'istype', 'mn'),
                (0xc2, 'inclocal_i', 'u30'), (0xc3, 'declocal_i', 'u30'), (0xf0, 'debugline', 'u30'),
                (0xf1, 'debugfile', 'str'), (0xf2, 'bkptline', 'u30')]:
    _op(c, n, k)
for c, n in [(0x32, 'hasnext2'), (0x43, 'callmethod'), (0x44, 'callstatic')]:
    _op(c, n, 'u30', 'u30')
for c, n in [(0x45, 'callsuper'), (0x46, 'callproperty'), (0x4a, 'constructprop'), (0x4c, 'callproplex'),
             (0x4e, 'callsupervoid'), (0x4f, 'callpropvoid')]:
    _op(c, n, 'mn', 'u30')
_op(0x24, 'pushbyte', 'u8'); _op(0x65, 'getscopeobject', 'u8')
_op(0xef, 'debug', 'u8', 'u30', 'u8', 'u30')
BR = {0x0c: 'ifnlt', 0x0d: 'ifnle', 0x0e: 'ifngt', 0x0f: 'ifnge', 0x10: 'jump', 0x11: 'iftrue', 0x12: 'iffalse',
      0x13: 'ifeq', 0x14: 'ifne', 0x15: 'iflt', 0x16: 'ifle', 0x17: 'ifgt', 0x18: 'ifge', 0x19: 'ifstricteq',
      0x1a: 'ifstrictne'}
for c, n in BR.items():
    _op(c, n, 'br')
_op(0x1b, 'lookupswitch', 'switch')
NAME2OP = {v[0]: k for k, v in OPS.items()}


def decode(code):
    """-> list of [pos, op, operands]; branch operands are absolute byte targets."""
    r = R(code); out = []
    while r.p < len(code):
        pos = r.p; op = r.u8()
        if op not in OPS:
            raise ValueError('unknown opcode %02x at %d' % (op, pos))
        name, kinds = OPS[op]; args = []
        for k in kinds:
            if k == 'u8': args.append(r.u8())
            elif k == 'br':
                off = r.s24(); args.append(r.p + off)
            elif k == 'switch':
                base = pos
                d = r.s24(); c = r.u30(); cases = [r.s24() for _ in range(c + 1)]
                args.append(base + d); args.append([base + x for x in cases])
            else:
                args.append(r.u30())
        out.append([pos, op, args])
    return out


def encode(ins):
    """ins: list of [label_or_pos, op, args] where branch targets refer to the 'pos' keys of other
    instructions (or the code end).  Returns bytes and pos->newpos map."""
    # sizes don't depend on branch offsets (s24 fixed) -> two passes
    def size(op, args):
        w = W(); _emit(w, op, args, 0, {}, True); return len(w.a)
    newpos = {}; p = 0
    for i in ins:
        newpos[i[0]] = p; p += size(i[1], i[2])
    end = p
    w = W()
    for i in ins:
        _emit(w, i[1], i[2], newpos[i[0]], newpos, False, end, i[0])
    return bytes(w.a), newpos


def _emit(w, op, args, here, newpos, dry, end=None, oldpos=None):
    w.u8(op)
    name, kinds = OPS[op]
    ai = 0
    for k in kinds:
        if k == 'u8': w.u8(args[ai]); ai += 1
        elif k == 'br':
            if dry:
                w.s24(0)
            elif args[ai] in newpos or args[ai] == 'END':
                w.s24(newpos.get(args[ai], end) - (here + 4))
            else:  # dead branch into mid-instruction / past end (compiler artifact): keep raw delta
                w.s24(args[ai] - (oldpos + 4))
            ai += 1
        elif k == 'switch':
            d = args[ai]; cases = args[ai + 1]; ai += 2
            w.s24(0 if dry else newpos[d] - here); w.u30(len(cases) - 1)
            for c in cases: w.s24(0 if dry else newpos[c] - here)
        else:
            w.u30(args[ai]); ai += 1


def fmt(abc, ins_list, owners=None):
    out = []
    for pos, op, args in ins_list:
        name, kinds = OPS[op]
        parts = []
        for k, a in zip(kinds, args):
            if k == 'mn': parts.append(abc.mn(a))
            elif k == 'str': parts.append(repr(abc.str(a)))
            elif k == 'int': parts.append(str(abc.ints[a]))
            elif k == 'uint': parts.append(str(abc.uints[a]))
            elif k == 'dbl': parts.append(str(struct.unpack('<d', abc.doubles[a])[0]))
            elif k == 'br': parts.append('->%d' % a)
            elif k == 'switch': parts.append('default->%d cases->%s' % (a, args[1]))
            elif k == 'meth': parts.append('method#%d %s' % (a, (owners or {}).get(a, '')))
            elif k == 'cls': parts.append('class#%d %s' % (a, abc.mn(abc.instances[a]['name'])))
            elif k == 'ns': parts.append(abc.nsname(a))
            else: parts.append(str(a))
        if op in (0xf0, 0xf1, 0xef):
            continue
        out.append('%5d  %-16s %s' % (pos, name, ', '.join(parts)))
    return '\n'.join(out)


# --------------------------------------------------------------------------- SWF level
def swf_tags(data):
    nbits = data[8] >> 3
    p = 8 + (5 + 4 * nbits + 7) // 8 + 4
    tags = []
    while p < len(data):
        hp = p
        cl = struct.unpack_from('<H', data, p)[0]; p += 2
        code = cl >> 6; ln = cl & 0x3f; long_ = ln == 0x3f
        if long_:
            ln = struct.unpack_from('<I', data, p)[0]; p += 4
        tags.append((code, hp, p, ln, long_))
        p += ln
        if code == 0:
            break
    return tags


def swf_abcs(data):
    out = []
    for code, hp, bp, ln, lg in swf_tags(data):
        body = data[bp:bp + ln]
        if code == 82:
            e = body.index(b'\0', 4)
            out.append((body[4:e].decode('latin1'), body[e + 1:], (hp, bp, ln, body[:e + 1])))
        elif code == 72:
            out.append(('abc', body, (hp, bp, ln, b'')))
    return out


def replace_abc(data, new_abc, which=0):
    """Return new FWS SWF bytes with DoABC #which replaced (header file length updated)."""
    _, _, (hp, bp, ln, prefix) = swf_abcs(data)[which]
    body = prefix + new_abc
    tag = struct.pack('<HI', (82 << 6) | 0x3f, len(body)) + body
    end_old = bp + ln
    out = bytearray(data[:hp] + tag + data[end_old:])
    struct.pack_into('<I', out, 4, len(out))
    return bytes(out)


def load(path):
    data = open(path, 'rb').read()
    if data[:3] in (b'CFX', b'CWS'):
        import zlib
        data = b'FWS' + data[3:8] + zlib.decompress(data[8:])
    hl = struct.unpack_from('<I', data, 4)[0]
    return data[:hl] if hl <= len(data) else data


def class_dump(abc, i, c):
    own = abc.owners()
    fl = i['flags']
    fls = [n for b, n in ((1, 'SEALED'), (2, 'FINAL'), (4, 'INTERFACE'), (8, 'PROTECTEDNS')) if fl & b]
    if not fl & 1: fls.append('DYNAMIC')
    lines = ['class %s extends %s  flags=0x%02x %s  iinit=#%d' % (abc.mn(i['name'], True), abc.mn(i['super'], True), fl,
                                                                '|'.join(fls), i['iinit'])]
    if i['interfaces']:
        lines.append('  implements ' + ', '.join(abc.mn(x, True) for x in i['interfaces']))
    for label, traits in (('inst', i['traits']), ('static', c['traits'])):
        for t in traits:
            if t.kind in (0, 6):
                lines.append('  %s %s %s:%s = %s  (slot %d)' % (label, 'const' if t.kind == 6 else 'var',
                             abc.mn(t.name, True), abc.mn(t.type_name), abc.const(t.vindex, t.vkind), t.slot_id))
            elif t.kind in (1, 2, 3):
                m = abc.methods[t.method]
                ps = ','.join(abc.mn(x) for x in m['params'])
                lines.append('  %s %s %s(%s):%s  #%d%s' % (label, {1: 'function', 2: 'get', 3: 'set'}[t.kind],
                             abc.mn(t.name, True), ps, abc.mn(m['ret']), t.method,
                             ' override' if t.attr & 2 else ''))
            else:
                lines.append('  %s trait kind %d %s' % (label, t.kind, abc.mn(t.name, True)))
    return '\n'.join(lines)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    cmd, path = sys.argv[1], sys.argv[2]
    data = load(path)
    args = sys.argv[3:]
    for abcname, raw, info in swf_abcs(data):
        abc = ABC(raw)
        if cmd == 'roundtrip':
            ser = abc.serialize()
            print('ABC %r: %d bytes, %d strings, %d multinames, %d methods, %d classes, %d bodies' % (
                abcname, len(raw), len(abc.strings), len(abc.mns), len(abc.methods), len(abc.instances), len(abc.bodies)))
            print('  ABC serialize byte-identical:', ser == raw)
            bad = 0; total = 0
            for b in abc.bodies:
                ins = decode(b['code']); total += len(ins)
                enc, _ = encode(ins)
                if enc != b['code']:
                    bad += 1
            print('  method bodies: %d, instructions: %d, code decode->encode mismatches: %d' % (len(abc.bodies), total, bad))
            new = replace_abc(data, ser)
            print('  SWF with re-serialized ABC identical:', new == data)
        elif cmd == 'classes':
            for i, c in zip(abc.instances, abc.classes):
                nm = abc.mn(i['name'], True)
                if not args or any(a in nm for a in args):
                    print(class_dump(abc, i, c)); print()
        elif cmd in ('dis', 'grep'):
            own = abc.owners()
            for b in abc.bodies:
                o = own.get(b['method'], '#%d' % b['method'])
                if cmd == 'dis':
                    if not any(o == w or (w.endswith('*') and o.startswith(w[:-1])) for w in args):
                        continue
                    txt = fmt(abc, decode(b['code']), own)
                    print('===== %s  (method #%d, max_stack %d, locals %d, scope %d..%d, %d bytes, exc %r)' % (
                        o, b['method'], b['max_stack'], b['locals'], b['init_scope'], b['max_scope'],
                        len(b['code']), b['exceptions']))
                    m = abc.methods[b['method']]
                    print('      params (%s) -> %s' % (', '.join(abc.mn(x) for x in m['params']), abc.mn(m['ret'])))
                    print(txt)
                else:
                    txt = fmt(abc, decode(b['code']), own)
                    if all(a in txt for a in args):
                        print('=====', o)
