#!/usr/bin/env python3
"""Check the emulated CAT dialects without any hardware.

The bridge makes the HRI-200 answer as a Kenwood, a Yaesu or an Icom,
depending on what the software expects. A nibble in the wrong place does
not raise an error - it tells flrig the radio is on 14 MHz - so the
encodings are compared against values worked out by hand, and every
dialect is driven through a full request/reply cycle against a stand-in
radio.

    python3 tests/test_radio_frames.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hri200_proto as proto


class FakeHri(object):
    """The parts of the HRI-200 the protocols reach for."""

    def __init__(self):
        self.freq_mhz = 144.85
        self.power = "mid"
        self.tx = False
        self.level = 21
        self.calls = []

    def set_freq(self, freq_mhz):
        self.freq_mhz = float(freq_mhz)
        self.calls.append(("freq", round(freq_mhz, 6)))

    def set_power(self, step):
        self.power = step
        self.calls.append(("power", step))

    def set_ptt(self, on):
        self.tx = bool(on)
        self.calls.append(("ptt", bool(on)))

    def smeter(self):
        return self.level


# -- Kenwood ----------------------------------------------------------

def kenwood_checks():
    out = []
    radio = FakeHri()
    server = proto.Kenwood(radio)

    out.append(("kenwood FA answers with 11 digits",
                server.feed(b"FA;") == b"FA00144850000;"))
    out.append(("kenwood FA tunes the box",
                (server.feed(b"FA00145500000;") or True)
                and abs(radio.freq_mhz - 145.5) < 1e-9))
    out.append(("kenwood TX keys the box",
                (server.feed(b"TX;") or True) and radio.tx is True))
    out.append(("kenwood RX drops it",
                (server.feed(b"RX;") or True) and radio.tx is False))
    out.append(("kenwood SM reports the meter",
                server.feed(b"SM0;") == b"SM00021;"))
    out.append(("kenwood ID says TS-2000",
                server.feed(b"ID;") == b"ID019;"))
    out.append(("kenwood PC reports watts",
                server.feed(b"PC;") == b"PC020;"))
    out.append(("kenwood PC sets the step",
                (server.feed(b"PC005;") or True) and radio.power == "low"))
    out.append(("kenwood says ? for what it does not know",
                server.feed(b"ZZ;") == b"?;"))

    status = server.feed(b"IF;").decode()
    out.append(("kenwood IF is 38 characters", len(status) == 38))
    out.append(("kenwood IF starts with the frequency",
                status.startswith("IF00145500000")))

    # A command split across two reads has to survive.
    server.feed(b"FA0014")
    out.append(("kenwood reassembles a split command",
                server.feed(b"6000000;") == b""
                and abs(radio.freq_mhz - 146.0) < 1e-9))
    return out


# -- Yaesu 5-byte -----------------------------------------------------

def yaesu_checks():
    out = []
    out.append(("yaesu 7.1 MHz is 07 10 00 00 in Hz",
                proto.yaesu_bcd(7100000, 8) == bytes.fromhex("07100000")))
    out.append(("yaesu picks Hz below 100 MHz",
                proto.yaesu_freq_unit(7100000) == (1, True)))
    out.append(("yaesu picks 10 Hz above 100 MHz",
                proto.yaesu_freq_unit(144850000) == (10, True)))
    out.append(("yaesu flags a frequency off the 10 Hz grid",
                proto.yaesu_freq_unit(145850001)[1] is False))
    out.append(("yaesu reads its own BCD back",
                proto.yaesu_from_bcd(bytes.fromhex("07100000")) == 7100000))

    radio = FakeHri()
    server = proto.Yaesu5(radio)

    out.append(("yaesu read frequency answers 5 bytes",
                len(server.feed(bytes([0, 0, 0, 0, 0x03]))) == 5))
    out.append(("yaesu read frequency is the current one",
                server.feed(bytes([0, 0, 0, 0, 0x03]))[:4]
                == bytes.fromhex("14485000")))
    # 145.5 MHz does not fit eight BCD digits in Hz, so the field is in
    # 10 Hz steps: 14550000 -> 14 55 00 00.
    out.append(("yaesu set frequency tunes the box",
                (server.feed(bytes.fromhex("14550000") + b"\x01") or True)
                and abs(radio.freq_mhz - 145.5) < 1e-9))
    out.append(("yaesu PTT on is opcode 0x08",
                (server.feed(bytes([0, 0, 0, 0, 0x08])) or True)
                and radio.tx is True))
    out.append(("yaesu TX status reports bit 7",
                server.feed(bytes([0, 0, 0, 0, 0xF7])) == b"\x80"))
    out.append(("yaesu PTT off is opcode 0x88",
                (server.feed(bytes([0, 0, 0, 0, 0x88])) or True)
                and radio.tx is False))
    out.append(("yaesu TX status clears bit 7",
                server.feed(bytes([0, 0, 0, 0, 0xF7])) == b"\x00"))
    out.append(("yaesu set mode FM is 0x04",
                (server.feed(bytes([0, 0, 0, 0x04, 0x07])) or True)
                and server.mode == "fm"))
    out.append(("yaesu stays silent on an opcode it does not know",
                server.feed(bytes([0, 0, 0, 0, 0x99])) == b""))

    # Frames are only taken five bytes at a time.
    server.feed(bytes([0, 0]))
    out.append(("yaesu waits for a whole frame",
                server.feed(bytes([0, 0, 0xF7])) == b"\x00"))
    return out


# -- Icom CI-V --------------------------------------------------------

def civ_checks():
    out = []
    out.append(("civ 144.85 MHz is 00 00 85 44 01",
                proto.civ_bcd_le(144850000) == bytes.fromhex("0000854401")))
    out.append(("civ 7.1 MHz is 00 00 10 07 00",
                proto.civ_bcd_le(7100000) == bytes.fromhex("0000100700")))
    out.append(("civ reads its own BCD back",
                proto.civ_from_bcd_le(bytes.fromhex("0000854401"))
                == 144850000))

    radio = FakeHri()
    server = proto.IcomCiv(radio, address=0x88)

    reply = server.feed(bytes.fromhex("FEFE88E004FD"))
    out.append(("civ 0x04 answers with the frequency",
                reply == bytes.fromhex("FEFEE088" "04" "0000854401" "FD")))
    out.append(("civ 0x05 tunes the box",
                (server.feed(bytes.fromhex("FEFE88E0050000100700FD")) or True)
                and abs(radio.freq_mhz - 7.1) < 1e-9))
    out.append(("civ PTT on",
                (server.feed(bytes.fromhex("FEFE88E01C0001FD")) or True)
                and radio.tx is True))
    out.append(("civ PTT off",
                (server.feed(bytes.fromhex("FEFE88E01C0000FD")) or True)
                and radio.tx is False))

    meter = server.feed(bytes.fromhex("FEFE88E01502FD"))
    out.append(("civ meter reports the level",
                meter == bytes.fromhex("FEFEE088" "1502" "2100" "FD")))
    out.append(("civ echoes the mode it was given",
                server.feed(bytes.fromhex("FEFE88E0060501FD"))
                == bytes.fromhex("FEFEE088060501FD")))
    out.append(("civ says FB for what it cannot do",
                server.feed(bytes.fromhex("FEFE88E099FD"))
                == bytes.fromhex("FEFEE088FB99FD")))
    out.append(("civ ignores frames addressed to somebody else",
                server.feed(bytes.fromhex("FEFE99E004FD")) == b""))

    # Two frames in one read, which is what a real port delivers.
    both = server.feed(bytes.fromhex("FEFE88E004FDFEFE88E01C0001FD"))
    out.append(("civ handles two frames in one read",
                both.count(b"\xfd") == 1 and radio.tx is True))
    return out


# -- auto-detection ---------------------------------------------------

def auto_checks():
    out = []
    out.append(("auto sees CI-V in a preamble",
                proto.detect(bytes.fromhex("FEFE88E004FD")) == "civ"))
    out.append(("auto sees Yaesu in five binary bytes",
                proto.detect(bytes([0, 0, 0, 0, 0x03])) == "yaesu"))
    out.append(("auto sees Kenwood in ASCII",
                proto.detect(b"FA;") == "kenwood"))
    out.append(("auto decides nothing on an empty buffer",
                proto.detect(b"") is None))

    radio = FakeHri()
    server = proto.make("auto", radio)
    out.append(("auto stays quiet until it knows",
                server.feed(bytes([0, 0])) == b""))
    reply = server.feed(bytes([0, 0, 0x03]))
    out.append(("auto answers Yaesu once it knows",
                len(reply) == 5 and server.delegate.name == "yaesu"))

    radio2 = FakeHri()
    server2 = proto.make("auto", radio2)
    out.append(("auto answers Kenwood once it knows",
                server2.feed(b"FA;") == b"FA00144850000;"
                and server2.delegate.name == "kenwood"))

    radio3 = FakeHri()
    server3 = proto.make("auto", radio3)
    out.append(("auto answers CI-V once it knows",
                server3.feed(bytes.fromhex("FEFE88E004FD")).startswith(
                    b"\xfe\xfe\xe0\x88\x04")
                and server3.delegate.name == "civ"))
    return out


# -- factory ----------------------------------------------------------

def factory_checks():
    out = []
    radio = FakeHri()
    out.append(("every dialect is registered",
                sorted(proto.PROTOCOLS) == ["civ", "kenwood", "yaesu"]))
    for name in ("kenwood", "yaesu", "civ", "auto"):
        out.append(("make(%r) builds something" % name,
                    proto.make(name, radio) is not None))
    try:
        proto.make("nope", radio)
    except ValueError:
        out.append(("an unknown dialect is refused", True))
    else:
        out.append(("an unknown dialect is refused", False))

    # PTT must not be left on when a protocol is torn down.
    radio2 = FakeHri()
    server = proto.make("kenwood", radio2)
    server.feed(b"TX;")
    server.close()
    out.append(("closing a protocol unkeys the radio", radio2.tx is False))
    return out


def main():
    checks = (kenwood_checks() + yaesu_checks() + civ_checks()
              + auto_checks() + factory_checks())
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
