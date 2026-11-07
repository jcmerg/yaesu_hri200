#!/usr/bin/env python3
"""Check the CAT frame builders without any hardware.

Every backend encodes a frequency, a mode and a keying command in its
own dialect, and a nibble in the wrong place does not produce an error -
it tunes the radio somewhere else. These are the checks that catch that
before a radio is plugged in.

    python3 tests/test_radio_frames.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hri200_radio as radio


def yaesu_checks():
    out = []
    # The 817 family documents Hz in four BCD bytes. 7.1 MHz fits with
    # room to spare, so the digit order can be read straight off.
    out.append(("yaesu 7.1 MHz is 07 10 00 00 01",
                radio.yaesu_freq_frame(7100000)
                == bytes.fromhex("0710000001")))
    # 144.85 MHz does not fit in Hz steps, which is exactly what the
    # unit parameter and the automatic choice are for.
    out.append(("yaesu 144.85 MHz in 10 Hz steps is 14 48 50 00 01",
                radio.yaesu_freq_frame(144850000, 10)
                == bytes.fromhex("1448500001")))
    out.append(("yaesu picks Hz below 100 MHz",
                radio.yaesu_pick_unit(7100000) == (1, True)))
    out.append(("yaesu picks 10 Hz above 100 MHz",
                radio.yaesu_pick_unit(144850000) == (10, True)))
    # A frequency that is not a multiple of 10 Hz cannot be expressed
    # exactly above 100 MHz; the caller is told rather than left to
    # discover it on the radio's display.
    out.append(("yaesu flags a frequency off the 10 Hz grid",
                radio.yaesu_pick_unit(145850001)[1] is False))
    try:
        radio.yaesu_freq_frame(144850000)
    except ValueError:
        out.append(("yaesu refuses a frequency that does not fit", True))
    else:
        out.append(("yaesu refuses a frequency that does not fit", False))
    out.append(("yaesu FM is 0x40",
                radio.yaesu_mode_frame("fm") == bytes.fromhex("0000004007")))
    out.append(("yaesu FM-narrow is 0xC0",
                radio.yaesu_mode_frame("fm-n") == bytes.fromhex("000000C007")))
    out.append(("yaesu PTT on is opcode 0x08",
                radio.yaesu_ptt_frame(True) == bytes.fromhex("0000000008")))
    out.append(("yaesu PTT off is opcode 0x88",
                radio.yaesu_ptt_frame(False) == bytes.fromhex("0000000088")))
    out.append(("every yaesu frame is five bytes",
                all(len(f) == 5 for f in (
                    radio.yaesu_freq_frame(7100000),
                    radio.yaesu_mode_frame("fm"),
                    radio.yaesu_ptt_frame(True),
                    radio.yaesu_read_tx_frame(),
                    radio.yaesu_power_frame(255)))))
    return out


def kenwood_checks():
    out = []
    out.append(("kenwood frequency is 11 digits",
                radio.kenwood_freq_cmd(144850000) == "FA00144850000;"))
    out.append(("kenwood FM is mode 4",
                radio.kenwood_mode_cmd("fm") == "MD4;"))
    out.append(("kenwood PTT on is TX;",
                radio.kenwood_ptt_cmd(True) == "TX;"))
    out.append(("kenwood PTT off is RX;",
                radio.kenwood_ptt_cmd(False) == "RX;"))
    out.append(("kenwood power pads to three digits",
                radio.kenwood_power_cmd(5) == "PC005;"))
    return out


def civ_checks():
    out = []
    # Read from the left the frequency is 01 44 85 00 00; CI-V sends
    # the lowest pair first, so the wire carries 00 00 85 44 01.
    out.append(("civ 144.85 MHz is 00 00 85 44 01",
                radio.civ_bcd_le(144850000, 10)
                == bytes.fromhex("0000854401")))
    out.append(("civ 7.1 MHz is 00 00 10 07 00",
                radio.civ_bcd_le(7100000, 10) == bytes.fromhex("0000100700")))
    out.append(("civ frames are FE FE .. FD",
                radio.civ_frame(0x88, 0x05, b"\x01")[:2] == b"\xfe\xfe"
                and radio.civ_frame(0x88, 0x05, b"\x01")[-1:] == b"\xfd"))
    out.append(("civ PTT on is 1C 00 01",
                radio.civ_ptt_frame(0x88, True)
                == bytes.fromhex("FEFE88E01C0001FD")))
    out.append(("civ PTT off is 1C 00 00",
                radio.civ_ptt_frame(0x88, False)
                == bytes.fromhex("FEFE88E01C0000FD")))
    reply = bytes.fromhex("FEFEE088150201 23FD".replace(" ", ""))
    parsed = radio.parse_civ_reply(reply, 0x88)
    out.append(("civ reply parses", parsed is not None
                and parsed[0] == radio.CIV_CMD_READ_METER))
    out.append(("civ reply filters on the radio address",
                radio.parse_civ_reply(reply, 0x99) is None))
    out.append(("civ meter 0123 is 15 on the bar",
                radio.civ_smeter_to_scale(bytes.fromhex("0123")) == 15))
    out.append(("civ meter clamps at 30",
                radio.civ_smeter_to_scale(bytes.fromhex("0255")) == 30))
    out.append(("civ cannot pack more than five BCD bytes",
                len(radio.civ_bcd_le(9999999999, 10)) == 5))
    return out


def backend_checks():
    out = []

    class Fake(radio.SerialRadio):
        name = "fake"

        def __init__(self):
            radio.SerialRadio.__init__(self, "fake", verbose=False)
            self.written = []

        def _write(self, data):
            self.written.append(data)

        def _read(self, count=64, seconds=0.0):
            return b""

        def freq_frame(self):
            return b"F"

        def ptt_frame(self, on):
            return b"P1" if on else b"P0"

        def apply(self):
            self._write(b"A")

    r = Fake()
    r.set_ptt(True)
    r.set_ptt(True)
    r.set_ptt(False)
    out.append(("PTT is idempotent", r.written == [b"P1", b"P0"]))
    out.append(("PTT state is tracked", r.tx is False))

    r2 = Fake()
    r2.set_freq(145.5)
    out.append(("set_freq reaches the radio", r2.written == [b"F"]))
    out.append(("set_freq is remembered", abs(r2.freq_mhz - 145.5) < 1e-9))

    r3 = Fake()

    def boom():
        raise radio.RadioError("no")
    r3.freq_frame = boom
    try:
        r3.set_freq(100.0)
    except radio.RadioError:
        out.append(("a failed retune is rolled back",
                    abs(r3.freq_mhz) < 1e-9))
    else:
        out.append(("a failed retune is rolled back", False))

    out.append(("every backend is registered",
                sorted(radio.BACKENDS) == ["hri200", "icom", "kenwood",
                                           "yaesu"]))
    try:
        radio.create_radio("nope", "port")
    except ValueError:
        out.append(("unknown backend is refused", True))
    else:
        out.append(("unknown backend is refused", False))
    return out


def adapter_checks():
    out = []

    class Stub(object):
        freq_mhz = 144.85
        power = "mid"
        tx = False

        def __init__(self):
            self.smeter_value = 21
            self.ptt = None

        def smeter(self):
            return self.smeter_value

        def set_ptt(self, on):
            self.ptt = on

        def set_freq(self, mhz):
            self.freq_mhz = mhz

        def set_power(self, step):
            self.power = step

    stub = Stub()
    handle = radio.CatHandle(stub)
    out.append(("adapter exposes freq_mhz", handle.freq_mhz == 144.85))
    out.append(("adapter scales the meter to 0..4", handle.rx_level == 3))
    stub.smeter_value = 30
    out.append(("adapter clamps the meter at 4", handle.rx_level == 4))
    handle.set_ptt(True)
    out.append(("adapter forwards PTT", stub.ptt is True))
    return out


def main():
    checks = (yaesu_checks() + kenwood_checks() + civ_checks()
              + backend_checks() + adapter_checks())
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
