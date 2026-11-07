#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Turn the PyInstaller output into one archive per platform.

    python3 packaging/make_dist.py --dist dist --out out

What comes out depends on where it runs, because the three platforms
ship software differently. Every archive holds a single top-level folder
so unpacking it never scatters files into the current directory:

    Windows   HRI-200-bridge-<ver>-windows-<arch>.zip
                hri200-gui.exe          double-click, no install
                hri200-ysf-gui.exe      the YSF gateway panel
                hri200-bridge.exe       the console bridge
                README.txt

    Linux     HRI-200-bridge-<ver>-linux-<arch>.tar.gz
                hri200-gui, hri200-ysf-gui, hri200-bridge
                hri200.desktop, hri200-ysf.desktop, hri200.png
                install.sh               copies them into ~/.local
                README.txt

    macOS     HRI-200-bridge-<ver>-macos-<arch>.zip
                HRI-200 bridge.app      drag into /Applications
                HRI-200 YSF gateway.app
                hri200-bridge           the console bridge
                README.txt

The archive is the deliverable; nothing else has to be installed on the
target machine.

One detail decides whether the macOS archive works at all: it is built
with ditto, because Python's zipfile cannot store a symlink. The
framework inside an app bundle is a tree of them, and an archive made by
flattening those links unpacks into a bundle whose code signature no
longer matches - which macOS reports as "damaged".
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

VERSION = "2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ICONS = os.path.join(HERE, "icons")

FOLDER = "HRI-200-bridge-%s" % VERSION

CONSOLE = "hri200-bridge"
PANELS = ("hri200-gui", "hri200-ysf-gui")
DESKTOP = ("hri200.desktop", "hri200-ysf.desktop")
MAC_APPS = ("HRI-200 bridge.app", "HRI-200 YSF gateway.app")

README_TXT = """HRI-200 bridge %s
================

Drei Programme liegen in diesem Ordner:

  hri200-gui      Bedienoberflaeche der CAT-Bruecke: HRI-200 oeffnen,
                  Frequenz setzen, PTT, Pegelanzeige, und den virtuellen
                  CAT-Port starten, den flrig oder fldigi ansprechen.
                  Der Port antwortet wahlweise als Kenwood TS-2000, als
                  Yaesu FT-817/857, als Icom (CI-V) oder erkennt selbst,
                  was die Software spricht.

  hri200-ysf-gui  Bedienoberflaeche des YSF-Gateways: Reflector,
                  Rufzeichen, Kanal, DG-ID. Zeigt mit, was ueber den
                  Reflector kommt und was zum Funkgeraet geht.

  hri200-bridge   Die CAT-Bruecke ohne Fenster, fuer den Dauerbetrieb.

Die Bruecke braucht nur den seriellen Port des Funkgeraets. Audio laeuft
ueber die Soundkarte, die am Funkgeraet haengt.

Beispiele:
  hri200-bridge COM11 --hri COM7 --freq 144.85
  hri200-bridge COM11 --hri COM7 --proto kenwood --freq 145.5
  hri200-bridge COM11 --hri COM7 --proto civ --civ-address 88

Hilfe:
  hri200-bridge --help

Windows: ist SmartScreen unsicher, Rechtsklick, Eigenschaften, "Zulassen".
macOS: beim ersten Start Systemeinstellungen, Datenschutz & Sicherheit,
"Trotzdem oeffnen". Die Programme sind nicht signiert.

Ausfuehrliche Doku: https://github.com/jcmerg/yaesu_hri200
""" % VERSION

LINUX_INSTALL = """#!/bin/sh
# Install the HRI-200 programs for the current user.
set -e
here=$(cd "$(dirname "$0")" && pwd)
bindir="$HOME/.local/bin"
appdir="$HOME/.local/share/applications"
icondir="$HOME/.local/share/icons/hicolor/256x256/apps"

mkdir -p "$bindir" "$appdir" "$icondir"
for prog in hri200-gui hri200-ysf-gui hri200-bridge; do
    install -m 755 "$here/$prog" "$bindir/$prog"
done
if [ -f "$here/hri200.png" ]; then
    install -m 644 "$here/hri200.png" "$icondir/hri200.png"
fi
for entry in hri200.desktop hri200-ysf.desktop; do
    [ -f "$here/$entry" ] && install -m 644 "$here/$entry" "$appdir/$entry"
done

echo "installed to $bindir"
case ":$PATH:" in
  *":$bindir:"*) ;;
  *) echo "note: $bindir is not on your PATH" ;;
esac
"""


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


def fresh(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path)
    return path


def stage_windows(dist, top):
    for name in PANELS + (CONSOLE,):
        src = find(dist, name)
        shutil.copy2(src, os.path.join(top, os.path.basename(src)))
    with open(os.path.join(top, "README.txt"), "w") as fh:
        fh.write(README_TXT)


def stage_linux(dist, top):
    for name in PANELS + (CONSOLE,):
        dst = os.path.join(top, name)
        shutil.copy2(find(dist, name), dst)
        os.chmod(dst, os.stat(dst).st_mode | stat.S_IXUSR
                 | stat.S_IXGRP | stat.S_IXOTH)
    for entry in DESKTOP:
        shutil.copy2(os.path.join(HERE, entry), os.path.join(top, entry))
    icon = os.path.join(ICONS, "hri200-256.png")
    if os.path.exists(icon):
        shutil.copy2(icon, os.path.join(top, "hri200.png"))
    with open(os.path.join(top, "install.sh"), "w") as fh:
        fh.write(LINUX_INSTALL)
    os.chmod(os.path.join(top, "install.sh"), 0o755)
    with open(os.path.join(top, "README.txt"), "w") as fh:
        fh.write(README_TXT)


def stage_macos(dist, top):
    for name in MAC_APPS:
        src = os.path.join(dist, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(top, name), symlinks=True)
    shutil.copy2(find(dist, CONSOLE), os.path.join(top, CONSOLE))
    with open(os.path.join(top, "README.txt"), "w") as fh:
        fh.write(README_TXT)


def zip_tree(top, archive):
    """Plain zip for trees without symlinks (Windows)."""
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _dirs, files in os.walk(top):
            for name in sorted(files):
                src = os.path.join(base, name)
                arc = os.path.join(FOLDER, os.path.relpath(src, top))
                zf.write(src, arc)


def ditto_tree(top, archive):
    """ditto for trees with symlinks inside (macOS app bundles)."""
    if os.path.exists(archive):
        os.unlink(archive)
    subprocess.check_call([
        "ditto", "-c", "-k", "--norsrc", "--noextattr",
        "--keepParent", top, archive])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--out", default=os.path.join(ROOT, "out"))
    args = ap.parse_args(argv)

    if not os.path.isdir(args.out):
        os.makedirs(args.out)
    work = fresh(os.path.join(args.out, "_stage"))
    top = fresh(os.path.join(work, FOLDER))

    if sys.platform.startswith("win"):
        stage_windows(args.dist, top)
        archive = os.path.join(args.out, "HRI-200-bridge-%s-windows-%s.zip"
                               % (VERSION, arch_tag()))
        zip_tree(top, archive)
    elif sys.platform == "darwin":
        stage_macos(args.dist, top)
        archive = os.path.join(args.out, "HRI-200-bridge-%s-macos-%s.zip"
                               % (VERSION, arch_tag()))
        ditto_tree(top, archive)
    else:
        stage_linux(args.dist, top)
        archive = os.path.join(args.out, "HRI-200-bridge-%s-linux-%s.tar.gz"
                               % (VERSION, arch_tag()))
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(top, arcname=FOLDER)

    size = os.path.getsize(archive)
    print("%s  (%.1f MB)" % (archive, size / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
