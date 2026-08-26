#!/usr/bin/env python3
"""Extract HRI-200 protocol frames from a Device Monitoring Studio log.

DMS writes .dmslog8 as a proprietary binary container, but the captured
payload bytes sit in it verbatim, so the SOH/EOT framing can simply be
scanned for.  Records appear twice (IRP request and completion), which
is why identical adjacent frames are collapsed.

    python3 tools/dmslog.py capture.dmslog8            # plain text listing
    python3 tools/dmslog.py capture.dmslog8 --only D1E

Note this recovers writes only, PC to HRI-200.  Reads are not present in
the captures taken so far.
"""

import argparse
import re
import sys

FRAME = re.compile(rb"\x01([\x20-\x7e]{1,600}?)\x04")
PREFIXES = (b"M00", b"R6", b"P0", b"P1", b"B0", b"D1")


def frames(path, prefixes=PREFIXES):
    """Yield (file offset, frame body) for every protocol frame in the log."""
    data = open(path, "rb").read()
    previous = None
    for match in FRAME.finditer(data):
        body = match.group(1)
        if not body.startswith(prefixes) or len(body) < 3:
            continue
        if body == previous:
            continue
        previous = body
        yield match.start(), body


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfile")
    ap.add_argument("--only", metavar="PREFIX", help="keep frames starting with this")
    args = ap.parse_args()

    keep = args.only.encode() if args.only else b""
    for offset, body in frames(args.logfile):
        if body.startswith(keep):
            print(f"{offset:08x} {body.decode()}")


if __name__ == "__main__":
    sys.exit(main())
