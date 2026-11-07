#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hri200_radio.py - one interface, many radios

The HRI-200 is only one of several ways to put a radio behind a sound
card. This module hides which one is in use behind a single object, so
the CAT bridge and the GUI do not care whether the frequency goes to a
WIRES-X box, to a Yaesu 5-byte CAT port, to a Kenwood ASCII port or over
Icom's CI-V bus.

    Radio
     +-- Hri200Radio   the WIRES-X box, protocol from hri200_cat.py
     +-- YaesuCat      FT-817/857/897/818 style, 5-byte binary frames
     +-- KenwoodCat    TS-2000 style ASCII, "FA00014485000;"
     +-- IcomCiv       CI-V, FE FE <addr> <cmd> ... FD

All of them answer the same questions:

    open() / close()
    apply()                 push frequency, mode, tone, power
    set_ptt(bool)           key or unkey
    set_freq(mhz)           retune
    set_power(step)         high / mid / low, where the radio has it
    tx                      is the radio transmitting right now
    smeter                  0..30, the TS-2000 bar scale

Two deliberate choices:

* ``smeter`` is a number on the TS-2000 scale even for radios that
  report something else. Radios without a meter at all return 0, which
  the bridge reports as S0 rather than inventing a signal.

* Nothing is transmitted to find out whether a radio is there. Every
  backend opens its port, sends what it was told to send and then only
  listens. A wrong baud rate therefore costs a timeout, not a carrier.

The frame builders are pure functions so they can be checked without
hardware; see tests/test_radio_frames.py.
"""

import sys

if sys.version_info < (3, 6):
    sys.exit("Python 3.6 or newer required")

import threading
import time

try:
    import serial
except ImportError:
    serial = None


POWER = ("high", "mid", "low")


# ----------------------------------------------------------------------
# Yaesu 5-byte CAT
#
# Every command is five bytes: four BCD data bytes and one opcode. The
# radio answers with the same opcode and the current value.
#
#   0x01  set frequency        0x03  read frequency
#   0x07  set mode             0x08  read mode
#   0x87  set TX power         0x88  read TX power
#   0x08  PTT on   (opcode only, data 0x00)
#   0x88  PTT off
#   0xF7  read TX status -> one byte, bit 7 set while transmitting
#
# Mode lives in the high nibble: 0x00 LSB, 0x01 USB, 0x02 CW, 0x04 FM,
# 0x08 AM, 0x0A data-LSB, 0x0C FM-N.
#
# This is the dialect of the FT-817/818/857/897 family, which is what a
# small FM set with a data port is most likely to speak. It is *not*
# the ASCII CAT of the FT-991/FTDX line; those are Kenwood-like and
# use KenwoodCat.
# ----------------------------------------------------------------------

YAESU_OP_SET_FREQ = 0x01
YAESU_OP_READ_FREQ = 0x03
YAESU_OP_SET_MODE = 0x07
YAESU_OP_READ_MODE = 0x08
YAESU_OP_SET_POWER = 0x87
YAESU_OP_READ_POWER = 0x88
YAESU_OP_TX_ON = 0x08
YAESU_OP_TX_OFF = 0x88
YAESU_OP_READ_TX = 0xF7

YAESU_MODE = {"lsb": 0x00, "usb": 0x01, "cw": 0x02, "fm": 0x04,
              "am": 0x08, "data-lsb": 0x0A, "fm-n": 0x0C}


def yaesu_bcd(value, digits=8):
    """Encode an integer as big-endian packed BCD of ``digits`` nibbles."""
    if digits % 2:
        raise ValueError("BCD needs an even number of digits")
    value = int(value)
    if value < 0 or value >= 10 ** digits:
        raise ValueError("%d does not fit in %d BCD digits" % (value, digits))
    out = bytearray()
    for shift in range(digits - 2, -2, -2):
        nibble = (value // (10 ** shift)) % 100
        out.append(((nibble // 10) << 4) | (nibble % 10))
    return bytes(out)


# The 5-byte dialect carries only four BCD bytes, so eight digits, and
# the unit is what the radio decides it is. The manuals of the FT-817
# family document Hz, which covers 0..99.999999 MHz and therefore cannot
# express a 2 m frequency at all. Radios in the field do reach 145 MHz
# over this protocol, so above 99.999999 MHz the field is in 10 Hz
# steps. Both are offered as an explicit unit rather than guessed at
# silently: unit=1 is Hz, unit=10 is 10 Hz steps.

def yaesu_freq_frame(hz, unit=1):
    """0x01 + 8 BCD digits of the frequency, in ``unit`` Hz steps."""
    if unit not in (1, 10, 100):
        raise ValueError("unit must be 1, 10 or 100 Hz")
    steps = int(round(hz / float(unit)))
    if steps >= 10 ** 8:
        raise ValueError(
            "%d Hz does not fit four BCD bytes in %d Hz steps; use a "
            "coarser unit or a radio with a longer frequency field"
            % (hz, unit))
    return yaesu_bcd(steps, 8) + bytes([YAESU_OP_SET_FREQ])


def yaesu_pick_unit(hz):
    """Smallest faithful unit for a frequency: Hz if it fits, else 10 Hz.

    Returns (unit, exact). ``exact`` is False when the frequency does
    not land on a step of the chosen unit, in which case the radio will
    be tuned to something a few Hz away - which for FM does not matter,
    but the caller deserves to be told.
    """
    hz = int(round(hz))
    if hz < 10 ** 8:
        return 1, True
    if hz % 10 == 0 and hz // 10 < 10 ** 8:
        return 10, True
    if hz // 10 < 10 ** 8:
        return 10, False
    raise ValueError("%d Hz does not fit the Yaesu frequency field" % hz)


def yaesu_read_freq_frame():
    return b"\x00\x00\x00\x00" + bytes([YAESU_OP_READ_FREQ])


def yaesu_mode_frame(mode):
    if mode not in YAESU_MODE:
        raise ValueError("unknown Yaesu mode: %r" % mode)
    return b"\x00\x00\x00" + bytes([YAESU_MODE[mode] << 4,
                                    YAESU_OP_SET_MODE])


def yaesu_power_frame(level):
    """``level`` is 0..255, the radio's own scale; 255 is full."""
    if not 0 <= level <= 255:
        raise ValueError("power level out of range: %r" % level)
    return b"\x00\x00\x00" + bytes([level, YAESU_OP_SET_POWER])


def yaesu_ptt_frame(on):
    return b"\x00\x00\x00\x00" + bytes([YAESU_OP_TX_ON if on
                                        else YAESU_OP_TX_OFF])


def yaesu_read_tx_frame():
    return b"\x00\x00\x00\x00" + bytes([YAESU_OP_READ_TX])


YAESU_POWER_STEP = {"high": 255, "mid": 128, "low": 40}


# ----------------------------------------------------------------------
# Kenwood ASCII CAT
#
# "FA" + 11 digits + ";"  frequency in Hz
# "MD" + 1 digit + ";"    mode, 4 = FM, 5 = AM, 1 = LSB, 2 = USB
# "PC" + 3 digits + ";"   power in watts
# "TX;" / "RX;"           PTT
# "SM0" + 4 digits + ";"  S-meter, 0000..0030
# "IF;"                   the 38-character status block
#
# The TS-2000 and the later Yaesu FTDX/FT-991 line both answer to this,
# which is why the bridge emulates it on the other side as well.
# ----------------------------------------------------------------------

KENWOOD_MODE = {"lsb": 1, "usb": 2, "cw": 3, "fm": 4, "am": 5, "fm-n": 4}


def kenwood_freq_cmd(hz):
    if not 0 <= int(hz) <= 99999999999:
        raise ValueError("frequency out of range: %r" % hz)
    return "FA%011d;" % int(hz)


def kenwood_mode_cmd(mode):
    if mode not in KENWOOD_MODE:
        raise ValueError("unknown Kenwood mode: %r" % mode)
    return "MD%d;" % KENWOOD_MODE[mode]


def kenwood_power_cmd(watts):
    return "PC%03d;" % max(0, min(999, int(watts)))


def kenwood_ptt_cmd(on):
    return "TX;" if on else "RX;"


def kenwood_smeter_cmd():
    return "SM0;"


KENWOOD_WATT = {"high": 50, "mid": 20, "low": 5}


# ----------------------------------------------------------------------
# Icom CI-V
#
# FE FE <to> <from> <command> <data...> FD
#
#   0x03  read band edge
#   0x04  read operating frequency
#   0x05  set operating frequency   (5 bytes BCD, little endian)
#   0x06  set mode                  (1 byte mode, 1 byte filter)
#   0x15  read meter                sub 0x02 = S-meter
#   0x1C  set TX state              sub 0x00 on, 0x01 off
#
# Frequencies are packed BCD with the least significant byte first,
# which is the opposite of Yaesu's order and a classic source of
# "the radio went to 5 MHz" bugs.
# ----------------------------------------------------------------------

CIV_PREAMBLE = b"\xfe\xfe"
CIV_EOM = b"\xfd"
CIV_CMD_SET_FREQ = 0x05
CIV_CMD_SET_MODE = 0x06
CIV_CMD_READ_METER = 0x15
CIV_CMD_SET_TX = 0x1C
CIV_SUB_SMETER = 0x02
CIV_SUB_TX = 0x00

CIV_MODE = {"lsb": 0x00, "usb": 0x01, "am": 0x02, "cw": 0x03, "fm": 0x05}


def civ_bcd_le(value, digits=10):
    """Packed BCD, least significant digit pair in the first byte.

    Icom sends the frequency as BCD with the lowest pair first, and
    each byte carries its two digits the normal way round (high nibble
    is the tens digit). 144.850000 MHz is 01 44 85 00 00 read from the
    left, so the wire carries 00 00 85 44 01.
    """
    value = int(value)
    if value < 0 or value >= 10 ** digits:
        raise ValueError("%d does not fit in %d BCD digits" % (value, digits))
    out = bytearray()
    for shift in range(0, digits, 2):
        nibble = (value // (10 ** shift)) % 100
        out.append(((nibble // 10) << 4) | (nibble % 10))
    return bytes(out)


def civ_frame(address, command, data=b"", sender=0xE0):
    return (CIV_PREAMBLE + bytes([address, sender, command]) + bytes(data)
            + CIV_EOM)


def civ_freq_frame(address, hz):
    return civ_frame(address, CIV_CMD_SET_FREQ, civ_bcd_le(hz, 10))


def civ_mode_frame(address, mode, filt=0x01):
    if mode not in CIV_MODE:
        raise ValueError("unknown CI-V mode: %r" % mode)
    return civ_frame(address, CIV_CMD_SET_MODE,
                     bytes([CIV_MODE[mode], filt]))


def civ_ptt_frame(address, on):
    return civ_frame(address, CIV_CMD_SET_TX,
                     bytes([CIV_SUB_TX, 0x01 if on else 0x00]))


def civ_smeter_frame(address):
    return civ_frame(address, CIV_CMD_READ_METER, bytes([CIV_SUB_SMETER]))


# ----------------------------------------------------------------------
# Shared serial plumbing
# ----------------------------------------------------------------------

class RadioError(Exception):
    """Anything the radio or its port did wrong."""


class Radio(object):
    """What every backend has to answer."""

    name = "radio"
    has_power = True
    has_meter = True

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.lock = threading.RLock()
        self.tx = False
        self.freq_mhz = 0.0
        self.shift_mhz = 0.0
        self.power = "mid"
        self.tone_hz = 88.5
        self.sql = "none"
        self.narrow = False
        self.mode = "fm"
        self._rx_level = 0
        self._running = False

    # -- to be implemented ---------------------------------------------
    def open(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def apply(self):
        """Push the whole channel configuration to the radio."""
        raise NotImplementedError

    def set_ptt(self, on):
        raise NotImplementedError

    # -- shared -------------------------------------------------------
    def set_freq(self, freq_mhz):
        old = self.freq_mhz
        self.freq_mhz = float(freq_mhz)
        try:
            self.apply()
        except RadioError:
            self.freq_mhz = old
            raise
        return True

    def set_power(self, step):
        if step not in POWER:
            raise ValueError("power must be one of %s" % (POWER,))
        old = self.power
        self.power = step
        try:
            self.apply()
        except RadioError:
            self.power = old
            raise
        return True

    def set_narrow(self, narrow):
        self.narrow = bool(narrow)
        self.mode = "fm-n" if self.narrow else "fm"
        return self.apply()

    @property
    def tx(self):
        return self._tx

    @tx.setter
    def tx(self, value):
        self._tx = bool(value)

    @property
    def rx_level(self):
        return self._rx_level

    def smeter(self):
        """Return the meter on the TS-2000 scale 0..30."""
        return max(0, min(30, self._rx_level))

    def log(self, text):
        if self.verbose:
            print("    [%s] %s" % (self.name, text))

    def note(self, text):
        """Say something the operator should see, not just the log."""
        print("  %s: %s" % (self.name, text))


class SerialRadio(Radio):
    """A radio behind one serial port, polled by a background thread."""

    baudrate = 38400
    poll_interval = 1.0

    def __init__(self, port, verbose=False, baudrate=None):
        Radio.__init__(self, verbose)
        self.port = port
        if baudrate:
            self.baudrate = baudrate
        self.ser = None
        self.buf = b""
        self._poll_thread = None

    # -- port ---------------------------------------------------------
    def _open_port(self, **extra):
        if serial is None:
            raise RadioError("pyserial missing:  pip install pyserial")
        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = self.baudrate
        ser.bytesize = 8
        ser.parity = "N"
        ser.stopbits = 1
        ser.timeout = 0.05
        # A write timeout turns a stalled consumer into an exception
        # instead of a thread that never comes back.
        ser.write_timeout = 1.0
        ser.rtscts = False
        ser.xonxoff = False
        for key, value in extra.items():
            setattr(ser, key, value)
        try:
            ser.open()
        except Exception as exc:
            raise RadioError("%s: %s" % (self.port, exc))
        return ser

    def _write(self, data):
        if self.ser is None:
            raise RadioError("port is not open")
        self.log("-> %s" % data.hex().upper())
        try:
            # write() is called in blocking mode with a timeout, so it
            # has already handed every byte over when it returns. An
            # extra flush() would wait for the line to drain, which
            # never ends if the far end stops reading.
            self.ser.write(data)
        except Exception as exc:
            raise RadioError("write failed: %s" % exc)

    def _read(self, count=64, seconds=0.3, quiet=0.06):
        """Collect everything the radio says until it goes quiet.

        Stopping as soon as ``count`` bytes arrived looks tidier and is
        wrong: a radio answers several buffered commands in one go, and
        a count limit then cuts the stream mid-frame and leaves the rest
        for the *next* poll. Draining until nothing has arrived for
        ``quiet`` keeps each poll a self-contained conversation.
        """
        if self.ser is None:
            raise RadioError("port is not open")
        end = time.time() + seconds
        out = b""
        last = time.time()
        while time.time() < end:
            try:
                chunk = self.ser.read(count)
            except Exception as exc:
                raise RadioError("read failed: %s" % exc)
            if chunk:
                out += chunk
                last = time.time()
            elif out and (time.time() - last) >= quiet:
                break
        if out:
            self.log("<- %s" % out.hex().upper())
        return out

    def _flush_input(self):
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    # -- lifecycle ----------------------------------------------------
    def open(self):
        self.ser = self._open_port()
        self.open_hook()
        self.apply()
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll, daemon=True)
        self._poll_thread.start()
        return self

    def open_hook(self):
        """Backend-specific setup after the port is open."""

    def close(self):
        self._running = False
        if self._poll_thread:
            try:
                self._poll_thread.join(timeout=2.0)
            except Exception:
                pass
        try:
            if self.ser is not None and self.tx:
                self.set_ptt(False)
        finally:
            if self.ser is not None:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None

    def _poll(self):
        while self._running:
            try:
                with self.lock:
                    self.poll_once()
            except RadioError as exc:
                self.log("poll: %s" % exc)
            except Exception as exc:                      # pragma: no cover
                self.log("poll: %r" % exc)
            time.sleep(self.poll_interval)

    def poll_once(self):
        """Read the meter and the TX state; overridden per backend."""

    # -- PTT ----------------------------------------------------------
    def set_ptt(self, on):
        on = bool(on)
        with self.lock:
            if on == self.tx:
                return
            self._write(self.ptt_frame(on))
            self.tx = on

    def ptt_frame(self, on):
        raise NotImplementedError

    def set_freq(self, freq_mhz):
        old = self.freq_mhz
        self.freq_mhz = float(freq_mhz)
        try:
            with self.lock:
                self._write(self.freq_frame())
        except RadioError:
            self.freq_mhz = old
            raise
        return True

    def freq_frame(self):
        raise NotImplementedError


# ----------------------------------------------------------------------
# Yaesu 5-byte CAT backend
# ----------------------------------------------------------------------

class YaesuCat(SerialRadio):
    """FT-817/818/857/897 over their 5-byte CAT port.

    The radio is polled with 0xF7 every second, which is how the bridge
    learns about a PTT that was pressed on the radio itself. Polling is
    read-only: nothing is retuned unless the operator asked for it.
    """

    name = "Yaesu CAT"
    baudrate = 9600

    def __init__(self, port, verbose=False, baudrate=None, power=True,
                 unit=None):
        SerialRadio.__init__(self, port, verbose, baudrate)
        self.has_power = power
        self.mode = "fm"
        self.unit = unit          # None = pick per frequency
        self._unit_warned = False

    def freq_frame(self):
        hz = int(round(self.freq_mhz * 1e6))
        if self.unit is None:
            unit, exact = yaesu_pick_unit(hz)
            if not exact and not self._unit_warned:
                self._unit_warned = True
                self.note("frequency rounded to the nearest 10 Hz, "
                          "the field has no room for the last digit")
        else:
            unit = self.unit
        return yaesu_freq_frame(hz, unit)

    def ptt_frame(self, on):
        return yaesu_ptt_frame(on)

    def open_hook(self):
        self._flush_input()

    def apply(self):
        with self.lock:
            self._write(self.freq_frame())
            self._read(1, 0.2)
            self._write(yaesu_mode_frame(self.mode))
            self._read(1, 0.2)
            if self.has_power:
                self._write(yaesu_power_frame(YAESU_POWER_STEP[self.power]))
                self._read(1, 0.2)

    def poll_once(self):
        self._write(yaesu_read_tx_frame())
        answer = self._read(1, 0.25)
        if answer:
            self.tx = bool(answer[0] & 0x80)
        # The 817 family has no CAT-readable S-meter; the bridge reports
        # S0 rather than a made-up number.
        self._rx_level = 0


# ----------------------------------------------------------------------
# Kenwood ASCII backend
# ----------------------------------------------------------------------

class KenwoodCat(SerialRadio):
    """TS-2000 and the Yaesu FTDX/FT-991 line, which share the dialect."""

    name = "Kenwood CAT"
    baudrate = 38400

    def __init__(self, port, verbose=False, baudrate=None):
        SerialRadio.__init__(self, port, verbose, baudrate)
        self.mode = "fm"

    def freq_frame(self):
        return kenwood_freq_cmd(int(round(self.freq_mhz * 1e6))).encode()

    def ptt_frame(self, on):
        return kenwood_ptt_cmd(on).encode()

    def open_hook(self):
        self._flush_input()
        # Two newlines are the usual "wake up" for a Kenwood port; the
        # radio answers with "?;" if it was not expecting anything, and
        # both are ignored.
        self._write(b"\r\n")
        self._read(2, 0.2)

    def apply(self):
        with self.lock:
            for cmd in (self.freq_frame(),
                        kenwood_mode_cmd(self.mode).encode(),
                        kenwood_power_cmd(KENWOOD_WATT[self.power]).encode()):
                self._write(cmd)
                self._read(64, 0.25)
            # Whatever the radio said about the three commands above
            # belongs to them, not to the next poll.
            self._read(64, 0.2)

    def poll_once(self):
        self._write(kenwood_smeter_cmd().encode())
        answer = self._read(12, 0.25)
        for part in answer.split(b";"):
            part = part.strip()
            if part.startswith(b"SM") and len(part) >= 7:
                try:
                    self._rx_level = min(30, int(part[3:7]))
                except ValueError:
                    pass


# ----------------------------------------------------------------------
# Icom CI-V backend
# ----------------------------------------------------------------------

class IcomCiv(SerialRadio):
    """CI-V, as used by the IC-706/7100/7300/9700 and most other Icoms."""

    name = "Icom CI-V"
    baudrate = 19200

    def __init__(self, port, address=0x88, verbose=False, baudrate=None):
        SerialRadio.__init__(self, port, verbose, baudrate)
        self.address = address
        self.mode = "fm"

    def freq_frame(self):
        return civ_freq_frame(self.address, int(round(self.freq_mhz * 1e6)))

    def ptt_frame(self, on):
        return civ_ptt_frame(self.address, on)

    def open_hook(self):
        self._flush_input()

    def apply(self):
        with self.lock:
            self._write(self.freq_frame())
            self._read(16, 0.2)
            self._write(civ_mode_frame(self.address, self.mode))
            self._read(16, 0.2)

    def poll_once(self):
        self._write(civ_smeter_frame(self.address))
        answer = self._read(64, 0.3)
        for command, payload in civ_replies(answer, self.address):
            if command == CIV_CMD_READ_METER and len(payload) >= 3:
                self._rx_level = civ_smeter_to_scale(payload[1:3])


def civ_replies(data, radio_address=None):
    """Every complete CI-V frame in ``data``, as (command, payload)."""
    out = []
    pos = 0
    while True:
        start = data.find(CIV_PREAMBLE, pos)
        if start < 0:
            break
        end = data.find(CIV_EOM, start)
        if end < 0:
            break
        frame = data[start + 2:end]
        if len(frame) >= 3:
            if radio_address is None or frame[1] == radio_address:
                out.append((frame[2], frame[3:]))
        pos = end + 1
    return out


def parse_civ_reply(data, radio_address=None):
    """Pick the last complete CI-V frame out of ``data``.

    Returns (command, payload) or None. A frame is
    FE FE <to> <from> <cmd> <payload> FD, so anything shorter than six
    bytes is dropped rather than guessed at. ``radio_address`` filters
    on the <from> byte, which is the radio's own address; passing None
    accepts a frame from anybody on the bus.
    """
    start = data.rfind(CIV_PREAMBLE)
    while start >= 0:
        end = data.find(CIV_EOM, start)
        if end < 0:
            break
        frame = data[start + 2:end]
        if len(frame) >= 3:
            if radio_address is None or frame[1] == radio_address:
                return frame[2], frame[3:]
        start = data.rfind(CIV_PREAMBLE, 0, start)
    return None


def civ_smeter_to_scale(pair):
    """Two BCD bytes, 0000..0255, onto the 0..30 bar scale.

    The Icom value counts from S0 in steps of the radio's own S-unit;
    dividing by 8 maps the usual 0..255 range onto the bar without
    claiming more precision than a 3-digit BCD number has.
    """
    out = 0
    for byte in pair:
        out = out * 100 + (byte >> 4) * 10 + (byte & 0x0F)
    return max(0, min(30, out // 8))

# ----------------------------------------------------------------------
# HRI-200 backend
# ----------------------------------------------------------------------

class Hri200Radio(Radio):
    """The WIRES-X box, driven through hri200_cat.HRI200.

    Imported lazily so that this module stays importable without
    pyserial, which keeps the frame builders testable anywhere.
    """

    name = "HRI-200"

    def __init__(self, port, verbose=False, freq_mhz=144.85):
        Radio.__init__(self, verbose)
        self.port = port
        self.freq_mhz = freq_mhz
        self.hri = None

    def open(self):
        import hri200_cat
        self.hri = hri200_cat.HRI200(
            self.port, self.freq_mhz, self.shift_mhz,
            verbose=self.verbose, power=self.power,
            tone_hz=self.tone_hz, sql=self.sql, narrow=self.narrow)
        self.hri.open()
        return self

    def close(self):
        if self.hri is not None:
            self.hri.set_ptt(False)
            time.sleep(0.2)
            self.hri.close()
            self.hri = None

    def apply(self):
        if self.hri is None:
            raise RadioError("not open")
        self.hri.freq_mhz = self.freq_mhz
        self.hri.shift_mhz = self.shift_mhz
        self.hri.power = self.power
        self.hri.tone_hz = self.tone_hz
        self.hri.sql = self.sql
        self.hri.narrow = self.narrow
        self.hri._apply("channel %.5f MHz" % self.freq_mhz)

    def set_ptt(self, on):
        if self.hri is None:
            raise RadioError("not open")
        self.hri.set_ptt(on)
        self.tx = on

    @property
    def tx(self):
        if self.hri is not None:
            return self.hri.tx
        return self._tx

    @tx.setter
    def tx(self, value):
        self._tx = bool(value)

    @property
    def rx_level(self):
        if self.hri is not None:
            return self.hri.rx_level * 7
        return self._rx_level

    def smeter(self):
        return max(0, min(30, self.rx_level))


# ----------------------------------------------------------------------
# Adapter for the CAT bridge
# ----------------------------------------------------------------------

class CatHandle(object):
    """The HRI-200-shaped handle hri200_cat.TS2000 expects.

    TS2000 was written against a HRI-200 and reaches for ``freq_mhz``,
    ``power``, ``rx_level``, ``tx``, ``set_freq``, ``set_power`` and
    ``set_ptt``. A generic backend offers a slightly different set, so
    this adapts one to the other instead of teaching TS2000 two shapes.

    ``rx_level`` is scaled back to the 0..4 the HRI-200 reports, because
    that is what TS2000 multiplies by seven to fill the SM command. The
    generic backend already speaks the 0..30 scale, so the round trip
    through 0..4 loses a little resolution and gains nothing - but it
    keeps the two paths identical from TS2000's point of view, which
    matters more than a bar segment.
    """

    def __init__(self, radio):
        self.radio = radio

    def __getattr__(self, name):
        if name == "rx_level":
            return max(0, min(4, self.radio.smeter() // 7))
        return getattr(self.radio, name)

    @property
    def freq_mhz(self):
        return self.radio.freq_mhz

    @freq_mhz.setter
    def freq_mhz(self, value):
        self.radio.freq_mhz = value

    @property
    def tx(self):
        return self.radio.tx

    def set_freq(self, freq_mhz):
        return self.radio.set_freq(freq_mhz)

    def set_power(self, step):
        return self.radio.set_power(step)

    def set_ptt(self, on):
        return self.radio.set_ptt(on)


# ----------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------

BACKENDS = {
    "hri200": Hri200Radio,
    "yaesu": YaesuCat,
    "kenwood": KenwoodCat,
    "icom": IcomCiv,
}


def create_radio(kind, port, verbose=False, baudrate=None, address=0x88,
                 freq_mhz=144.85):
    """Build a backend by name. Unknown names raise ValueError."""
    kind = (kind or "hri200").lower()
    if kind not in BACKENDS:
        raise ValueError("unknown radio %r, pick one of %s"
                         % (kind, ", ".join(sorted(BACKENDS))))
    if kind == "hri200":
        return Hri200Radio(port, verbose=verbose, freq_mhz=freq_mhz)
    if kind == "icom":
        return IcomCiv(port, address=address, verbose=verbose,
                       baudrate=baudrate)
    return BACKENDS[kind](port, verbose=verbose, baudrate=baudrate)


def list_serial_ports():
    """Every serial port the system admits to having, sorted."""
    if serial is None:
        return []
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out = []
    for info in list_ports.comports():
        out.append((info.device, info.description or ""))
    return sorted(out)
