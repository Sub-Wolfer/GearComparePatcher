"""Gear Compare Patcher - makes Gear Compare work together with another mod that changes the inventory screen.

It takes the inventory screen file (panel_inventory.redswf) the game would load from your OTHER mods, applies
Gear Compare's changes on top of it and saves the result in Gear Compare's folder. Both mods' inventory
features then load from that one file. Nothing of the other mod is changed or redistributed: the patching
happens on your PC, with your copy of the other mod.

Usage (double-click, or from a command prompt):
  GearComparePatcher.exe                        interactive
  GearComparePatcher.exe --game "D:\\Games\\The Witcher 3" --source auto|vanilla|<mod folder> --yes
"""
import argparse
import ctypes
import datetime
import glob
import json
import os
import re
import shutil
import struct
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gc_patch   # noqa: E402
import w3bundle   # noqa: E402
import w3meta     # noqa: E402

VERSION = '1.0.0'
DEPOT = 'gameplay\\gui_new\\swf\\inventory\\panel_inventory.redswf'
GC_SCRIPT = os.path.join('content', 'scripts', 'local', 'gearCompare.ws')
MODIO_GAME_ID = '8254'


# ------------------------------------------------------------------------------------------- small helpers
def say(msg=''):
    print(msg, flush=True)


def ask(prompt, default=''):
    try:
        v = input(prompt).strip()
    except EOFError:
        return default
    return v or default


def documents_dir():
    """The real Documents folder (OneDrive-redirected included)."""
    try:
        class GUID(ctypes.Structure):
            _fields_ = [('a', ctypes.c_uint32), ('b', ctypes.c_uint16), ('c', ctypes.c_uint16), ('d', ctypes.c_ubyte * 8)]
        fid = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(p)) == 0:
            path = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            return path
    except Exception:
        pass
    return os.path.join(os.path.expanduser('~'), 'Documents')


def is_game_dir(d):
    return bool(d) and os.path.isdir(os.path.join(d, 'content', 'content0')) and os.path.isdir(os.path.join(d, 'bin'))


def find_game_dir():
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
    d = here
    for _ in range(5):                                   # the patcher placed somewhere inside the game folder
        if is_game_dir(d):
            return d
        d = os.path.dirname(d)
    cands = []
    try:
        import winreg
        for hive, key, val in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                               (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath')):
            try:
                with winreg.OpenKey(hive, key) as k:
                    steam = winreg.QueryValueEx(k, val)[0]
                libs = [steam]
                vdf = os.path.join(steam, 'steamapps', 'libraryfolders.vdf')
                if os.path.isfile(vdf):
                    libs += [m.replace('\\\\', '\\') for m in re.findall(r'"path"\s+"([^"]+)"', open(vdf, encoding='utf-8', errors='replace').read())]
                cands += [os.path.join(l, 'steamapps', 'common', 'The Witcher 3') for l in libs]
            except OSError:
                pass
        for key in (r'SOFTWARE\WOW6432Node\GOG.com\Games\1495134320', r'SOFTWARE\WOW6432Node\GOG.com\Games\1207664663'):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                    cands.append(winreg.QueryValueEx(k, 'path')[0])
            except OSError:
                pass
    except ImportError:
        pass
    for c in cands:
        if is_game_dir(c):
            return os.path.normpath(c)
    return None


# ------------------------------------------------------------------------------------------- bundles
def bundle_entry(path):
    """The panel_inventory entry of a bundle (TOC only), or None."""
    try:
        b = w3bundle.read_bundle(path, load_payload=False)
    except Exception:
        return None
    for e in b.entries:
        if e.name.lower() == DEPOT:
            return e
    return None


def read_entry(path, e):
    with open(path, 'rb') as f:
        f.seek(e.offset)
        e.payload = f.read(e.zsize)
    return e.data()


def find_in_content(content_dir):
    if not os.path.isdir(content_dir):
        return None
    for b in sorted(glob.glob(os.path.join(content_dir, '*.bundle'))):
        e = bundle_entry(b)
        if e is not None:
            return b, e
    return None


def mods_settings(docs):
    """mods.settings -> {mod folder (lower): (enabled, priority or None)}"""
    out = {}
    p = os.path.join(docs, 'The Witcher 3', 'mods.settings')
    if not os.path.isfile(p):
        return out
    cur = None
    for line in open(p, encoding='utf-8', errors='replace'):
        line = line.strip()
        m = re.match(r'^\[(.+)\]$', line)
        if m:
            cur = m.group(1).lower()
            out[cur] = [True, None]
        elif cur and '=' in line:
            k, v = [x.strip() for x in line.split('=', 1)]
            if k.lower() == 'enabled':
                out[cur][0] = v != '0'
            elif k.lower() == 'priority':
                try:
                    out[cur][1] = int(v)
                except ValueError:
                    pass
    return {k: tuple(v) for k, v in out.items()}


def local_mods(game, docs):
    """Enabled local mods in load-priority order (first one wins a file conflict)."""
    mdir = os.path.join(game, 'Mods')
    if not os.path.isdir(mdir):
        return []
    st = mods_settings(docs)
    names = [n for n in os.listdir(mdir) if os.path.isdir(os.path.join(mdir, n)) and n.lower().startswith('mod')]
    names = [n for n in names if st.get(n.lower(), (True, None))[0]]
    with_pri = sorted([n for n in names if st.get(n.lower(), (True, None))[1] is not None],
                      key=lambda n: (st[n.lower()][1], n.lower()))
    rest = sorted([n for n in names if n not in with_pri], key=str.lower)
    return with_pri + rest


def modio_inventory_mods(docs):
    """Enabled mod.io mods that ship panel_inventory (they always win over the Mods folder)."""
    base = os.path.join(os.environ.get('PUBLIC', r'C:\Users\Public'), 'mod.io', MODIO_GAME_ID, 'mods')
    cfg = os.path.join(docs, 'The Witcher 3', 'modioModsConfig.json')
    enabled = None
    if os.path.isfile(cfg):
        txt = open(cfg, encoding='utf-8', errors='replace').read()
        enabled = {m.group(1) for m in re.finditer(r'"modID"\s*:\s*(\d+)\s*,\s*"isEnabled"\s*:\s*true', txt)}
    hits = []
    for mod_id in (os.listdir(base) if os.path.isdir(base) else []):
        if enabled is not None and mod_id not in enabled:
            continue
        for content in glob.glob(os.path.join(base, mod_id, '*', '*', 'content')):
            if find_in_content(content):
                hits.append('%s (mod.io %s)' % (os.path.basename(os.path.dirname(content)), mod_id))
    return hits


def vanilla_inventory(game):
    for b in sorted(glob.glob(os.path.join(game, 'content', 'content0', 'bundles', '*.bundle'))):
        e = bundle_entry(b)
        if e is not None:
            return b, e
    return None


# ------------------------------------------------------------------------------------------- main work
def find_gc(game):
    mdir = os.path.join(game, 'Mods')
    hits = [n for n in (os.listdir(mdir) if os.path.isdir(mdir) else []) if os.path.isfile(os.path.join(mdir, n, GC_SCRIPT))]
    if not hits:
        return None
    hits.sort(key=lambda n: (n.lower() != 'mod0_gearcompare', n.lower()))
    return os.path.join(mdir, hits[0])


def install(gc_dir, data, source_label, feats, skipped):
    content = os.path.join(gc_dir, 'content')
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = os.path.join(gc_dir, 'patcher_backup', stamp)
    os.makedirs(backup, exist_ok=True)
    for n in ('blob0.bundle', 'metadata.store'):
        p = os.path.join(content, n)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(backup, n))
    bpath = os.path.join(content, 'blob0.bundle')
    tmp = bpath + '.new'
    entry = w3bundle.make_entry(DEPOT, data, w3bundle.COMP_ZLIB)
    with open(tmp, 'wb') as f:
        f.write(w3bundle.build_bundle_bytes([entry], alignment=16, pad_end=False, pad_file_to=0, gap_fill=None))
    os.replace(tmp, bpath)
    store = w3meta.write_store(w3meta.generate([('blob0.bundle', bpath)]))
    with open(os.path.join(content, 'metadata.store'), 'wb') as f:
        f.write(store)
    with open(os.path.join(gc_dir, 'GearComparePatcher.txt'), 'w', encoding='utf-8') as f:
        f.write('Gear Compare Patcher %s - %s\n' % (VERSION, stamp))
        f.write('Inventory screen built from: %s\n' % source_label)
        f.write('Gear Compare features included:\n')
        for bit, name in gc_patch.FEATURE_NAMES.items():
            f.write('  [%s] %s\n' % ('x' if feats & bit else ' ', name))
        for name, why in skipped:
            f.write('Skipped: %s (%s)\n' % (name, why))
        f.write('Previous files: %s\n' % backup)
    return backup


def run(args):
    say('Gear Compare Patcher %s' % VERSION)
    say('=' * 60)
    game = args.game or find_game_dir()
    while not is_game_dir(game):
        if args.yes:
            say('Could not find The Witcher 3. Pass --game "<game folder>".')
            return 2
        game = ask('Could not find The Witcher 3. Paste the game folder path (the one with "bin" and "content"): ').strip('"')
    game = os.path.normpath(game)
    say('Game folder: %s' % game)
    docs = documents_dir()

    gc_dir = find_gc(game)
    if not gc_dir:
        say('\nGear Compare is not installed (no Mods\\...\\%s). Install the main file first.' % GC_SCRIPT)
        return 2
    gc_name = os.path.basename(gc_dir)
    say('Gear Compare: Mods\\%s' % gc_name)

    order = local_mods(game, docs)
    others = []
    for name in order:
        if name.lower() == gc_name.lower():
            continue
        hit = find_in_content(os.path.join(game, 'Mods', name, 'content'))
        if hit:
            others.append((name, hit))
    modio = modio_inventory_mods(docs)

    say('')
    if others:
        say('Other mods that change the inventory screen (load order):')
        for i, (name, _) in enumerate(others, 1):
            say('  %d. %s%s' % (i, name, '   <- the one the game would load' if i == 1 else ''))
    else:
        say('No other mod in the Mods folder changes the inventory screen.')
    if modio:
        say('')
        say('WARNING: these mod.io mods (in-game Mods menu) replace the inventory screen too:')
        for m in modio:
            say('  - ' + m)
        say('mod.io mods always load before the Mods folder, so the patched file would NOT be used.')
        say('Disable them in the in-game Mods menu, or use the "Script only" file of Gear Compare.')
    gi = [n.lower() for n in order].index(gc_name.lower()) if gc_name in order else -1
    if others and gi > [n.lower() for n in order].index(others[0][0].lower()):
        say('')
        say('WARNING: %s loads before %s (mods.settings priority). Give Gear Compare a higher priority' % (others[0][0], gc_name))
        say('(a lower Priority number) or the patched file will not be used.')

    # choose the source
    src = args.source
    if not src:
        if args.yes:
            src = 'auto'
        else:
            say('')
            say('What should the patched inventory screen be built from?')
            if others:
                say('  [Enter] %s (recommended: keeps that mod\'s inventory features)' % others[0][0])
                for i, (name, _) in enumerate(others[1:], 2):
                    say('  %d       %s' % (i, name))
            else:
                say('  [Enter] the game\'s own inventory screen (standard Gear Compare)')
            say('  v       the game\'s own inventory screen (undo: back to standard Gear Compare)')
            say('  q       quit without changes')
            c = ask('> ', '').lower()
            if c == 'q':
                say('No changes made.')
                return 0
            src = 'vanilla' if c == 'v' else ('auto' if c == '' else c)
    if src == 'auto':
        src = others[0][0] if others else 'vanilla'
    if src.isdigit() and 1 <= int(src) <= len(others):
        src = others[int(src) - 1][0]

    if src.lower() == 'vanilla':
        hit = vanilla_inventory(game)
        label = "the game's own inventory screen"
        if not hit:
            say('Could not find the game\'s inventory screen in content\\content0\\bundles.')
            return 2
    else:
        match = [o for o in others if o[0].lower() == src.lower()]
        if not match:
            say('"%s" is not one of the mods listed above.' % src)
            return 2
        hit, label = match[0][1], match[0][0]

    bpath, entry = hit
    say('')
    say('Reading %s ...' % label)
    orig = read_entry(bpath, entry)
    say('Applying Gear Compare:')
    try:
        data, feats, skipped = gc_patch.patch_best(orig, log=say)
    except gc_patch.PatchError as e:
        say('')
        say('This inventory screen cannot be patched: %s' % e)
        say('Nothing was changed. Use the "Script only" file of Gear Compare with that mod instead.')
        return 1
    if args.dry_run:
        say('\nDry run: nothing written.')
        return 0
    backup = install(gc_dir, data, label, feats, skipped)
    say('')
    say('Done. Saved to Mods\\%s\\content (previous files: %s).' % (gc_name, backup))
    if skipped:
        say('Some Gear Compare features did not fit this inventory screen and were left out (see above);')
        say('Gear Compare adapts to that automatically.')
    say('Start the game to use it. Run this patcher again after updating %s or Gear Compare.' % (
        label if src.lower() != 'vanilla' else 'a mod that changes the inventory screen'))
    return 0


def main():
    ap = argparse.ArgumentParser(description='Gear Compare Patcher')
    ap.add_argument('--game', help='The Witcher 3 folder')
    ap.add_argument('--source', help='auto | vanilla | <mod folder name> | <number from the list>')
    ap.add_argument('--yes', action='store_true', help='no questions (auto source unless --source is given)')
    ap.add_argument('--dry-run', action='store_true', help='patch in memory only, write nothing')
    args = ap.parse_args()
    try:
        code = run(args)
    except Exception:
        say('\nUnexpected error - nothing was changed unless "Done" was shown above:')
        say(traceback.format_exc())
        code = 3
    if not args.yes:
        ask('\nPress Enter to close.')
    return code


if __name__ == '__main__':
    sys.exit(main())
