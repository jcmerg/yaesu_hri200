#!/usr/bin/env python3
"""Find out what the HRI-200 makes of the D1M mode field.

The box does not echo D1M. Asked for mode 7000 — the value the original
software used with the node in digital — an FTM-510D answered 5000, one
bit short of what was asked for. Which bits it will take and which it
drops is what this walks through:

    python3 tools/modeprobe.py --hri COM7 --freq 144.85

Every value of the first digit is sent in turn and the reply printed
beside it, then the digital flag in the TX block is tried both ways.
Nothing is transmitted; only the channel configuration is touched, and
the settings the gateway uses are restored at the end.

A value that comes back unchanged is one the radio accepted. One that
comes back different is the radio saying what it is instead, and the
difference is the interesting part.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import hri200_cat
    from hri200_cat import HRI200
except ImportError as exc:
    sys.exit("%s\n\nThis needs hri200_cat.py in the directory above it."
             % exc)

MODE_FM = "4000"
MODE_DIGITAL = "7000"


class Probe(HRI200):
    """The box with the inherited 1 Hz poll, which keeps the radio awake."""

    def __init__(self, *args, **kwargs):
        self.digital = kwargs.pop("digital", True)
        self.dgid = kwargs.pop("dgid", 0)
        HRI200.__init__(self, *args, **kwargs)

    def _d1m(self):
        return hri200_cat.d1m(self.freq_mhz, self.shift_mhz, power=self.power,
                              tone_hz=self.tone_hz, sql=self.sql,
                              narrow=self.narrow, digital=self.digital,
                              dgid=self.dgid)

    def d1m_raw(self, mode, freq, dgid=0, tx_digital="1"):
        """A D1M with the mode field set by hand."""
        body = (mode
                + hri200_cat.block(freq, 0.0, False, "none", 88.5, "023",
                                   hri200_cat.POWER["mid"], dgid=dgid)
                + hri200_cat.block(freq, 0.0, False, "none", 88.5, "754",
                                   "2", tail=tx_digital)
                + "F")
        return hri200_cat.d1m(freq, raw=body)

    def try_mode(self, mode, freq, tx_digital="1"):
        """Send one D1M and report the mode field that comes back."""
        cmd = self.d1m_raw(mode, freq, tx_digital=tx_digital)
        reply = self._exchange(cmd, b"D1M", wait=1.5)
        if reply is None:
            return None, False
        reply = reply.decode("ascii", "replace")
        same_rest = reply[11:] == cmd[11:len(reply)]
        self._exchange("D1B00010", b"D1B", wait=1.0)
        return reply[7:11], same_rest


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hri", default="COM7", help="HRI-200 serial port")
    ap.add_argument("--freq", type=float, default=144.85, help="MHz")
    ap.add_argument("--dgid", type=int, default=0, help="DG-ID 0..99")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if hri200_cat.serial is None:
        sys.exit("pyserial missing:  py -3 -m pip install pyserial")

    probe = Probe(args.hri, args.freq, verbose=args.verbose, dgid=args.dgid)
    probe.open()
    print("")

    try:
        print("  mode field, TX block digital flag set")
        print("  asked   answered  rest unchanged")
        for digit in "0123456789ABCDEF":
            mode = digit + "000"
            got, same = probe.try_mode(mode, args.freq)
            print("  %s    %s      %s%s"
                  % (mode, got or "(no reply)", "yes" if same else "NO",
                     "   <- taken as asked" if got == mode else ""))

        print("\n  the two values the capture shows, flag both ways")
        for mode in (MODE_FM, MODE_DIGITAL):
            for flag in ("0", "1"):
                got, same = probe.try_mode(mode, args.freq, tx_digital=flag)
                print("  %s flag %s -> %s" % (mode, flag, got or "(no reply)"))

        print("\n  restoring what the gateway sets")
        probe.try_mode(MODE_DIGITAL, args.freq)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        probe.close()

    print("\nA value that came back as asked is one the radio took. Where the\n"
          "answer differs, the bits it dropped are the ones it will not do.")


if __name__ == "__main__":
    sys.exit(main())
