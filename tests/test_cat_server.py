#!/usr/bin/env python3
"""Drive the TS-2000 CAT server over a pseudo terminal.

The server is the half of the bridge that other software talks to, so it
is worth checking without a radio: a client sends "ID;" and has to get
"ID019;" back, "TX;" has to key the radio and "RX;" has to drop it, and
the S-meter has to appear where flrig looks for it.

Needs a POSIX pseudo terminal, which is why it is skipped on Windows.

    python3 tests/test_cat_server.py
"""

import os
import select
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hri200_cat


class FakeRadio(object):
    """The parts of HRI200 that TS2000 reaches for, and nothing else."""

    def __init__(self):
        self.freq_mhz = 144.85
        self.shift_mhz = 0.0
        self.power = "mid"
        self.tone_hz = 88.5
        self.sql = "none"
        self.narrow = False
        self.tx = False
        self.rx_level = 3
        self.calls = []

    def set_ptt(self, on):
        self.tx = bool(on)
        self.calls.append(("ptt", bool(on)))

    def set_freq(self, freq_mhz):
        self.freq_mhz = float(freq_mhz)
        self.calls.append(("freq", freq_mhz))

    def set_power(self, step):
        self.power = step
        self.calls.append(("power", step))


def read_reply(fd, seconds=1.0):
    out = b""
    end = time.time() + seconds
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 256)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        if out.endswith(b";"):
            break
    return out


def main():
    if os.name != "posix":
        print("  skipped: needs a pseudo terminal")
        return 0

    radio = FakeRadio()
    master, slave = os.openpty()
    slave_name = os.ttyname(slave)
    os.close(slave)

    server = hri200_cat.TS2000(radio, slave_name, verbose=False,
                               line_ptt="off")

    def send(text):
        os.write(master, text.encode("ascii"))
        return read_reply(master).decode("ascii", "replace")

    def pump(times=6):
        for _ in range(times):
            server.pump()

    checks = []

    send("ID;")
    pump()
    got = read_reply(master)
    checks.append(("ID is answered with ID019;", got == b"ID019;"))

    send("FA;")
    pump()
    checks.append(("FA reports the frequency",
                   read_reply(master) == b"FA00144850000;"))

    send("FA00145500000;")
    pump()
    checks.append(("FA tunes the radio",
                   abs(radio.freq_mhz - 145.5) < 1e-9))
    checks.append(("FA retune is silent on the wire", read_reply(master,
                                                                0.2) == b""))

    send("MD;")
    pump()
    checks.append(("MD reports FM", read_reply(master) == b"MD4;"))

    send("SM0;")
    pump()
    # rx_level 3 out of the HRI-200's 0..4 is stretched onto the
    # TS-2000 bar by the factor seven used in hri200_cat.TS2000.
    checks.append(("SM reports the level", read_reply(master) == b"SM00021;"))

    send("TX;")
    pump()
    checks.append(("TX keys the radio", radio.tx is True))
    send("RX;")
    pump()
    checks.append(("RX drops the radio", radio.tx is False))

    send("PC;")
    pump()
    checks.append(("PC reports watts", read_reply(master) == b"PC020;"))

    send("PC005;")
    pump()
    checks.append(("PC sets the power step", radio.power == "low"))

    send("ZZ;")
    pump()
    checks.append(("unknown commands get ?;", read_reply(master) == b"?;"))

    send("IF;")
    pump()
    status = read_reply(master).decode("ascii", "replace")
    checks.append(("IF is 38 characters", len(status) == 38))
    checks.append(("IF starts with the frequency",
                   status.startswith("IF00145500000")))

    # A command split across two read cycles has to survive.
    os.write(master, b"FA0014")
    pump(2)
    os.write(master, b"6000000;")
    pump(2)
    checks.append(("a split command is assembled",
                   abs(radio.freq_mhz - 146.0) < 1e-9))

    os.close(master)
    try:
        server.ser.close()
    except Exception:
        pass

    width = max(len(name) for name, _ok in checks)
    failed = 0
    for name, ok in checks:
        print("  %-*s  %s" % (width, name, "ok" if ok else "FAILED"))
        if not ok:
            failed += 1
    print("\n%d checks, %d failed" % (len(checks), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
