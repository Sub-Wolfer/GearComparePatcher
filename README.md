# Gear Compare Patcher

Source code of **Gear Compare Patcher**, the optional tool for the Witcher 3 mod
[Gear Compare](https://www.nexusmods.com/witcher3/mods/14585).

Gear Compare and other inventory mods (Recent Loot Tab, Smooth UI Remastered, ...)
all replace the same file, `panel_inventory.redswf`, and the game loads only one of
them. The patcher takes the inventory screen of your other mod, adds Gear Compare's
changes to it and saves the result in Gear Compare's own folder
(`Mods\mod0_GearCompare\content`), so both mods' features load together.

What it does, and does not do:

- Reads the game's and your mods' bundle files, and writes only inside Gear
  Compare's mod folder. A backup of the previous files goes to
  `Mods\mod0_GearCompare\patcher_backup\<date-time>`.
- Never changes the other mod's files and never redistributes them.
- No network access, no installer, no registry writes. It only *reads* the
  Steam/GOG registry keys to find the game folder.
- Python standard library only; no third-party packages.

## Files

| File | Purpose |
| --- | --- |
| `src/gc_patcher.py` | Command-line front end: finds the game, lists inventory mods, writes the result |
| `src/gc_patch.py` | Gear Compare's changes to the inventory screen (Flash bytecode and SWF tags) |
| `src/abcrt.py` | ActionScript 3 (ABC) bytecode reader/writer |
| `src/cr2w.py` | CR2W wrapper of `.redswf` files |
| `src/w3bundle.py` | Witcher 3 `.bundle` reader/writer |
| `src/w3meta.py` | `metadata.store` writer |
| `src/lz4block.py` | LZ4 block decompression |

## Run from source

```
python src\gc_patcher.py
python src\gc_patcher.py --game "D:\Games\The Witcher 3" --source auto --yes
```

`--source` takes `auto` (the mod the game would load), `vanilla` (undo), a mod
folder name, or its number in the list. `--dry-run` shows what would happen.

## Build the exe

Windows, Python 3.10 (the release was built with Python 3.10.6 and
PyInstaller 6.22.3):

```
build.bat
```

which runs

```
python -m pip install pyinstaller==6.22.3
python -m PyInstaller --onefile --console --name GearComparePatcher --exclude-module numpy --exclude-module PIL --exclude-module tkinter src\gc_patcher.py
```

The exe is written to `dist\GearComparePatcher.exe`. PyInstaller builds are not
byte-for-byte reproducible, so a rebuilt exe will have a different hash.

Released exes (on Nexus Mods and GitHub Releases), SHA-256:

- 1.1.0: `6f7287f995f315454da3c2551187679555a31aab8786005ed87756e686e38115`
- 1.0.0: `61a829d08414b54f45947f262488e4c961600ec34ca135995ed4191c80c43d69`

## License

MIT, see `LICENSE`.
