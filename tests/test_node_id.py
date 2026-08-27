#!/usr/bin/env python3
"""Rebuild the captured frames of the write direction.

The node identification frame is checked against the capture byte for
byte, which is the only one of the three whose room fields are empty and
therefore the only one this gateway could ever send. Its checksum is
then verified against all three, including the two taken while the node
was connected to a WIRES-X room.

    python3 tests/test_node_id.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hri200_ysf

CAPTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "captures", "20260826-183057-digital.txt")
NUMBER = "97201"
NAME = "DL4JC-ND"
CALL = "DL4JC"
CITY = "Riegelsberg"
HEAD_LENGTH = 70        # ASCII characters ahead of the hex payload


def node_frames():
    out = []
    for line in open(CAPTURE):
        body = line.split(None, 1)[1].strip()
        if body.startswith("D1F") and len(body) - 7 > 100:
            out.append(body)
    return out


def main():
    frames = node_frames()
    checks = [("three identification frames in the capture", len(frames) == 3)]

    # the last one was sent after the operator had left the room
    built = hri200_ysf.d1f_node(NUMBER, NAME, CALL, CITY, 0x83)
    checks.append(("the disconnected frame is rebuilt byte for byte",
                   built == frames[-1]))

    for n, body in enumerate(frames):
        head = body[7:7 + HEAD_LENGTH]
        payload = bytes.fromhex(body[7 + HEAD_LENGTH:])
        want = (sum(head.encode("ascii")) + sum(payload[:-1])
                + hri200_ysf.NODE_CHECK) & 0xFF
        checks.append(("checksum of frame %d" % n, want == payload[-1]))

    short = hri200_ysf.d1f("*****FD2wr", "DL8RN", "DL4JC", "", rem2="     FD2wr")
    checks.append(("the transmission header keeps its length",
                   len(short) - 7 == 82))

    for name, ok in checks:
        print("  %-46s %s" % (name, "ok" if ok else "FAILED"))
    failed = [n for n, ok in checks if not ok]
    print("FAIL" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
