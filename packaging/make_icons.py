#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Draw the application icon and emit PNG, ICO and ICNS.

No drawing library is involved: the image is computed pixel by pixel and
encoded with zlib, so this runs on a bare Python with nothing installed.
That matters because the icon is part of the build, and a build that
needs a graphics stack on three operating systems is a build that breaks
on one of them.

The picture is a rounded tile with three broadcast arcs and a feed
point - the same idea at every size, rendered natively rather than
scaled, so a 16 pixel icon is still legible.

    python3 packaging/make_icons.py
"""

import os
import struct
import sys
import zlib

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

SIZES = [16, 32, 48, 64, 128, 256, 512, 1024]

BG_TOP = (0x0d, 0x2b, 0x45)
BG_BOTTOM = (0x12, 0x73, 0x8c)
FG = (0xf7, 0xfb, 0xff)
ACCENT = (0xff, 0xb3, 0x00)


def rounded_alpha(x, y, size, radius):
    """Inside test for a rounded square, returned as coverage 0..1."""
    r = radius * size
    left, top = r, r
    right, bottom = size - r, size - r
    if left <= x <= right or top <= y <= bottom:
        inside_x = left <= x <= right
        inside_y = top <= y <= bottom
        if inside_x or inside_y:
            return 1.0
    cx = left if x < left else right
    cy = top if y < top else bottom
    dx = x - cx
    dy = y - cy
    return 1.0 if dx * dx + dy * dy <= r * r else 0.0


def ring_coverage(x, y, size, cx, cy, radius, width):
    """Stroke coverage of a circle, in normalised coordinates."""
    dx = (x / size) - cx
    dy = (y / size) - cy
    dist = (dx * dx + dy * dy) ** 0.5
    half = width / 2.0
    delta = abs(dist - radius)
    if delta > half:
        return 0.0
    # A soft edge of one pixel and a half keeps small sizes from
    # turning into a dotted line.
    feather = 1.5 / size
    return max(0.0, min(1.0, (half - delta) / feather + 0.5))


def render(size):
    """Return ``size``x``size`` RGBA bytes."""
    px = bytearray(size * size * 4)
    radius = 0.22
    cx, cy = 0.20, 0.80
    rings = ((0.20, 0.075), (0.34, 0.065), (0.48, 0.055))

    for y in range(size):
        ty = (y + 0.5) / size
        # Vertical gradient for the tile.
        mix = ty
        base = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * mix)
                     for i in range(3))
        for x in range(size):
            tx = (x + 0.5) / size
            tile = rounded_alpha(tx * size, ty * size, size, radius)
            if tile <= 0.0:
                continue
            r, g, b = base

            # Broadcast arcs, only in the upper-right quadrant.
            ink = 0.0
            if tx >= cx and ty <= cy:
                for ring_radius, width in rings:
                    ink = max(ink, ring_coverage(x + 0.5, y + 0.5, size,
                                                 cx, cy, ring_radius, width))
            # The feed point.
            dx = tx - cx
            dy = ty - cy
            if (dx * dx + dy * dy) ** 0.5 <= 0.085:
                ink = max(ink, 1.0)

            if ink > 0:
                r, g, b = (int(r + (FG[0] - r) * ink),
                           int(g + (FG[1] - g) * ink),
                           int(b + (FG[2] - b) * ink))

            off = (y * size + x) * 4
            px[off] = r
            px[off + 1] = g
            px[off + 2] = b
            px[off + 3] = int(255 * tile)
    return bytes(px)


def png(size, rgba):
    """Minimal PNG encoder: one IDAT of filter-0 scanlines."""
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def ico(images):
    """ICO with PNG payloads, which Windows has accepted since Vista."""
    entries = sorted(images.items())
    header = struct.pack("<HHH", 0, 1, len(entries))
    directory = b""
    payload = b""
    offset = 6 + 16 * len(entries)
    for size, data in entries:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                                 len(data), offset + len(payload))
        payload += data
    return header + directory + payload


ICNS_TYPES = [(16, b"icp4"), (32, b"icp5"), (64, b"icp6"),
              (128, b"ic07"), (256, b"ic08"), (512, b"ic09"),
              (1024, b"ic10")]


def icns(images):
    body = b""
    for size, tag in ICNS_TYPES:
        if size in images:
            data = images[size]
            body += tag + struct.pack(">I", len(data) + 8) + data
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def main():
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)

    images = {}
    for size in SIZES:
        images[size] = png(size, render(size))
        path = os.path.join(OUT_DIR, "hri200-%d.png" % size)
        with open(path, "wb") as fh:
            fh.write(images[size])
        print("wrote %s (%d bytes)" % (path, len(images[size])))

    ico_sizes = [s for s in images if s <= 256]
    path = os.path.join(OUT_DIR, "hri200.ico")
    with open(path, "wb") as fh:
        fh.write(ico({s: images[s] for s in ico_sizes}))
    print("wrote %s" % path)

    path = os.path.join(OUT_DIR, "hri200.icns")
    with open(path, "wb") as fh:
        fh.write(icns(images))
    print("wrote %s" % path)

    # The desktop entry points at the 256 pixel PNG, which every Linux
    # desktop environment can read.
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "hri200.desktop")
    if os.path.exists(src):
        print("desktop entry: %s" % src)
    return 0


if __name__ == "__main__":
    sys.exit(main())
