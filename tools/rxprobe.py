#!/usr/bin/env python3
"""Find out what the HRI-200 sends when the radio receives C4FM.

Run this, then key a second radio on the node frequency in C4FM for a
few seconds. Everything the box sends is logged verbatim, and every
frame is searched for voice.

    python3 tools/rxprobe.py --hri COM7 --freq 144.85 --out rx.txt

The search makes no assumption about how a receive frame is laid out.
It walks every run of hex characters in the frame, cuts 65-byte windows
out of it at every offset, and runs the triplet test from README.md over
each one. Voice passes at 100 %, anything else at the 25 % a coin toss
gives, so a hit is a hit no matter what command letter, counter or header
the box wrapped it in — and the report says at which offset it sat.

If nothing arrives at all, try --poll: the box may want to be asked.
"""

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import hri200_cat
    from hri200_cat import EOT, SOH, HRI200
    from hri200_ysf import PAYLOAD_LENGTH, VCH_COUNT, VCH_LENGTH, is_voice
except ImportError as exc:
    sys.exit("%s\n\nThis needs hri200_cat.py and hri200_ysf.py in the "
             "directory above it.\nCopy the repository, not the single "
             "file." % exc)

HEX_RUN = re.compile(r"[0-9A-Fa-f]{%d,}" % (2 * PAYLOAD_LENGTH))


def find_voice(body):
    """Locate a V/D mode 2 payload anywhere in a frame body.

    Returns (character offset, 65 bytes) or None. Offsets are reported so
    that a layout other than the one D1E uses in the write direction can
    be read straight off the output.
    """
    for run in HEX_RUN.finditer(body):
        text, base = run.group(0), run.start()
        for i in range(0, len(text) - 2 * PAYLOAD_LENGTH + 1):
            window = text[i:i + 2 * PAYLOAD_LENGTH]
            try:
                payload = bytes.fromhex(window)
            except ValueError:
                continue
            blocks = [payload[VCH_LENGTH * n:VCH_LENGTH * (n + 1)]
                      for n in range(VCH_COUNT)]
            if all(is_voice(b) for b in blocks):
                return base + i, payload
    return None


class Probe(HRI200):
    """HRI-200 that listens instead of talking.

    The inherited poll loop consumes replies to match them to commands,
    which is the wrong shape here: nothing is being asked, and anything
    that arrives is the point.
    """

    def __init__(self, *args, **kwargs):
        self.log = kwargs.pop("log", None)
        self.poll_fast = kwargs.pop("poll_fast", False)
        self.digital = kwargs.pop("digital", True)
        self.dgid = kwargs.pop("dgid", 0)
        HRI200.__init__(self, *args, **kwargs)
        self.frames = 0
        self.hits = 0
        self.unknown = {}

    def _d1m(self):
        return hri200_cat.d1m(self.freq_mhz, self.shift_mhz, power=self.power,
                              tone_hz=self.tone_hz, sql=self.sql,
                              narrow=self.narrow, digital=self.digital,
                              dgid=self.dgid)

    def _poll(self):
        beat = time.time()
        while self.running:
            now = time.time()
            if now >= beat:
                beat = now + (0.1 if self.poll_fast else 1.0)
                with self.lock:
                    self._send("D1C0000" if self.poll_fast else "P010000")
            with self.lock:
                waiting = self.ser.in_waiting
                if waiting:
                    self.buf += self.ser.read(waiting)
                while EOT in self.buf:
                    chunk, self.buf = self.buf.split(EOT, 1)
                    self._report(chunk.lstrip(SOH))
            time.sleep(0.01)

    def _report(self, chunk):
        if not chunk:
            return
        self.frames += 1
        body = chunk.decode("ascii", "replace")
        if self.log:
            self.log.write("%.3f %s\n" % (time.time(), body))
            self.log.flush()

        found = find_voice(body)
        if found:
            offset, payload = found
            self.hits += 1
            if self.hits == 1:
                print("\n*** voice at character offset %d of a %d-character "
                      "frame" % (offset, len(body)))
                print("    header: %r" % body[:offset])
                print("    that is the layout the gateway needs\n")
            return

        # Frames the write direction already explains are not news.
        if body.startswith(("D1C", "B0", "M00", "R", "D1V", "D1M", "D1B")):
            return
        key = body[:7]
        if key not in self.unknown:
            self.unknown[key] = body
            print("  unknown frame: %s" % (body if len(body) < 90
                                           else body[:87] + "..."))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hri", default="COM7", help="HRI-200 serial port")
    ap.add_argument("--freq", type=float, default=144.85, help="MHz")
    ap.add_argument("--dgid", type=int, default=0, help="DG-ID 0..99")
    ap.add_argument("--seconds", type=float, default=120.0, help="run time")
    ap.add_argument("--out", default="rxprobe.txt", help="raw frame log")
    ap.add_argument("--poll", action="store_true",
                    help="ask with D1C0000 at 10 Hz instead of idling at 1 Hz")
    ap.add_argument("--analog", action="store_true",
                    help="stay in FM instead of switching to digital")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if hri200_cat.serial is None:
        sys.exit("pyserial missing:  py -3 -m pip install pyserial")

    log = open(args.out, "w", encoding="ascii")
    probe = Probe(args.hri, args.freq, verbose=args.verbose, log=log,
                  poll_fast=args.poll, digital=not args.analog,
                  dgid=args.dgid)
    probe.open()
    print("listening for %.0f s -- key a radio on %.4f MHz in C4FM now"
          % (args.seconds, args.freq))
    end = time.time() + args.seconds
    try:
        while time.time() < end:
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        probe.close()
        log.close()

    print("\n%d frames from the box, %d carried voice" % (probe.frames,
                                                          probe.hits))
    if not probe.hits:
        print("no voice found. %s"
              % ("try again without --poll" if args.poll
                 else "try --poll: the box may want to be asked"))
    print("raw log: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
