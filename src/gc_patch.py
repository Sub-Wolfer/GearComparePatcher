"""Gear Compare: patch panel_inventory.redswf (Smooth UI Remastered build as base).

  python -I patch_inv.py <toolsDir> <in.redswf> <out.redswf>

ABC:
  * MenuInventory.setSortingMode gets a 7th optional String (label) and a 6th entry 'StatSort' (mode 5)
  * MenuInventory.sortingModeApply maps a checked 'StatSort' to mode 5
  * SlotsListGrid.sortList: mode 5 -> data.sort(gcSorter_Stat)
  * new SlotsListGrid.gcSorter_Stat(a,b): descending by item.gcStat (missing/NaN -> -1), ties -> type sort
SWF:
  * sort popup (MC_MODULE_Filters #475): 6th row mcItemRenderer6, frame/background +45 px, close button +45 px
  * grid slot quantity field (DefineEditText #70) widened to the left so '+123' fits (right-aligned, x unchanged)
"""
import struct
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import abcrt  # noqa: E402
import cr2w   # noqa: E402
import zlib  # noqa: E402

ROW = 45.0                      # px between sort rows (900 twips in the vanilla layout)
POPUP_SPRITE = 475
RENDERER_CHAR = 315
QTY_EDITTEXT = 70
QTY_NEW_XMIN_PX = -110.0
LABEL_DEFAULT = 'Damage / Armor'


# ------------------------------------------------------------------------------------------- ABC helpers
class P:
    def __init__(s, abc):
        s.a = abc
        s.own = abc.owners()
        s.by_owner = {v: k for k, v in s.own.items()}

    def string(s, txt):
        b = txt.encode('utf-8')
        for i, v in enumerate(s.a.strings):
            if v == b:
                return i
        s.a.strings.append(b)
        return len(s.a.strings) - 1

    def body(s, owner):
        return s.a.body_of[s.by_owner[owner]]

    def ins(s, owner):
        return abcrt.decode(s.body(owner)['code'])

    def operand(s, owner, opname, name):
        """multiname index used by the first <opname> whose multiname is <name> in <owner>"""
        op = abcrt.NAME2OP[opname]
        for pos, o, args in s.ins(owner):
            if o == op and s.a.mn(args[0]) == name:
                return args[0]
        raise SystemExit('operand %s %s not found in %s' % (opname, name, owner))

    def renamed_mn(s, mn_idx, new_name):
        m = s.a.mns[mn_idx]
        si = s.string(new_name)
        if m[0] in (0x07, 0x0D):
            new = (m[0], m[1], si)
        elif m[0] in (0x09, 0x0E):
            new = (m[0], si, m[2])
        else:
            raise SystemExit('unsupported multiname kind %x' % m[0])
        if new in s.a.mns:
            return s.a.mns.index(new)
        s.a.mns.append(new)
        return len(s.a.mns) - 1


def I(op, *args):
    return [op, abcrt.NAME2OP[op], list(args)]


def assemble(items):
    """items: [key, opcode, args] ; encode wants [key, op, args]"""
    return [[k, o, a] for k, o, a in items]


def setcode(p, owner, ins, **body_fields):
    code, _ = abcrt.encode(ins)
    b = p.body(owner)
    b['code'] = code
    b.update(body_fields)


# Feature bits; also the value of MenuInventory.gcUiVersion, which the script reads.
F_SORT, F_ALWAYS, F_DUR, F_WIDE, F_POS = 1, 2, 4, 8, 32          # 16 = "Gear Compare UI present" (_marker)
ALL = F_SORT | F_ALWAYS | F_DUR | F_WIDE | F_POS
FEATURE_NAMES = {F_SORT: 'Damage / Armor sort option', F_ALWAYS: 'always-sorted grid',
                 F_DUR: 'durability above equipped items', F_WIDE: 'wider corner numbers (wear loss in red)',
                 F_POS: 'number position setting'}


def patch_abc(abc, feats=ALL):
    p = P(abc)
    if feats & F_SORT:
        _feat_sort(p)
    if feats & F_ALWAYS:
        _feat_always(p)
    if feats & F_DUR:
        patch_abc_durability(p)
    if feats & F_POS:
        _feat_pos(p)
    _marker(p, feats)
    return abc


def _feat_sort(p):
    abc = p.a
    A = 'MenuInventory::'
    G = 'SlotsListGrid::'
    names = {abc.mn(tr.name) for ins in abc.instances for tr in ins['traits']}
    names |= {abc.mn(tr.name) for cls in abc.classes for tr in cls['traits']}       # static members too
    if any('DAMAGE' in n.upper() and 'SORT' in n.upper() for n in names):
        raise PatchError('this inventory file already has its own damage / armor sort')

    # ---- 1. setSortingMode(mode, l1..l5, l6 = 'Damage / Armor')
    own = A + 'setSortingMode'
    mi = p.by_owner[own]
    meth = abc.methods[mi]
    assert len(meth['params']) == 6, 'setSortingMode already patched?'
    meth['params'].append(meth['params'][1])                     # String
    if meth['flags'] & 0x08:
        meth['options'].append((p.string(LABEL_DEFAULT), 0x01))
    else:
        meth['flags'] |= 0x08
        meth['options'] = [(p.string(LABEL_DEFAULT), 0x01)]
    if meth['flags'] & 0x80:
        meth['pnames'].append(0)
    s_key, s_label, s_checked = p.string('key'), p.string('label'), p.string('isChecked')
    out = []
    n_newarray = 0
    for pos, op, args in p.ins(own):
        name = abcrt.OPS[op][0]
        if name in ('setlocal', 'getlocal') and args[0] == 7:
            args = [8]                                             # the Array local moves past the new param
        if name == 'newarray':
            assert args[0] == 5
            n_newarray += 1
            out += [I('pushstring', s_key), I('pushstring', p.string('StatSort')),
                    I('pushstring', s_label), I('getlocal', 7),
                    I('pushstring', s_checked), I('getlocal1'), I('pushbyte', 5), I('equals'),
                    I('newobject', 3)]
            for k, it in enumerate(out[-9:]):
                it[0] = 'ssm%d' % k
            args = [6]
        out.append([pos, op, args])
    assert n_newarray == 1
    setcode(p, own, out, locals=9, max_stack=p.body(own)['max_stack'] + 3)

    # ---- 2. sortingModeApply: 'StatSort' -> 5 (inserted at the join point every branch jumps to)
    own = A + 'sortingModeApply'
    ins = p.ins(own)
    mc_sort = p.operand(own, 'getproperty', 'mcSortingMode')
    is_checked = p.operand(own, 'callproperty', 'isBoxChecked')
    join = None
    for pos, op, args in ins:                       # target of the 'TypeSort' jump
        if abcrt.OPS[op][0] == 'jump':
            join = args[0]
            break
    assert join is not None
    assert all(a[0] == join for _, o, a in ins if abcrt.OPS[o][0] == 'jump'), 'unexpected jump layout'
    out = []
    for pos, op, args in ins:
        if pos == join:
            blk = [I('getlocal0'), I('getproperty', mc_sort), I('pushstring', p.string('StatSort')),
                   I('callproperty', is_checked, 1), I('iffalse', 'orig_join'), I('pushbyte', 5), I('setlocal2')]
            blk[0][0] = join                         # existing branches to <join> now land on the new check
            for k, it in enumerate(blk[1:]):
                it[0] = 'sma%d' % k
            out += blk
            out.append(['orig_join', op, args])
        else:
            out.append([pos, op, args])
    setcode(p, own, out, max_stack=max(p.body(own)['max_stack'], 3))

    # ---- 3. new SlotsListGrid.gcSorter_Stat
    price = G + 'inventorySorter_Price'
    pm = abc.methods[p.by_owner[price]]
    pb = p.body(price)
    gc_stat = p.renamed_mn(p.operand(price, 'getproperty', 'price'), 'gcStat')
    type_sorter = p.operand(price, 'callproperty', 'inventorySorter_Type')
    grid_ci = next(ci for ci, i in enumerate(abc.instances) if abc.mn(i['name']) == 'SlotsListGrid')
    type_trait = next(t for t in abc.instances[grid_ci]['traits']
                      if t.kind == 1 and abc.mn(t.name) == 'inventorySorter_Type')
    sorter_mn = p.renamed_mn(type_trait.name, 'gcSorter_Stat')
    abc.methods.append({'ret': pm['ret'], 'params': list(pm['params']), 'name': 0, 'flags': 0,
                        'options': None, 'pnames': None})
    new_mi = len(abc.methods) - 1
    code = [
        I('getlocal0'), I('pushscope'),
        I('getlocal1'), I('getproperty', gc_stat), I('convert_d'), I('setlocal3'),
        I('getlocal2'), I('getproperty', gc_stat), I('convert_d'), I('setlocal', 4),
        I('getlocal3'), I('getlocal3'), I('ifeq', 'A'),
        I('pushbyte', 255), I('convert_d'), I('setlocal3'),
        ['A', abcrt.NAME2OP['getlocal'], [4]], I('getlocal', 4), I('ifeq', 'B'),
        I('pushbyte', 255), I('convert_d'), I('setlocal', 4),
        ['B', abcrt.NAME2OP['getlocal3'], []], I('getlocal', 4), I('ifeq', 'TIE'),
        I('getlocal', 4), I('getlocal3'), I('subtract'), I('returnvalue'),
        ['TIE', abcrt.NAME2OP['getlocal0'], []], I('getlocal1'), I('getlocal2'),
        I('callproperty', type_sorter, 2), I('returnvalue'),
    ]
    for k, it in enumerate(code):
        if it[0] in (None,) or (isinstance(it[0], str) and it[0] not in ('A', 'B', 'TIE')):
            it[0] = 'gs%d' % k
    enc, _ = abcrt.encode(code)
    abc.bodies.append({'method': new_mi, 'max_stack': 3, 'locals': 5, 'init_scope': pb['init_scope'],
                       'max_scope': pb['max_scope'], 'code': enc, 'exceptions': [], 'traits': []})
    abc.body_of[new_mi] = abc.bodies[-1]
    t = abcrt.Trait()
    t.name = sorter_mn
    t.kind = 1
    t.attr = 0
    t.disp_id = 0
    t.method = new_mi
    abc.instances[grid_ci]['traits'].append(t)

    # ---- 4. sortList: mode 5 -> data.sort(gcSorter_Stat)
    own = G + 'sortList'
    ins = p.ins(own)
    last_sort = p.operand(own, 'getproperty', '_lastSetSort')
    sort_mn = p.operand(own, 'callpropvoid', 'sort')
    out = []
    done = False
    for idx, (pos, op, args) in enumerate(ins):
        out.append([pos, op, args])
        if not done and abcrt.OPS[op][0] == 'pushscope':
            nxt = ins[idx + 1][0]
            blk = [I('getlocal0'), I('getproperty', last_sort), I('pushbyte', 5), I('ifne', nxt),
                   I('getlocal1'), I('iffalse', nxt),
                   I('getlocal1'), I('getlocal0'), I('getproperty', sorter_mn), I('callpropvoid', sort_mn, 1),
                   I('returnvoid')]
            for k, it in enumerate(blk):
                it[0] = 'sl%d' % k
            out += blk
            done = True
    assert done
    setcode(p, own, out, max_stack=max(p.body(own)['max_stack'], 3))



def _feat_always(p):
    abc = p.a
    G = 'SlotsListGrid::'
    # ---- 5. populateData: always sort by the current mode (mouse too), like the game does for controllers.
    #      local2 = "mouse and not told to sort"; while the static SlotsListGrid.gcAlwaysSort is true (default) it is
    #      forced false, so stored grid positions are ignored. MenuInventory.gcSetAlwaysSort(b) is the script's switch.
    own = G + 'populateData'
    ins = p.ins(own)
    k = next(i for i, (pos, op, args) in enumerate(ins)
             if abcrt.OPS[op][0] == 'getproperty' and abc.mn(args[0]) == 'ignoreNextGridPosition')
    j = next(i for i in range(k, k + 6) if abcrt.OPS[ins[i][1]][0] == 'setlocal2')

    grid_ci = next(ci for ci, x in enumerate(abc.instances) if abc.mn(x['name']) == 'SlotsListGrid')
    hsb = next(tr for tr in abc.instances[grid_ci]['traits'] if abc.mn(tr.name) == 'handleScrollBar')   # public Boolean
    mn_flag = p.renamed_mn(hsb.name, 'gcAlwaysSort')
    flag = abcrt.Trait()
    flag.name, flag.kind, flag.attr, flag.slot_id = mn_flag, 0, 0, 0
    flag.type_name, flag.vindex, flag.vkind = hsb.type_name, 0x0B, 0x0B                # Boolean = true
    abc.classes[grid_ci]['traits'].append(flag)
    grid_mn = abc.instances[grid_ci]['name']

    out = []
    for i, (pos, op, args) in enumerate(ins):
        if i == j:
            first = [pos, abcrt.NAME2OP['getlex'], [grid_mn]]                    # keeps the original key
            out += [first, I('getproperty', mn_flag), I('iffalse', 'pd_keep'),
                    I('pop'), I('pushfalse')]
            for n, it in enumerate(out[-4:]):
                it[0] = 'pd%d' % n
            out.append(['pd_keep', op, args])
        else:
            out.append([pos, op, args])
    setcode(p, own, out, max_stack=p.body(own)['max_stack'] + 1)

    # MenuInventory.gcSetAlwaysSort(on:Boolean)
    A = 'MenuInventory::'
    mci = next(ci for ci, x in enumerate(abc.instances) if abc.mn(x['name']) == 'MenuInventory')
    ssm = next(tr for tr in abc.instances[mci]['traits'] if abc.mn(tr.name) == 'setSortingMode')
    void_mn = abc.methods[ssm.method]['ret']
    code = [I('getlocal0'), I('pushscope'), I('getlex', grid_mn), I('getlocal1'), I('setproperty', mn_flag),
            I('returnvoid')]
    add_method(p, mci, p.renamed_mn(ssm.name, 'gcSetAlwaysSort'), [hsb.type_name], void_mn, code, 2, 2,
               p.body(A + 'setSortingMode'))


POS_MARGIN_PX = 6                # same inset the game uses for the corner icons


def _feat_pos(p):
    """Where the +/- sits in grid slots: static InventorySlotOverlay.gcQtyPos (0 = untouched, 1 bottom right,
    2 bottom left, 3 top right, 4 top left, 5 centre), set by MenuInventory.gcSetQtyPos(int). Text wider than the slot
    is scaled down to fit (font size comes from the script).
    InventorySlotOverlay.gcPlaceQty() runs at the end of realignIcons (SlotBase.updateData calls SetQuantity before
    updateIcons). Only Gear Compare's text moves (it starts with + - or =); stack counts and every other number stay
    where the game puts them: the field's original x is kept in gcQtyX0 and put back for them."""
    abc = p.a
    O = 'InventorySlotOverlay::'
    ci = next(i for i, x in enumerate(abc.instances) if abc.mn(x['name']) == 'InventorySlotOverlay')
    traits = abc.instances[ci]['traits']
    tq = next(t for t in traits if abc.mn(t.name) == 'tfQuantity')
    t_void = abc.methods[p.by_owner[O + 'SetQuantity']]['ret']
    mci = next(i for i, x in enumerate(abc.instances) if abc.mn(x['name']) == 'MenuInventory')
    t_int = next(t for t in abc.instances[mci]['traits'] if abc.mn(t.name) == 'currentSortingMode').type_name
    t_num = next(t.type_name for ins in abc.instances for t in ins['traits']
                 if t.kind == 0 and abc.mn(t.type_name) == 'Number')
    R = O + 'realignIcons'
    mn_tf = p.operand(R, 'getproperty', 'tfQuantity')
    mn_rect = p.operand(R, 'getproperty', '_targetRect')
    mn_x = p.operand(R, 'getproperty', 'x')
    mn_y = p.operand(R, 'getproperty', 'y')
    mn_w = p.operand(R, 'getproperty', 'width')
    mn_h = p.operand(R, 'getproperty', 'height')
    mn_th = p.operand(R, 'getproperty', 'textHeight')
    mn_tw = p.renamed_mn(mn_th, 'textWidth')
    mn_text = p.renamed_mn(mn_th, 'text')
    mn_charat = p.renamed_mn(mn_th, 'charAt')
    mn_pos = p.renamed_mn(tq.name, 'gcQtyPos')
    mn_x0 = p.renamed_mn(tq.name, 'gcQtyX0')
    mn_place = p.renamed_mn(tq.name, 'gcPlaceQty')
    cls_mn = abc.instances[ci]['name']

    st = abcrt.Trait()                                      # static gcQtyPos:int = 0
    st.name, st.kind, st.attr, st.slot_id, st.type_name, st.vindex, st.vkind = mn_pos, 0, 0, 0, t_int, 0, 0
    abc.classes[ci]['traits'].append(st)
    add_slot(abc, ci, mn_x0, t_num)                         # gcQtyX0:Number = NaN

    def rect(prop):
        return [I('getlocal0'), I('getproperty', mn_rect), I('getproperty', prop)]

    def tf(*more):
        return [I('getlocal0'), I('getproperty', mn_tf)] + list(more)

    m = POS_MARGIN_PX
    mn_sx = p.renamed_mn(mn_th, 'scaleX')
    mn_sy = p.renamed_mn(mn_th, 'scaleY')

    def scale_to(*val):
        return tf(*val, I('setproperty', mn_sx)) + tf(*val, I('setproperty', mn_sy))

    # locals: 1 pos (1..5), 2 right edge of the text, 3 scaled text width, 4 first char, 5 scale
    code = [
        I('getlocal0'), I('pushscope'),
        I('getlocal0'), I('getproperty', mn_rect), I('iffalse', 'P_END'),
        I('getlocal0'), I('getproperty', mn_tf), I('iffalse', 'P_END'),
        # remember the game's x once (NaN != NaN)
        I('getlocal0'), I('getproperty', mn_x0), I('getlocal0'), I('getproperty', mn_x0), I('ifeq', 'P_HAVE'),
        I('getlocal0')] + tf(I('getproperty', mn_x)) + [I('setproperty', mn_x0),
        L('P_HAVE', 'getlex', cls_mn), I('getproperty', mn_pos), I('convert_i'), I('setlocal1'),
        I('getlocal1'), I('iffalse', 'P_END')] + tf(
        I('getproperty', mn_text), I('pushbyte', 0), I('callproperty', mn_charat, 1), I('coerce_s'),
        I('setlocal', 4)) + [
        I('getlocal', 4), I('pushstring', p.string('+')), I('ifeq', 'P_OK'),
        I('getlocal', 4), I('pushstring', p.string('-')), I('ifeq', 'P_OK'),
        I('getlocal', 4), I('pushstring', p.string('=')), I('ifeq', 'P_OK')
        ] + scale_to(I('pushbyte', 1)) + tf(I('getlocal0'), I('getproperty', mn_x0), I('setproperty', mn_x)) + [
        I('jump', 'P_END'),                                                 # not ours: the game's place and size
        # shrink to fit the icon: scale = (slot width - 2 margins) / text width, at most 1
        L('P_OK', 'getlocal0')] + [I('getproperty', mn_rect), I('getproperty', mn_w), I('pushbyte', 2 * m),
        I('subtract')] + tf(I('getproperty', mn_tw)) + [I('divide'), I('convert_d'), I('setlocal', 5),
        I('getlocal', 5), I('pushbyte', 1), I('iflt', 'P_SC'),
        I('pushbyte', 1), I('convert_d'), I('setlocal', 5),
        L('P_SC', 'getlocal0')] + [I('getproperty', mn_tf), I('getlocal', 5), I('setproperty', mn_sx)] + tf(
        I('getlocal', 5), I('setproperty', mn_sy)) + tf(
        I('getproperty', mn_tw), I('getlocal', 5), I('multiply'), I('convert_d'), I('setlocal3')) + rect(mn_x) + \
        rect(mn_w) + [I('add'), I('pushbyte', m), I('subtract'), I('convert_d'), I('setlocal2'),
        I('getlocal1'), I('pushbyte', 2), I('ifeq', 'P_LEFT'),
        I('getlocal1'), I('pushbyte', 4), I('ifeq', 'P_LEFT'),
        I('getlocal1'), I('pushbyte', 5), I('ifeq', 'P_MIDX'),
        I('jump', 'P_Y'),
        L('P_LEFT', 'getlocal0'), I('getproperty', mn_rect), I('getproperty', mn_x), I('pushbyte', m), I('add'),
        I('getlocal3'), I('add'), I('convert_d'), I('setlocal2'), I('jump', 'P_Y'),
        L('P_MIDX', 'getlocal0'), I('getproperty', mn_rect), I('getproperty', mn_x)] + rect(mn_w) + [
        I('pushbyte', 2), I('divide'), I('add'),
        I('getlocal3'), I('pushbyte', 2), I('divide'), I('add'), I('convert_d'), I('setlocal2'),
        L('P_Y', 'getlocal1'), I('pushbyte', 3), I('ifeq', 'P_TOP'),
        I('getlocal1'), I('pushbyte', 4), I('ifeq', 'P_TOP'),
        I('getlocal1'), I('pushbyte', 5), I('ifeq', 'P_MIDY'),
        # bottom: like the game, text bottom 'm' above the slot bottom
        I('getlocal0'), I('getproperty', mn_tf)] + rect(mn_y) + rect(mn_h) + [I('add')] + tf(
        I('getproperty', mn_th)) + [I('getlocal', 5), I('multiply'), I('subtract'), I('pushbyte', m), I('subtract'),
        I('setproperty', mn_y), I('jump', 'P_SETX'),
        L('P_TOP', 'getlocal0'), I('getproperty', mn_tf)] + rect(mn_y) + [I('pushbyte', 2), I('add'),
        I('setproperty', mn_y), I('jump', 'P_SETX'),
        L('P_MIDY', 'getlocal0'), I('getproperty', mn_tf)] + rect(mn_y) + rect(mn_h) + [I('pushbyte', 2),
        I('divide'), I('add')] + tf(I('getproperty', mn_th)) + [I('getlocal', 5), I('multiply'), I('pushbyte', 2),
        I('divide'), I('subtract'), I('pushbyte', 2), I('subtract'), I('setproperty', mn_y),
        # right-aligned field: the text's right edge = x + width - 2 (gutter); width is already scaled
        L('P_SETX', 'getlocal0'), I('getproperty', mn_tf), I('getlocal2')] + tf(I('getproperty', mn_w)) + [
        I('subtract'), I('pushbyte', 2), I('add'), I('setproperty', mn_x),
        L('P_END', 'returnvoid'),
    ]
    labels = ('P_END', 'P_HAVE', 'P_OK', 'P_SC', 'P_LEFT', 'P_MIDX', 'P_Y', 'P_TOP', 'P_MIDY', 'P_SETX')
    add_method(p, ci, mn_place, [], t_void, code, 6, 6, p.body(O + 'SetQuantity'), labels)

    # realignIcons: place it before every return (after the game's layout)
    out, n = [], 0
    for pos, op, args in p.ins(R):
        if abcrt.OPS[op][0] == 'returnvoid':
            out += [[pos, abcrt.NAME2OP['getlocal0'], []], ['qp%d' % n, abcrt.NAME2OP['callpropvoid'], [mn_place, 0]],
                    ['qp%d_r' % n, op, args]]
            n += 1
        else:
            out.append([pos, op, args])
    assert n >= 1
    setcode(p, R, out, max_stack=max(p.body(R)['max_stack'], 2))

    # MenuInventory.gcSetQtyPos(pos:int)
    ssm = next(tr for tr in abc.instances[mci]['traits'] if abc.mn(tr.name) == 'setSortingMode')
    code = [I('getlocal0'), I('pushscope'), I('getlex', cls_mn), I('getlocal1'), I('setproperty', mn_pos),
            I('returnvoid')]
    add_method(p, mci, p.renamed_mn(ssm.name, 'gcSetQtyPos'), [t_int], t_void, code, 2, 2,
               p.body('MenuInventory::setSortingMode'))


def _marker(p, feats):
    abc = p.a
    # ---- 6. marker the script reads to know Gear Compare's inventory UI is the one loaded:
    #      MenuInventory.gcUiVersion:int = feature bits (another mod's or the game's panel has no such member -> 0)
    value = 16 | feats                                   # 16 = "Gear Compare UI present" even with no feature
    mci = next(i for i, x in enumerate(abc.instances) if abc.mn(x['name']) == 'MenuInventory')
    cur = next(t for t in abc.instances[mci]['traits'] if abc.mn(t.name) == 'currentSortingMode')
    if value not in abc.ints:
        abc.ints.append(value)
    t = abcrt.Trait()
    t.name, t.kind, t.attr, t.slot_id = p.renamed_mn(cur.name, 'gcUiVersion'), 0, 0, 0
    t.type_name, t.vindex, t.vkind = cur.type_name, abc.ints.index(value), 0x03
    abc.instances[mci]['traits'].append(t)


def add_method(p, ci, trait_mn, params, ret, code, locals_, max_stack, like_body, labels=()):
    abc = p.a
    abc.methods.append({'ret': ret, 'params': params, 'name': 0, 'flags': 0, 'options': None, 'pnames': None})
    mi = len(abc.methods) - 1
    for k, it in enumerate(code):
        if not isinstance(it[0], str) or (it[0] not in LABELS and it[0] not in labels):
            it[0] = 'm%d_%d' % (mi, k)
    enc, _ = abcrt.encode(code)
    abc.bodies.append({'method': mi, 'max_stack': max_stack, 'locals': locals_, 'init_scope': like_body['init_scope'],
                       'max_scope': like_body['max_scope'], 'code': enc, 'exceptions': [], 'traits': []})
    abc.body_of[mi] = abc.bodies[-1]
    t = abcrt.Trait()
    t.name, t.kind, t.attr, t.disp_id, t.method = trait_mn, 1, 0, 0, mi
    abc.instances[ci]['traits'].append(t)
    return mi


def add_slot(abc, ci, name_mn, type_mn):
    t = abcrt.Trait()
    t.name, t.kind, t.attr, t.slot_id, t.type_name, t.vindex = name_mn, 0, 0, 0, type_mn, 0
    abc.instances[ci]['traits'].append(t)


LABELS = ('END', 'L2', 'POS2', 'SKIP', 'NOBARS', 'NR1', 'NR2', 'GRID')
PD_UP_PX = 18
BAR_INSET_X, BAR_INSET_Y = 2, 4
DUR_TEXT_W_PX = 60
DUR_GAP_PX = 9


def L(key, op, *args):
    return [key, abcrt.NAME2OP[op], list(args)]


def patch_abc_durability(p):
    """InventorySlotOverlay gets gcTfDurability / gcIconDurability (timeline instances), gcDurText,
    gcSetDurability(text) and gcUpdateDurability(); SlotBase.updateData passes _data.gcDurText."""
    abc = p.a
    O = 'InventorySlotOverlay::'
    ci = next(i for i, x in enumerate(abc.instances) if abc.mn(x['name']) == 'InventorySlotOverlay')
    traits = abc.instances[ci]['traits']
    tq = next(t for t in traits if abc.mn(t.name) == 'tfQuantity')
    ir = next(t for t in traits if abc.mn(t.name) == 'mcIconRepair')
    sq = abc.methods[p.by_owner[O + 'SetQuantity']]
    t_string, t_void = sq['params'][0], sq['ret']
    mn_tf = p.renamed_mn(tq.name, 'gcTfDurability')
    mn_ic = p.renamed_mn(tq.name, 'gcIconDurability')
    mn_txt = p.renamed_mn(tq.name, 'gcDurText')
    mn_set = p.renamed_mn(tq.name, 'gcSetDurability')
    mn_upd = p.renamed_mn(tq.name, 'gcUpdateDurability')
    add_slot(abc, ci, mn_tf, tq.type_name)
    add_slot(abc, ci, mn_ic, ir.type_name)
    add_slot(abc, ci, mn_txt, t_string)
    mn_repair = p.operand(O + 'updateIcons', 'getproperty', 'mcIconRepair')
    mn_visible = p.operand(O + 'updateIcons', 'setproperty', 'visible')
    mn_html = p.operand(O + 'SetQuantity', 'setproperty', 'htmlText')
    mn_x = p.operand(O + 'realignIcons', 'setproperty', 'x')
    mn_y = p.operand(O + 'realignIcons', 'setproperty', 'y')
    like = p.body(O + 'SetQuantity')
    mn_rect = p.operand(O + 'realignIcons', 'getproperty', '_targetRect')
    mn_tw = p.renamed_mn(p.operand(O + 'realignIcons', 'getproperty', 'textHeight'), 'textWidth')

    upd = [
        I('getlocal0'), I('pushscope'),
        I('getlocal0'), I('getproperty', mn_tf), I('iffalse', 'END'),
        I('getlocal0'), I('getproperty', mn_ic), I('iffalse', 'END'),
        I('getlocal0'), I('getproperty', mn_txt), I('convert_b'), I('setlocal1'),
        I('getlocal0'), I('getproperty', mn_tf), I('getlocal1'), I('setproperty', mn_visible),
        I('pushfalse'), I('setlocal2'),
        I('getlocal1'), I('iffalse', 'L2'),
        I('pushtrue'), I('setlocal2'),
        I('getlocal0'), I('getproperty', mn_repair), I('iffalse', 'L2'),
        I('getlocal0'), I('getproperty', mn_repair), I('getproperty', mn_visible), I('iffalse', 'L2'),
        I('pushfalse'), I('setlocal2'),
        L('L2', 'getlocal0'), I('getproperty', mn_ic), I('getlocal2'), I('setproperty', mn_visible),
        I('getlocal1'), I('iffalse', 'END'),
        I('getlocal0'), I('getproperty', mn_repair), I('iffalse', 'POS2'),
        # equipped (paperdoll) slots never get a _targetRect: put the row above the item, centred on the slot
        # (slot centre = mcIconRepair.x - 14 in both paperdoll overlays; row = text + gap + 15 px hammer)
        I('getlocal0'), I('getproperty', mn_rect), I('iftrue', 'GRID'),
        I('getlocal0'), I('getproperty', mn_ic), I('getlocal0'), I('getproperty', mn_repair), I('getproperty', mn_x),
        I('pushbyte', 9), I('subtract'), I('getlocal0'), I('getproperty', mn_tf), I('getproperty', mn_tw),
        I('pushbyte', 2), I('divide'), I('add'), I('setproperty', mn_x),
        I('getlocal0'), I('getproperty', mn_ic), I('getlocal0'), I('getproperty', mn_repair), I('getproperty', mn_y),
        I('pushbyte', PD_UP_PX), I('subtract'), I('setproperty', mn_y),
        I('jump', 'POS2'),
        L('GRID', 'getlocal0'), I('getproperty', mn_ic), I('getlocal0'), I('getproperty', mn_repair), I('getproperty', mn_x),
        I('setproperty', mn_x),
        I('getlocal0'), I('getproperty', mn_ic), I('getlocal0'), I('getproperty', mn_repair), I('getproperty', mn_y),
        I('setproperty', mn_y),
        L('POS2', 'getlocal0'), I('getproperty', mn_tf), I('getlocal0'), I('getproperty', mn_ic), I('getproperty', mn_x),
        I('pushbyte', DUR_GAP_PX + DUR_TEXT_W_PX), I('subtract'), I('setproperty', mn_x),
        I('getlocal0'), I('getproperty', mn_tf), I('getlocal0'), I('getproperty', mn_ic), I('getproperty', mn_y),
        I('pushbyte', 10), I('subtract'), I('setproperty', mn_y),
        L('END', 'returnvoid'),
    ]
    add_method(p, ci, mn_upd, [], t_void, upd, 3, 5, like)

    setm = [
        I('getlocal0'), I('pushscope'),
        I('getlocal0'), I('getlocal1'), I('setproperty', mn_txt),
        I('getlocal1'), I('iffalse', 'SKIP'),
        I('getlocal0'), I('getproperty', mn_tf), I('iffalse', 'SKIP'),
        I('getlocal0'), I('getproperty', mn_tf), I('getlocal1'), I('setproperty', mn_html),
        L('SKIP', 'getlocal0'), I('callpropvoid', mn_upd, 0),
        I('returnvoid'),
    ]
    add_method(p, ci, mn_set, [t_string], t_void, setm, 2, 3, like)

    # realignIcons (also the tail of updateIcons): re-run the layout before every return
    own = O + 'realignIcons'
    out = []
    n = 0
    for pos, op, args in p.ins(own):
        if abcrt.OPS[op][0] == 'returnvoid':
            out += [[pos, abcrt.NAME2OP['getlocal0'], []], ['ra%d' % n, abcrt.NAME2OP['callpropvoid'], [mn_upd, 0]]]
            out.append(['ra%d_r' % n, op, args])
            n += 1
        else:
            out.append([pos, op, args])
    assert n >= 1
    setcode(p, own, out, max_stack=max(p.body(own)['max_stack'], 2))

    # configUI (may run after the first updateData): re-apply the current state, never clear it
    own = O + 'configUI'
    out = []
    done = False
    ins = p.ins(own)
    for idx, (pos, op, args) in enumerate(ins):
        out.append([pos, op, args])
        if not done and abcrt.OPS[op][0] == 'pushscope':
            out += [['cu0', abcrt.NAME2OP['getlocal0'], []], ['cu2', abcrt.NAME2OP['callpropvoid'], [mn_upd, 0]]]
            done = True
    assert done
    setcode(p, own, out, max_stack=max(p.body(own)['max_stack'], 2))

    # SlotBase.updateData: mcSlotOverlays.gcSetDurability(_data.gcDurText) right before updateIcons()
    own = 'SlotBase::updateData'
    ins = p.ins(own)
    mn_ovl = p.operand(own, 'getproperty', 'mcSlotOverlays')
    mn_data = p.operand(own, 'getproperty', '_data')
    mn_dtxt = p.renamed_mn(p.operand(own, 'getproperty', 'charges'), 'gcDurText')
    k = next(i for i, (pos, op, args) in enumerate(ins)
             if abcrt.OPS[op][0] == 'callpropvoid' and abc.mn(args[0]) == 'updateIcons')
    assert abcrt.OPS[ins[k - 1][1]][0] == 'getproperty' and abcrt.OPS[ins[k - 2][1]][0] == 'getlocal0'
    first = ins[k - 2][0]
    blk = [[first, abcrt.NAME2OP['getlocal0'], []], I('getproperty', mn_ovl), I('getlocal0'), I('getproperty', mn_data),
           I('getproperty', mn_dtxt), I('callpropvoid', mn_set, 1)]
    for j, it in enumerate(blk[1:]):
        it[0] = 'ud%d' % j
    out = []
    for i, (pos, op, args) in enumerate(ins):
        if i == k - 2:
            out += blk
            out.append(['ud_orig', op, args])
        else:
            out.append([pos, op, args])
    setcode(p, own, out, max_stack=max(p.body(own)['max_stack'], 3))


# ------------------------------------------------------------------------------------------- SWF helpers
class BitW:
    def __init__(s):
        s.bits = []

    def u(s, v, n):
        s.bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]

    def sb(s, v, n):
        s.u(v & ((1 << n) - 1), n)

    def bytes(s):
        b = s.bits + [0] * (-len(s.bits) % 8)
        return bytes(int(''.join(map(str, b[i:i + 8])), 2) for i in range(0, len(b), 8))


def nbits_s(*vals):
    n = 1
    for v in vals:
        while not (-(1 << (n - 1)) <= v < (1 << (n - 1))):
            n += 1
    return n


def enc_rect(x0, x1, y0, y1):
    n = nbits_s(x0, x1, y0, y1)
    w = BitW()
    w.u(n, 5)
    for v in (x0, x1, y0, y1):
        w.sb(v, n)
    return w.bytes()


def enc_matrix(sx, sy, tx, ty):
    """scale (float) + translate (twips) only"""
    w = BitW()
    if sx != 1.0 or sy != 1.0:
        a, d = int(round(sx * 65536)), int(round(sy * 65536))
        n = nbits_s(a, d)
        w.u(1, 1); w.u(n, 5); w.sb(a, n); w.sb(d, n)
    else:
        w.u(0, 1)
    w.u(0, 1)                                                     # no rotate
    n = nbits_s(tx, ty) if (tx or ty) else 0
    w.u(n, 5)
    if n:
        w.sb(tx, n); w.sb(ty, n)
    return w.bytes()


def matrix_end(b, p):
    bt = abcrt_bits(b, p)
    if bt.u(1):
        n = bt.u(5); bt.u(n); bt.u(n)
    if bt.u(1):
        n = bt.u(5); bt.u(n); bt.u(n)
    n = bt.u(5); bt.u(n); bt.u(n)
    return (bt.p + 7) >> 3


class abcrt_bits:
    def __init__(s, b, p):
        s.b = b; s.p = p * 8

    def u(s, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | ((s.b[s.p >> 3] >> (7 - (s.p & 7))) & 1); s.p += 1
        return v


def read_matrix(b, p):
    bt = abcrt_bits(b, p)

    def sbits(n):
        v = bt.u(n)
        return v - (1 << n) if n and v & (1 << (n - 1)) else v
    sx = sy = 1.0
    if bt.u(1):
        n = bt.u(5); sx = sbits(n) / 65536.0; sy = sbits(n) / 65536.0
    rot = False
    if bt.u(1):
        n = bt.u(5); sbits(n); sbits(n); rot = True
    n = bt.u(5); tx = sbits(n); ty = sbits(n)
    return sx, sy, tx, ty, rot


def tags(data, start, end):
    out = []
    p = start
    while p < end:
        cl = struct.unpack_from('<H', data, p)[0]
        code, ln = cl >> 6, cl & 0x3f
        hl = 2
        if ln == 0x3f:
            ln = struct.unpack_from('<I', data, p + 2)[0]; hl = 6
        out.append([code, data[p + hl:p + hl + ln]])
        p += hl + ln
        if code == 0:
            break
    return out


def tag_bytes(code, body):
    if len(body) < 0x3f and code not in (39, 37, 26):
        return struct.pack('<H', (code << 6) | len(body)) + body
    return struct.pack('<HI', (code << 6) | 0x3f, len(body)) + body


def po2(depth, char=None, name=None, matrix=None, move=False):
    flags = (0x01 if move else 0) | (0x02 if char is not None else 0) | (0x04 if matrix else 0) | (0x20 if name else 0)
    b = bytes([flags]) + struct.pack('<H', depth)
    if char is not None:
        b += struct.pack('<H', char)
    if matrix:
        b += matrix
    if name:
        b += name.encode('latin1') + b'\0'
    return b


def set_po_matrix(body, code, fn):
    """PlaceObject2/3 body -> body with its matrix replaced by fn(sx, sy, tx, ty)"""
    flags = body[0]
    p = 1
    flags2 = 0
    if code == 70:
        flags2 = body[1]; p = 2
    p += 2                                                        # depth
    if code == 70 and (flags2 & 0x08 or (flags2 & 0x10 and flags & 0x02)):
        p = body.index(b'\0', p) + 1                              # class name
    if flags & 0x02:
        p += 2
    assert flags & 0x04, 'no matrix'
    sx, sy, tx, ty, rot = read_matrix(body, p)
    assert not rot
    e = matrix_end(body, p)
    return body[:p] + enc_matrix(*fn(sx, sy, tx, ty)) + body[e:]


def patch_swf(fws, feats=ALL):
    nb = fws[8] >> 3
    start = 8 + (5 + 4 * nb + 7) // 8 + 4
    top = tags(fws, start, len(fws))
    hit_sprite = hit_text = False
    qty_body = None
    for t in top:
        code, body = t
        if feats & F_SORT and code == 39 and struct.unpack_from('<H', body, 0)[0] == POPUP_SPRITE:
            sub = tags(body, 4, len(body))
            new = []
            last_row = None
            for c2, b2 in sub:
                if c2 in (26, 70):
                    depth = struct.unpack_from('<H', b2, 2 if c2 == 70 else 1)[0]
                    raw = b''.join(tag_bytes(c2, b2) for _ in [0])
                    nm = b'mcItemRenderer5\0' in raw
                    if b'mcBackground\0' in raw and b'HitTest' not in raw:
                        # bg char #319 spans y -5.5..398.2 px (403.7 px); grow by one row, keep top
                        b2 = set_po_matrix(b2, c2, lambda sx, sy, tx, ty: (sx, sy + ROW / 403.7, tx, ty))
                    elif b'mcCloseButton\0' in raw:
                        b2 = set_po_matrix(b2, c2, lambda sx, sy, tx, ty: (sx, sy, tx, ty + int(ROW * 20)))
                    elif depth == 7:
                        # frame shape #321 spans y 228.2..574.1 px at identity; stretch about its top edge
                        top_px, h_px = 228.2, 345.9
                        k = (h_px + ROW) / h_px
                        b2 = set_po_matrix(b2, c2, lambda sx, sy, tx, ty: (sx, k, tx, int(round(-top_px * 20 * (k - 1)))))
                    elif nm:
                        sx, sy, tx, ty, _ = read_matrix(b2, 1 + 2 + 2)
                        last_row = (tx, ty, depth)
                    new.append([c2, b2])
                    if nm:
                        tx, ty, d = last_row
                        new.append([26, po2(d + 2, RENDERER_CHAR, 'mcItemRenderer6',
                                            enc_matrix(1.0, 1.0, tx, ty + int(ROW * 20)))])
                else:
                    new.append([c2, b2])
            assert last_row, 'mcItemRenderer5 not found'
            t[1] = body[:4] + b''.join(tag_bytes(c, b) for c, b in new)
            hit_sprite = True
        elif code == 37 and struct.unpack_from('<H', body, 0)[0] == QTY_EDITTEXT:
            qty_body = body
            hit_text = True
            if not feats & F_WIDE:
                continue
            bt = abcrt_bits(body, 2)
            n = bt.u(5)

            def sb():
                v = bt.u(n)
                return v - (1 << n) if v & (1 << (n - 1)) else v
            x0, x1, y0, y1 = sb(), sb(), sb(), sb()
            e = (bt.p + 7) >> 3
            t[1] = body[:2] + enc_rect(int(QTY_NEW_XMIN_PX * 20), x1, y0, y1) + body[e:]
            hit_text = True
    assert hit_text, 'corner text field not found'
    assert hit_sprite or not feats & F_SORT, 'sort popup not found'
    if feats & F_DUR:
        top = add_durability_display(top, qty_body)
    out = bytearray(fws[:start] + b''.join(tag_bytes(c, b) for c, b in top))
    struct.pack_into('<I', out, 4, len(out))
    return bytes(out)


DUR_TEXT_ID = 30000
DUR_FONT_PX = 15
DUR_TEXT_W_PX = 60
DUR_GAP_PX = 9
REPAIR_ICON_CHAR = 51
OVERLAY_SPRITES = (81, 214, 275)          # grid slot, small paperdoll slot, big paperdoll slot
DUR_ICON_DEPTH, DUR_TEXT_DEPTH = 30, 31
BAR_STEEL_ID, BAR_SILVER_ID = 30001, 30002
BAR_STEEL_SPR, BAR_SILVER_SPR = 30003, 30004
BAR_STEEL_DEPTH, BAR_SILVER_DEPTH = 32, 33
BAR_STEEL_RGB, BAR_SILVER_RGB = (0x4A, 0x8C, 0xE0), (0xE8, 0xE8, 0xE8)
BAR_W_PX, BAR_H_PX = 4, 100


def define_rect_shape(sid, w_tw, h_tw, rgb):
    """DefineShape (code 2): one solid-filled rectangle from (0,0) to (w,h)"""
    w = BitW()
    w.u(1, 4); w.u(0, 4)                                          # NumFillBits=1, NumLineBits=0
    n = nbits_s(w_tw, h_tw)
    # style change: StateFillStyle1 + MoveTo(0,0)
    w.u(0, 1); w.u(0, 1); w.u(0, 1); w.u(1, 1); w.u(0, 1); w.u(1, 1)
    w.u(1, 5); w.sb(0, 1); w.sb(0, 1)
    w.u(1, 1)                                                     # FillStyle1 = 1
    for dx, dy in ((w_tw, 0), (0, h_tw), (-w_tw, 0), (0, -h_tw)):
        w.u(1, 1); w.u(1, 1); w.u(n - 2, 4); w.u(0, 1)            # edge, straight, NumBits-2, not general
        if dx:
            w.u(0, 1); w.sb(dx, n)
        else:
            w.u(1, 1); w.sb(dy, n)
    w.u(0, 6)                                                     # end of shape
    styles = bytes([1, 0x00]) + bytes(rgb) + bytes([0])           # 1 solid fill, 0 line styles
    return struct.pack('<H', sid) + enc_rect(0, w_tw, 0, h_tw) + styles + w.bytes()


def edittext_clone(body, new_id, bounds_twips, font_px):
    """copy a DefineEditText with a new id, bounds and font height; initial text emptied"""
    bt = abcrt_bits(body, 2)
    n = bt.u(5)
    bt.u(4 * n)
    p = (bt.p + 7) >> 3
    f1, f2 = body[p], body[p + 1]
    q = p + 2
    if f1 & 0x01:
        q += 2                                                    # font id
    if f2 & 0x80:
        q = body.index(b'\0', q) + 1                              # font class
    assert f1 & 0x01 or f2 & 0x80
    hpos = q
    q += 2                                                        # font height
    if f1 & 0x04:
        q += 4                                                    # colour
    if f1 & 0x02:
        q += 2                                                    # max length
    if f2 & 0x20:
        q += 9                                                    # layout
    q = body.index(b'\0', q) + 1                                  # variable name
    assert f1 & 0x80                                              # HasText
    mid = body[p:hpos] + struct.pack('<H', font_px * 20) + body[hpos + 2:q]
    return struct.pack('<H', new_id) + enc_rect(*bounds_twips) + mid + b'\0'


def enc_cxa(mult, add):
    vals = list(mult) + list(add)
    n = nbits_s(*vals)
    w = BitW()
    w.u(1, 1); w.u(1, 1); w.u(n, 4)
    for v in vals:
        w.sb(v, n)
    return w.bytes()


def clone_po3(body, depth, char, old_name, new_name):
    flags1, flags2 = body[0], body[1]
    assert not flags2 & 0x18 and flags1 & 0x02 and flags1 & 0x20
    b = bytearray(body)
    struct.pack_into('<HH', b, 2, depth, char)
    i = bytes(b).index(old_name.encode() + b'\0')
    return bytes(b[:i]) + new_name.encode() + b'\0' + bytes(b[i + len(old_name) + 1:])


def add_durability_display(top, qty_body):
    used = set()
    for code, body in top:
        if code in (2, 22, 32, 83, 39, 37, 10, 48, 75, 11, 33, 6, 21, 35, 36, 20, 7, 34, 14, 46, 84, 1008, 1009):
            used.add(struct.unpack_from('<H', body, 0)[0])
    assert not {DUR_TEXT_ID, BAR_STEEL_ID, BAR_SILVER_ID, BAR_STEEL_SPR, BAR_SILVER_SPR} & used
    # text field: right edge at x=0 (text grows to the left of the hammer), ~22 px tall
    new_text = edittext_clone(qty_body, DUR_TEXT_ID, (0, DUR_TEXT_W_PX * 20, -2 * 20, 19 * 20), DUR_FONT_PX)
    out = []
    inserted = False
    hits = 0
    for code, body in top:
        sid = struct.unpack_from('<H', body, 0)[0] if code == 39 else None
        if code == 39 and sid in OVERLAY_SPRITES:
            if not inserted:
                out.append([37, new_text])
                inserted = True
            sub = tags(body, 4, len(body))
            qty_po = next(b2 for c2, b2 in sub if c2 == 70 and b'tfQuantity\0' in b2)
            depths = set()
            for c2, b2 in sub:
                if c2 in (26, 70):
                    depths.add(struct.unpack_from('<H', b2, 2 if c2 == 70 else 1)[0])
            assert not {DUR_ICON_DEPTH, DUR_TEXT_DEPTH, BAR_STEEL_DEPTH, BAR_SILVER_DEPTH} & depths
            icon = bytes([0x02 | 0x04 | 0x08 | 0x20]) + struct.pack('<HH', DUR_ICON_DEPTH, REPAIR_ICON_CHAR) + \
                enc_matrix(1.0, 1.0, 0, 0) + enc_cxa((0, 0, 0, 256), (204, 204, 204, 0)) + b'gcIconDurability\0'
            text = clone_po3(qty_po, DUR_TEXT_DEPTH, DUR_TEXT_ID, 'tfQuantity', 'gcTfDurability')
            new = []
            done = False
            for c2, b2 in sub:
                if c2 == 1 and not done:                          # before the first ShowFrame
                    new += [[26, icon], [70, text]]
                    done = True
                new.append([c2, b2])
            assert done
            out.append([39, body[:4] + b''.join(tag_bytes(c, b) for c, b in new)])
            hits += 1
        else:
            out.append([code, body])
    assert hits == len(OVERLAY_SPRITES), hits
    return out


class PatchError(Exception):
    pass


def inflate_swf(cfx):
    """CFX/CWS (zlib) or GFX/FWS (uncompressed) SWF bytes -> uncompressed FWS bytes.
    Some mod tools write the zlib stream without its end marker (checksum); the data itself is complete, so accept a
    stream that stops early as long as it gave the full length the SWF header declares."""
    if cfx[:3] in (b'FWS', b'GFX'):
        return b'FWS' + cfx[3:]
    declared = struct.unpack_from('<I', cfx, 4)[0]
    d = zlib.decompressobj()
    try:
        body = d.decompress(cfx[8:])
    except zlib.error:
        raise PatchError("its inventory screen file is damaged and can't be unpacked")
    if not d.eof and len(body) + 8 < declared:
        raise PatchError('its inventory screen file is cut short (%d of %d bytes)' % (len(body) + 8, declared))
    return b'FWS' + cfx[3:8] + body


# CR2W tables cr2w.rewrap copies unchanged (name in cr2w.checks -> table index, entry size); it rewrites only the chunk
# table, chunk 0, the header CRC and the sizes, and those must always pass.
COPIED_TABLES = {'strings': (0, 1), 'names': (1, 8), 'imports': (2, 8), 'props': (3, 16), 'buffers': (5, 24),
                 'embedded': (6, 24)}


def kept_crc_rows(orig, out):
    """cr2w.checks rows that fail in out only because they already failed in orig: the CRC of a table rewrap copies,
    with the same bytes and the same stored CRC in both files. Some mod tools store a non-standard CRC there (UI
    Remastered Edition: crc32 of the first 8 of the 16 bytes of its properties table); the game loads those files, and
    keeping the other author's bytes as they are is not an error of this patch."""
    wi, wo = cr2w.CR2W(orig), cr2w.CR2W(out)
    before = {r[0]: r for r in cr2w.checks(wi)}
    kept = []
    for nm, (i, es) in COPIED_TABLES.items():
        r = before.get('table %s crc32' % nm)
        if r and r[1] != r[2] and wi.tables[i] == wo.tables[i] and wi.table_bytes(i, es) == wo.table_bytes(i, es):
            kept.append(r[0])
    return kept


def patch_redswf(orig, feats=ALL):
    """panel_inventory.redswf bytes (any version derived from the game's) -> Gear Compare-patched bytes with the
    features in feats. Raises PatchError when a part one of them edits is missing or different."""
    try:
        w = cr2w.CR2W(orig)
        pe, lo, bo, bl, tail = w.swf_layout()
        cfx = w.chunk_data(0)[bo:bo + bl]
        fws = inflate_swf(cfx)
        abcs = abcrt.swf_abcs(fws)
        if len(abcs) != 1:
            raise PatchError('expected 1 code block, found %d' % len(abcs))
        abc = abcrt.ABC(abcs[0][1])
        if any(abc.mn(t.name) == 'gcUiVersion' for i in abc.instances for t in i['traits']):
            raise PatchError('this inventory file already contains Gear Compare')
        abc = patch_abc(abc, feats)
        new_abc = abc.serialize()
        abcrt.ABC(new_abc)
        fws3 = patch_swf(abcrt.replace_abc(fws, new_abc), feats)
        out, _ = cr2w.rewrap(orig, fws3)
    except PatchError:
        raise
    except (AssertionError, StopIteration, SystemExit, ValueError, KeyError, IndexError):
        raise PatchError('the part of the inventory screen it changes is different in this file')
    kept = kept_crc_rows(orig, out)
    bad = [r for r in cr2w.checks(cr2w.CR2W(out)) if r[1] != r[2] and r[0] not in kept]
    if bad:
        raise PatchError('container check failed: %s' % bad[0][0])
    return out


def patch_best(orig, log=print):
    """Add Gear Compare's features one at a time to a fresh copy of orig; keep each one that applies cleanly.
    Returns (patched bytes, feature bits, [(feature name, reason skipped)])."""
    feats, skipped = 0, []
    base = patch_redswf(orig, 0)                   # the base file itself must be readable and patchable
    for row in kept_crc_rows(orig, base):
        log('  note: this file stores a non-standard checksum for its %s table; kept as it is (the game accepts it)'
            % row.split()[1])
    for f in (F_SORT, F_ALWAYS, F_DUR, F_WIDE, F_POS):
        try:
            patch_redswf(orig, feats | f)
            feats |= f
            log('  + %s' % FEATURE_NAMES[f])
        except PatchError as e:
            skipped.append((FEATURE_NAMES[f], str(e)))
            log('  - %s: skipped (%s)' % (FEATURE_NAMES[f], e))
    return patch_redswf(orig, feats), feats, skipped


def main():
    src, dst = sys.argv[1], sys.argv[2]
    out = patch_redswf(open(src, 'rb').read())
    open(dst, 'wb').write(out)
    print('wrote', dst, len(out))
