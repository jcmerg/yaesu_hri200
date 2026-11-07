#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Turn the PyInstaller output into one file per platform.

    python3 packaging/make_dist.py --dist dist --out out

What comes out depends on where it runs, because the three platforms
ship software differently:

    Windows   HRI-200-bridge-<ver>-windows-x86_64.zip
                hri200-gui.exe          double-click, no install
                hri200-bridge.exe       the console bridge
                README.txt

    Linux     HRI-200-bridge-<ver>-linux-x86_64.tar.gz
                hri200-gui, hri200-bridge
                hri200.desktop, hri200.png   for the menu
                install.sh                   copies them into ~/.local

    macOS     HRI-200-bridge-<ver>-macos-<arch>.zip
                HRI-200 bridge.app      drag into /Applications
                hri200-bridge           the console bridge

The archive is the deliverable; nothing else has to be installed on the
target machine.
"""

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile

VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ICONS = os.path.join(HERE, "icons")

README_TXT = """HRI-200 bridge %s
================

Zwei Programme liegen in diesem Ordner:

  hri200-gui      Bedienoberflaeche: Funkgeraet waehlen, verbinden,
                  Frequenz setzen, PTT, Pegelanzeige. Optional laesst
                  sich damit zusaetzlich ein TS-2000-CAT-Server
                  starten, den flrig oder fldigi ansprechen.

  hri200-bridge   Dieselbe Bruecke ohne Fenster, fuer den Dauerbetrieb.

Beide brauchen nur den seriellen Port des Funkgeraets. Audio laeuft
ueber die Soundkarte, die am Funkgeraet haengt.

Beispiel:
  hri200-bridge COM11 --radio yaesu --hri COM6 --freq 145.5

Hilfe:
  hri200-bridge --help

Ist Windows unsicher, ob das Programm starten darf: Rechtsklick,
Eigenschaften, "Zulassen" setzen. Das Programm ist nicht signiert.
""" % VERSION


def arch_tag():
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine or "unknown"


def find(dist, name):
    for candidate in (os.path.join(dist, name),
                      os.path.join(dist, name + ".exe")):
        if os.path.exists(candidate):
            return candidate
    raise SystemExit("%s not found in %s - run PyInstaller first"
                     % (name, dist))


def zip_dir(path, archive, extra=None):
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in sorted(os.listdir(path)):
            full = os.path.join(path, entry)
            if os.path.isdir(full):
                for base, _dirs, files in os.walk(full):
                    for name in sorted(files):
                        src = os.path.join(base, name)
                        arc = os.path.join(entry,
                                           os.path.relpath(src, full))
                        zf.write(src, arc)
            else:
                zf.write(full, entry)
        for src, arc in (extra or []):
            zf.write(src, arc)


def build_windows(dist, out, stage):
    if not os.path.isdir(stage):
        os.makedirs(stage)
    for name in ("hri200-gui", "hri200-bridge"):
        src = find(dist, name)
        shutil.copy2(src, os.path.join(stage, os.path.basename(src)))
    with open(os.path.join(stage, "README.txt"), "w") as fh:
        fh.write(README_TXT)
    archive = os.path.join(out, "HRI-200-bridge-%s-windows-%s.zip"
                           % (VERSION, arch_tag()))
    zip_dir(stage, archive)
    return archive


def build_linux(dist, out, stage):
    if not os.path.isdir(stage):
        os.makedirs(stage)
    for name in ("hri200-gui", "hri200-bridge"):
        src = find(dist, name)
        dst = os.path.join(stage, name)
        shutil.copy2(src, dst)
        os.chmod(dst, os.stat(dst).st_mode | stat.S_IXUSR
                 | stat.S_IXGRP | stat.S_IXOTH)
    shutil.copy2(os.path.join(HERE, "hri200.desktop"),
                 os.path.join(stage, "hri200.desktop"))
    icon = os.path.join(ICONS, "hri200-256.png")
    if os.path.exists(icon):
        shutil.copy2(icon, os.path.join(stage, "hri200.png"))
    with open(os.path.join(stage, "README.txt"), "w") as fh:
        fh.write(README_TXT)
    with open(os.path.join(stage, "install.sh"), "w") as fh:
        fh.write(LINUX_INSTALL % {"version": VERSION})
    os.chmod(os.path.join(stage, "install.sh"), 0o755)

    archive = os.path.join(out, "HRI-200-bridge-%s-linux-%s.tar.gz"
                           % (VERSION, arch_tag()))
    with tarfile.open(archive, "w:gz") as tf:
        for entry in sorted(os.listdir(stage)):
            tf.add(os.path.join(stage, entry),
                   arcname=os.path.join("HRI-200-bridge-%s" % VERSION, entry))
    return archive


def build_macos(dist, out, stage):
    if not os.path.isdir(stage):
        os.makedirs(stage)
    app = os.path.join(dist, "HRI-200 bridge.app")
    app_copy = os.path.join(stage, "HRI-200 bridge.app")
    if os.path.isdir(app):
        # symlinks=True matters: the framework inside the bundle is a
        # tree of links, and a copy that resolves them is a different
        # bundle whose code signature no longer matches. macOS calls
        # that "damaged" and refuses to start it.
        shutil.copytree(app, app_copy, symlinks=True)
    shutil.copy2(find(dist, "hri200-bridge"),
                 os.path.join(stage, "hri200-bridge"))
    with open(os.path.join(stage, "README.txt"), "w") as fh:
        fh.write(README_TXT)
    archive = os.path.join(out, "HRI-200-bridge-%s-macos-%s.zip"
                           % (VERSION, arch_tag()))
    # ditto, not zipfile: Python's zipfile cannot store a symlink, so it
    # writes the target's contents instead and the archive is not the
    # bundle it claims to be. ditto is Apple's own tool for this.
    #
    # --norsrc --noextattr keep extended attributes out of the archive.
    # Stored as they are, they reappear on extraction as ._name files
    # inside the framework, and a stray file inside a signed bundle
    # makes macOS report the app as damaged.
    archive = os.path.abspath(archive)
    if os.path.isdir(app):
        subprocess.check_call([
            "ditto", "-c", "-k", "--norsrc", "--noextattr", "--keepParent",
            app_copy, archive])
        # The other two files ride along in the same zip.
        with zipfile.ZipFile(archive, "a", zipfile.ZIP_DEFLATED) as zf:
            for name in ("hri200-bridge", "README.txt"):
                zf.write(os.path.join(stage, name), name)
    else:
        zip_dir(stage, archive)
    return archive


LINUX_INSTALL = """#!/bin/sh
# Install the HRI-200 bridge for the current user.
set -e
here=$(cd "$(dirname "$0")" && pwd)
bindir="$HOME/.local/bin"
appdir="$HOME/.local/share/applications"
icondir="$HOME/.local/share/icons/hicolor/256x256/apps"

mkdir -p "$bindir" "$appdir" "$icondir"
install -m 755 "$here/hri200-gui" "$bindir/hri200-gui"
install -m 755 "$here/hri200-bridge" "$bindir/hri200-bridge"
if [ -f "$here/hri200.png" ]; then
    install -m 644 "$here/hri200.png" "$icondir/hri200.png"
fi
install -m 644 "$here/hri200.desktop" "$appdir/hri200.desktop"

echo "installed to $bindir"
echo "start it from the menu, or run: hri200-gui"
case ":$PATH:" in
  *":$bindir:"*) ;;
  *) echo "note: $bindir is not on your PATH" ;;
esac
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--out", default=os.path.join(ROOT, "out"))
    ap.add_argument("--stage", default=None,
                    help="directory the platform layout is built in")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.out):
        os.makedirs(args.out)
    stage = args.stage or os.path.join(args.out, "_stage")
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    os.makedirs(stage)

    if sys.platform.startswith("win"):
        archive = build_windows(args.dist, args.out, stage)
    elif sys.platform == "darwin":
        archive = build_macos(args.dist, args.out, stage)
    else:
        archive = build_linux(args.dist, args.out, stage)

    size = os.path.getsize(archive)
    print("%s  (%.1f MB)" % (archive, size / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
