#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hri200_proto.py - make the HRI-200 look like the radio the software wants

The box is the radio interface: one serial port, one protocol, and audio
through its USB sound card. What varies is the *other* side. Software
that speaks CAT does not agree on what CAT is, so this module answers in
three dialects on the virtual port:

    kenwood   ASCII, "FA00014485000;"   flrig, fldigi, WSJT-X, HRD
    yaesu     5-byte binary, 0x03 opcode  legacy FT-817/857 profiles
    civ       Icom CI-V, FE FE .. FD     Icom-oriented software

Pick one with --proto, or let ``auto`` work it out from the first bytes
that arrive. Auto is safe because the dialects do not overlap: CI-V
frames start with 0xFE, Kenwood commands are ASCII letters, and a Yaesu
5-byte frame is neither.

Everything here is about the wire format and nothing about the radio.
Each protocol is handed a ``Radio``-shaped object and asks it to retune,
key, or report its meter; the HRI-200 code in hri200_cat.py does the rest.

One thing worth stating plainly, because it is the difference between a
protocol that works and one that looks like it does: the Yaesu 5-byte
dialect has four BCD bytes for the frequency. In Hz that stops below
100 MHz, which rules out every 2 m channel. Legacy radios in the field do
reach 145 MHz over that protocol, so above 100 MHz the field is written
in 10 Hz steps, which is what --yaesu-unit selects if ``auto`` guesses
wrong for a given piece of software.

The frame builders are pure functions so they can be checked without a
virtual port; see tests/test_radio_frames.py.
"""

import sys

if sys.version_info < (3, 6):
    sys.exit("Python 3.6 or newer required")


POWER = ("high", "mid", "low")

# Watts for the Kenwood PC command. The HRI-200 only knows high, mid and
# low; these are the numbers the original software displays for them, not
# a measurement.
POWER_WATT = {"high": 50, "mid": 20, "low": 5}


def watt_to_step(watts):
    if watts <= 10:
        return "low"
    if watts <= 30:
        return "mid"
    return "high"


def clamp_level(level):
    return max(0, min(30, int(level)))


# ----------------------------------------------------------------------
# Kenwood ASCII
#
# The dialect the bridge has always offered, because it is the one the
# widest range of software speaks. Commands are a two-letter code, a
# parameter and a semicolon; a command without a parameter is a read.
# ----------------------------------------------------------------------

KENWOOD_MODE = {"lsb": 1, "usb": 2, "cw": 3, "fm": 4, "am": 5, "fm-n": 4}


def kenwood_freq(hz):
    return "FA%011d;" % int(hz)


def kenwood_mode(mode):
    return "MD%d;" % KENWOOD_MODE.get(mode, 4)


def kenwood_power(step):
    return "PC%03d;" % POWER_WATT[step]


def kenwood_smeter(level):
    return "SM0%04d;" % clamp_level(level)


class Kenwood(object):
    """A TS-2000 that is really an HRI-200."""

    name = "kenwood"
    label = "Kenwood TS-2000"

    def __init__(self, radio, verbose=False, line=None):
        self.radio = radio
        self.verbose = verbose
        self.buf = ""
        self.mode = "4"
        self.cat_tx = False

    # -- detection ----------------------------------------------------
    @staticmethod
    def looks_like(data):
        text = data.lstrip(b"\x01\x04")
        return bool(text) and all(0x20 <= byte < 0x7F for byte in text)

    # -- serving ------------------------------------------------------
    def feed(self, data):
        self.buf += data.decode("ascii", "ignore")
        out = []
        while ";" in self.buf:
            cmd, self.buf = self.buf.split(";", 1)
            cmd = cmd.strip()
            if not cmd:
                continue
            reply = self.answer(cmd)
            if self.verbose:
                print("    [kenwood] %-14s -> %s" % (cmd, reply))
            if reply:
                out.append(reply.encode("ascii"))
        return b"".join(out)

    def freq_hz(self):
        return int(round(self.radio.freq_mhz * 1e6))

    def answer(self, cmd):
        code = cmd[:2]
        if cmd == "ID":
            return "ID019;"
        if code == "PS":
            return "PS1;"
        if code == "AI":
            return "AI0;"
        if code in ("FA", "FB"):
            if len(cmd) > 2 and cmd[2:].isdigit():
                self.radio.set_freq(int(cmd[2:]) / 1e6)
                return None
            return "%s%011d;" % (code, self.freq_hz())
        if code == "MD":
            if len(cmd) > 2:
                self.mode = cmd[2]
                return None
            return "MD%s;" % self.mode
        if code == "SM":
            return kenwood_smeter(self.radio.smeter())
        if cmd == "TX":
            self.cat_tx = True
            self.radio.set_ptt(True)
            return None
        if cmd == "RX":
            self.cat_tx = False
            self.radio.set_ptt(False)
            return None
        if code == "IF":
            # 38 characters, as the TS-2000 sends them.
            return ("IF"
                    + "%011d" % self.freq_hz()
                    + "0000" + "+00000"
                    + "0" + "0" + "0" + "00"
                    + ("1" if self.radio.tx else "0")
                    + self.mode
                    + "0" + "0" + "0" + "0" + "00" + "0"
                    + ";")
        if code in ("FR", "FT"):
            return "%s0;" % code
        if code == "AG":
            return "AG0000;"
        if code == "RF":
            return "RF0000;"
        if code == "SQ":
            return "SQ0000;"
        if code == "PC":
            if len(cmd) > 2 and cmd[2:].isdigit():
                self.radio.set_power(watt_to_step(int(cmd[2:])))
                return None
            return kenwood_power(self.radio.power)
        # A Kenwood says so out loud when it does not know a command.
        return "?;"

    def poll(self):
        """Nothing to poll: the bridge keeps the HRI-200 awake itself."""
        return b""

    def close(self):
        self.radio.set_ptt(False)


# ----------------------------------------------------------------------
# Yaesu 5-byte binary
#
# Every command is four data bytes and an opcode. A command without data
# is still five bytes: the radio counts them, not the meaning.
#
#   0x01  set frequency        0x03  read frequency
#   0x07  set mode             0x08  set mode? see below
#   0x08  PTT on               0x88  PTT off
#   0xF7  read TX status       -> one byte, bit 7 set while transmitting
#   0x87  set TX power
#
# 0x08 is PTT on. Mode is read with 0x0A on some models and never on
# others, so it is deliberately not offered: software that asks for it
# gets silence, which is what a radio that cannot answer does, rather
# than a number that happens to look plausible.
# ----------------------------------------------------------------------

YAESU_OP_SET_FREQ = 0x01
YAESU_OP_READ_FREQ = 0x03
YAESU_OP_SET_MODE = 0x07
YAESU_OP_SET_POWER = 0x87
YAESU_OP_PTT_ON = 0x08
YAESU_OP_PTT_OFF = 0x88
YAESU_OP_READ_TX = 0xF7

YAESU_MODE_TO_NIBBLE = {"lsb": 0x00, "usb": 0x01, "cw": 0x02, "fm": 0x04,
                        "am": 0x08, "fm-n": 0x0C}
YAESU_NIBBLE_TO_MODE = {value: key
                        for key, value in YAESU_MODE_TO_NIBBLE.items()}


def yaesu_bcd(value, digits=8):
    """Big-endian packed BCD, high nibble is the tens digit."""
    value = int(value)
    if value < 0 or value >= 10 ** digits:
        raise ValueError("%d does not fit in %d BCD digits" % (value, digits))
    out = bytearray()
    for shift in range(digits - 2, -2, -2):
        nibble = (value // (10 ** shift)) % 100
        out.append(((nibble // 10) << 4) | (nibble % 10))
    return bytes(out)


def yaesu_from_bcd(data):
    value = 0
    for byte in data:
        value = value * 100 + (byte >> 4) * 10 + (byte & 0x0F)
    return value


def yaesu_freq_unit(hz):
    """Hz if the field holds it, otherwise 10 Hz steps.

    Returns (unit, exact). ``exact`` is False when the frequency does not
    land on a step of the chosen unit.
    """
    hz = int(round(hz))
    if hz < 10 ** 8:
        return 1, True
    if hz // 10 < 10 ** 8:
        return 10, hz % 10 == 0
    raise ValueError("%d Hz does not fit the Yaesu frequency field" % hz)


def yaesu_freq_bytes(hz, unit=None):
    if unit is None:
        unit, _exact = yaesu_freq_unit(hz)
    return yaesu_bcd(int(round(hz / float(unit))), 8)


class Yaesu5(object):
    """An FT-817-shaped radio that is really an HRI-200."""

    name = "yaesu"
    label = "Yaesu 5-byte (FT-817/857)"

    def __init__(self, radio, verbose=False, unit=None):
        self.radio = radio
        self.verbose = verbose
        self.unit = unit        # None = decide per frequency
        self.buf = bytearray()
        self.mode = "fm"

    @staticmethod
    def looks_like(data):
        return len(data) >= 1 and all(
            byte < 0x20 or byte > 0x7E for byte in data)

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 5:
            frame = bytes(self.buf[:5])
            del self.buf[:5]
            reply = self.answer(frame)
            if self.verbose:
                print("    [yaesu] %-15s -> %s"
                      % (frame.hex(" ").upper(),
                         reply.hex(" ").upper() if reply else "-"))
            if reply:
                out.append(reply)
        return b"".join(out)

    def freq_hz(self):
        return int(round(self.radio.freq_mhz * 1e6))

    def answer(self, frame):
        opcode = frame[4]
        if opcode == YAESU_OP_READ_FREQ:
            return yaesu_freq_bytes(self.freq_hz(), self.unit) + bytes([opcode])
        if opcode == YAESU_OP_SET_FREQ:
            steps = yaesu_from_bcd(frame[:4])
            unit = self.unit or yaesu_freq_unit(self.freq_hz())[0]
            self.radio.set_freq(steps * unit / 1e6)
            return None
        if opcode == YAESU_OP_READ_TX:
            return bytes([0x80 if self.radio.tx else 0x00])
        if opcode == YAESU_OP_PTT_ON:
            self.radio.set_ptt(True)
            return None
        if opcode == YAESU_OP_PTT_OFF:
            self.radio.set_ptt(False)
            return None
        if opcode == YAESU_OP_SET_MODE:
            mode = YAESU_NIBBLE_TO_MODE.get(frame[3] & 0x0F)
            if mode:
                self.mode = mode
            return None
        if opcode == YAESU_OP_SET_POWER:
            level = frame[3]
            self.radio.set_power("high" if level > 170 else
                                 ("mid" if level > 80 else "low"))
            return None
        # Silence: a radio that does not know a command says nothing.
        return None

    def poll(self):
        return b""

    def close(self):
        self.radio.set_ptt(False)


# ----------------------------------------------------------------------
# Icom CI-V
#
# FE FE <to> <from> <command> <data...> FD
#
#   0x03        read band edge      -> reply with the current band
#   0x04        read frequency      -> 5 bytes BCD, lowest pair first
#   0x05        set frequency
#   0x06        set mode            -> 1 byte mode, 1 byte filter
#   0x15 0x02   read S-meter        -> 2 bytes BCD
#   0x1C 0x00   set TX state        -> 0x01 on, 0x00 off
#
# Frequencies go out least significant byte first, which is the opposite
# of Yaesu's order and a classic source of "the radio went to 5 MHz".
# ----------------------------------------------------------------------

CIV_PREAMBLE = b"\xfe\xfe"
CIV_EOM = b"\xfd"
CIV_CMD_READ_BAND = 0x03
CIV_CMD_READ_FREQ = 0x04
CIV_CMD_SET_FREQ = 0x05
CIV_CMD_SET_MODE = 0x06
CIV_CMD_READ_METER = 0x15
CIV_CMD_SET_TX = 0x1C
CIV_SUB_SMETER = 0x02
CIV_SUB_TX = 0x00

CIV_MODE_TO_BYTE = {"lsb": 0x00, "usb": 0x01, "am": 0x02, "cw": 0x03,
                    "fm": 0x05, "fm-n": 0x05}
CIV_BYTE_TO_MODE = {0x00: "lsb", 0x01: "usb", 0x02: "am", 0x03: "cw",
                    0x05: "fm"}


def civ_bcd_le(value, digits=10):
    value = int(value)
    if value < 0 or value >= 10 ** digits:
        raise ValueError("%d does not fit in %d BCD digits" % (value, digits))
    out = bytearray()
    for shift in range(0, digits, 2):
        nibble = (value // (10 ** shift)) % 100
        out.append(((nibble // 10) << 4) | (nibble % 10))
    return bytes(out)


def civ_from_bcd_le(data):
    value = 0
    for byte in reversed(data):
        value = value * 100 + (byte >> 4) * 10 + (byte & 0x0F)
    return value


def civ_frame(to, sender, command, data=b""):
    return (CIV_PREAMBLE + bytes([to, sender, command]) + bytes(data)
            + CIV_EOM)


def civ_parse_frames(data):
    """Split a stream into CI-V frames, ignoring anything incomplete."""
    frames = []
    pos = 0
    while True:
        start = data.find(CIV_PREAMBLE, pos)
        if start < 0:
            break
        end = data.find(CIV_EOM, start + 2)
        if end < 0:
            break
        frame = data[start + 2:end]
        if len(frame) >= 3:
            frames.append(frame)
        pos = end + 1
    return frames


class IcomCiv(object):
    """An IC-706-shaped radio that is really an HRI-200."""

    name = "civ"
    label = "Icom CI-V"

    def __init__(self, radio, verbose=False, address=0x88, controller=0xE0):
        self.radio = radio
        self.verbose = verbose
        self.address = address
        self.controller = controller
        self.buf = bytearray()
        self.mode = "fm"

    @staticmethod
    def looks_like(data):
        return data.lstrip(b"\x01\x04").startswith(b"\xfe")

    def feed(self, data):
        self.buf += data
        out = []
        # Anything before the first preamble is line noise; a CI-V radio
        # drops it rather than trying to make sense of it.
        start = self.buf.find(CIV_PREAMBLE)
        if start < 0:
            del self.buf[:]
            return b""
        del self.buf[:start]
        while True:
            if not self.buf.startswith(CIV_PREAMBLE):
                nxt = self.buf.find(b"\xfe")
                if nxt < 0:
                    del self.buf[:]
                    break
                del self.buf[:nxt]
            end = self.buf.find(CIV_EOM, 2)
            if end < 0:
                break
            frame = bytes(self.buf[2:end])
            del self.buf[:end + 1]
            if len(frame) < 3 or frame[0] != self.address:
                continue
            self.controller = frame[1]
            reply = self.answer(frame[1], frame[2], frame[3:])
            if self.verbose:
                print("    [civ] %-20s -> %s"
                      % (frame.hex(" ").upper(),
                         reply.hex(" ").upper() if reply else "-"))
            if reply:
                out.append(reply)
        return b"".join(out)

    def freq_hz(self):
        return int(round(self.radio.freq_mhz * 1e6))

    def answer(self, sender, command, payload):
        if command == CIV_CMD_READ_FREQ:
            return civ_frame(sender, self.address, command,
                             civ_bcd_le(self.freq_hz()))
        if command == CIV_CMD_SET_FREQ:
            if len(payload) >= 5:
                self.radio.set_freq(civ_from_bcd_le(payload[:5]) / 1e6)
            return None
        if command == CIV_CMD_SET_MODE:
            if payload:
                mode = CIV_BYTE_TO_MODE.get(payload[0])
                if mode:
                    self.mode = mode
                # The radio echoes the mode back, as CI-V requires.
                return civ_frame(sender, self.address, command,
                                 bytes([payload[0], 0x01]))
            return None
        if command == CIV_CMD_READ_METER and payload[:1] == bytes(
                [CIV_SUB_SMETER]):
            level = clamp_level(self.radio.smeter())
            return civ_frame(sender, self.address, command,
                             bytes([CIV_SUB_SMETER]) + civ_bcd_le(level, 4))
        if command == CIV_CMD_SET_TX:
            if payload[:1] == bytes([CIV_SUB_TX]) and len(payload) >= 2:
                self.radio.set_ptt(payload[1] == 0x01)
            elif payload[:1] == b"\x01":
                self.radio.set_ptt(False)
            return None
        if command == CIV_CMD_READ_BAND:
            return civ_frame(sender, self.address, command,
                             civ_bcd_le(self.freq_hz()))
        # A CI-V radio acknowledges what it cannot answer with FB.
        return civ_frame(sender, self.address, 0xFB, bytes([command]))

    def poll(self):
        return b""

    def close(self):
        self.radio.set_ptt(False)


# ----------------------------------------------------------------------
# Choosing one
# ----------------------------------------------------------------------

PROTOCOLS = {
    "kenwood": Kenwood,
    "yaesu": Yaesu5,
    "civ": IcomCiv,
}

# What ``--proto auto`` will report once it has made up its mind, so the
# operator can see what the software was taken to be.
AUTO_ORDER = ("civ", "yaesu", "kenwood")


def detect(data, verbose=False):
    """Guess the dialect from the first bytes that arrive, or None.

    Length matters: a single 0x01 could start anything, so nothing is
    decided until there is enough to decide on - five bytes for Yaesu,
    a preamble for CI-V, a letter for Kenwood.
    """
    if not data:
        return None
    if b"\xfe\xfe" in data:
        return "civ"
    if len(data) >= 5 and Yaesu5.looks_like(data):
        return "yaesu"
    if any(0x41 <= byte <= 0x5A or 0x61 <= byte <= 0x7A for byte in data):
        return "kenwood"
    return None


class Auto(object):
    """Answers in whichever dialect the software turns out to speak.

    Until something decisive arrives nothing is sent: a wrong guess means
    a stream of bytes the other end will not understand, and silence
    costs one timeout and a retry, which every CAT client survives.
    """

    name = "auto"
    label = "auto-detect"

    def __init__(self, radio, verbose=False, **kwargs):
        self.radio = radio
        self.verbose = verbose
        self.kwargs = kwargs
        self.delegate = None
        self.pending = bytearray()

    def feed(self, data):
        if self.delegate is not None:
            return self.delegate.feed(data)
        self.pending += data
        choice = detect(bytes(self.pending))
        if choice is None:
            # Give up on sniffing after a while rather than staying mute
            # for ever; Kenwood is the dialect most software speaks.
            if len(self.pending) < 16:
                return b""
            choice = "kenwood"
        self.delegate = make(choice, self.radio, self.verbose, **self.kwargs)
        if self.verbose:
            print("    [auto] the software speaks %s" % self.delegate.label)
        buffered, self.pending = bytes(self.pending), bytearray()
        return self.delegate.feed(buffered)

    def poll(self):
        return self.delegate.poll() if self.delegate is not None else b""

    def close(self):
        self.radio.set_ptt(False)


def make(name, radio, verbose=False, **kwargs):
    """Build a protocol by name. Raises ValueError on an unknown one."""
    name = (name or "auto").lower()
    if name == "auto":
        return Auto(radio, verbose=verbose, **kwargs)
    if name not in PROTOCOLS:
        raise ValueError("unknown protocol %r, pick one of %s"
                         % (name, ", ".join(sorted(list(PROTOCOLS) + ["auto"]))))
    cls = PROTOCOLS[name]
    if cls is IcomCiv:
        return cls(radio, verbose=verbose,
                   address=kwargs.get("address", 0x88),
                   controller=kwargs.get("controller", 0xE0))
    if cls is Yaesu5:
        return cls(radio, verbose=verbose, unit=kwargs.get("unit"))
    return cls(radio, verbose=verbose)
