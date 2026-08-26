#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hri200_cat.py - use a Yaesu HRI-200 as a generic audio + PTT interface

The HRI-200 is Yaesu's WIRES-X interface box. It contains a USB sound
card and a serial control channel, but it only speaks Yaesu's own
protocol and is useless outside the WIRES-X software as shipped.

This script talks that protocol on one side and emulates a Kenwood
TS-2000 on the other, so the box can be driven by flrig, fldigi,
WSJT-X, Direwolf or anything else that speaks CAT.

    fldigi / WSJT-X -> flrig -> COM10 =[com0com]= COM11 -> this script
    -> COM7 -> HRI-200 -> radio

    In flrig: Rig = TS-2000, Port = COM10, baud rate irrelevant.

Audio is not touched by this script: the HRI-200 registers as a
standard USB audio device, so select it directly in your application.

Single file, pyserial is the only dependency.

    py -3 hri200_cat.py COM11
    py -3 hri200_cat.py COM11 --hri COM7 --freq 144.85 --power low -v


PROTOCOL
--------
38400 baud, 8N1, no flow control, DTR and RTS asserted.

Framing: 0x01 (SOH) + ASCII command + 0x04 (EOT), both directions.
Without the leading SOH the box discards the frame silently.

The device never transmits unsolicited - every reply follows a
command by roughly 30 ms.

Cold start, in this order:

    open port, assert DTR/RTS, flush
    wait ~7 s        the original software waits 6.90 / 7.08 s;
                     commands sent earlier are ignored silently
    M00           -> M00
    R6423         -> R<hex>     device identity, ASCII-hex CSV
    P010000       -> B0 0    0000000
    D1V0000       -> D1V0030<radio type and firmware>
    D1M<...>      -> echo       channel configuration
    D1B00010      -> D1B00010

Operation:

    P100000       PTT on        repeat at 1 Hz while the state holds
    P010000       PTT off
    D1C0000       status     -> D1C000B0 XXYY FD2wr0
                              YY ending in "5" means transmitting,
                              "00" means receiving; XX is an RX level
    P010010       shut down

Everything above was reconstructed from serial captures. Fields whose
meaning could not be established are marked as such in the comments.


LIMITATIONS
-----------
The only signal-strength information is the XX field of the D1C reply,
observed range 0..4 per digit. It is a coarse four-step indicator, not
an S-meter; the mapping onto the TS-2000 scale is arbitrary. Power
output, SWR and ALC are not available at all.

Whether D1M actually retunes the radio, as opposed to merely telling
the interface which frequency is in use, has not been verified.
"""

import sys

if sys.version_info < (3, 6):
    sys.exit("Python 3.6 or newer required")

import argparse
import threading
import time

try:
    import serial
except ImportError:
    # Importable without pyserial so that other modules can reuse the
    # frame builders and their dry-run paths.
    serial = None

SOH = b"\x01"
EOT = b"\x04"

MODE_FM = "4000"
MODE_DIGITAL = "7000"

PTT_ON = "P100000"
PTT_OFF = "P010000"
STATUS = "D1C0000"
SHUTDOWN = "P010010"
WARMUP = 8.0


# ----------------------------------------------------------------------
# D1M - frequency and channel command
#
# Layout:  D1M <length, 4 hex digits> 4000 <RX block, 32> <TX block, 32> F
#
# Block (32 characters):
#   [ 0: 9]  frequency     "144.85000"
#   [ 9:19]  offset        "+000.00000"; "-" means reverse
#   [19]     narrow        0 = wide, 1 = narrow
#   [20]     squelch       1 = none, 2 = CTCSS, 3 = DCS
#   [21:24]  tone          "077" = 77.0 Hz, "088" = 88.5, "254" = 254.1
#   [24:27]  DCS code      "023", "754"; retained even when DCS is off
#   [27:30]  "000"
#   [30]     RX block: power  0 = high, 1 = mid, 2 = low
#            TX block: constant "2", meaning unknown
#   [31]     "0"
#
# All of the above is backed by capture comparisons: frequency, power,
# narrow, CTCSS up to 254.1 Hz, DCS with two codes, reverse and a
# 1 MHz offset. What remains unclear is position [30] of the TX block,
# and whether the DCS code and the CTCSS tone can be set independently
# - they were never changed at the same time.
# ----------------------------------------------------------------------

POWER = {"high": "0", "mid": "1", "low": "2"}
SQL = {"none": "1", "tone": "2", "dcs": "3"}

# Standard CTCSS ladder; the first three digits are what gets encoded.
CTCSS = [67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
         94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0,
         127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 162.2, 167.9,
         173.8, 179.9, 186.2, 192.8, 203.5, 210.7, 218.1, 225.7, 233.6,
         241.8, 250.3, 254.1]


def tone_code(hz):
    """Encode a CTCSS frequency into the three-digit field."""
    if hz not in CTCSS:
        raise ValueError("%.1f Hz is not a standard CTCSS tone" % hz)
    return "%03d" % int(hz)


def block(freq_mhz, shift_mhz, narrow, sql, tone_hz, dcs, last,
          reverse=False, dgid=0, tail="0"):
    """Build one 32-character channel block.

    last: the power digit in the RX block, constant "2" in the TX
    block. Power was only ever observed to change in the RX block.
    dgid: DG-ID, carried in the RX block only in the one capture that
    shows it change; 99 appeared as "063", which is hex.
    tail: position 31, "1" in the TX block while the node runs digital.
    """
    if not 0 <= dgid <= 99:
        raise ValueError("DG-ID out of range: %r" % dgid)
    f = "%9.5f" % freq_mhz
    if len(f) != 9:
        raise ValueError("frequency does not fit the field: %r" % f)
    sign = "-" if (reverse or shift_mhz < 0) else "+"
    sh = "%s%09.5f" % (sign, abs(shift_mhz))
    if len(sh) != 10:
        raise ValueError("offset does not fit the field: %r" % sh)
    return (f + sh
            + ("1" if narrow else "0")
            + SQL[sql]
            + tone_code(tone_hz)
            + dcs
            + "%03X" % dgid
            + last
            + tail)


def d1m(freq_mhz, shift_mhz=0.0, power="mid", tone_hz=88.5,
        sql="none", narrow=False, reverse=False,
        dcs_rx="023", dcs_tx="754", tx_freq_mhz=None, raw=None,
        digital=False, dgid=0, tone_tx_hz=None):
    """Build a D1M command.

    sql:            "none", "tone" (CTCSS) or "dcs"
    dcs_rx/dcs_tx:  DCS code per block, retained even when sql != "dcs"
    reverse:        force a negative offset sign
    raw:            replace the entire body
    digital:        C4FM instead of FM; sets the mode field to 7000 and
                    position 31 of the TX block to 1, which moved
                    together in the one capture that shows the switch
    dgid:           DG-ID 0..99, RX block only
    tone_tx_hz:     CTCSS of the TX block; the captures show it differing
                    from the RX block, so it defaults to tone_hz but can
                    be set apart
    """
    if raw is not None:
        body = raw
    else:
        if tx_freq_mhz is None:
            tx_freq_mhz = freq_mhz
        body = ((MODE_DIGITAL if digital else MODE_FM)
                + block(freq_mhz, shift_mhz, narrow, sql, tone_hz,
                        dcs_rx, POWER[power], reverse, dgid=dgid)
                + block(tx_freq_mhz, shift_mhz, narrow, sql,
                        tone_hz if tone_tx_hz is None else tone_tx_hz,
                        dcs_tx, "2", reverse,
                        tail="1" if digital else "0")
                + "F")
    cmd = "D1M%04X%s" % (len(body), body)
    assert int(cmd[3:7], 16) == len(cmd) - 7
    return cmd


def parse_r6423(resp):
    """Decode the R6423 reply.

    Format: "R" + one format digit + ASCII-hex of a CSV line, e.g.
    "00000,00000,XXXXXXXX,20231120153554". Field 2 is the serial
    number shown by the original software; field 3 looks like a
    manufacturing timestamp. The two leading fields were empty on the
    device used for this work.
    """
    if isinstance(resp, bytes):
        resp = resp.decode("ascii", "replace")
    resp = resp.strip()
    if not resp.startswith("R") or len(resp) < 4:
        return None
    hexpart = resp[2:]
    if len(hexpart) % 2:
        hexpart = hexpart[:-1]
    try:
        text = bytes.fromhex(hexpart).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    fields = text.split(",")
    info = {"raw": text, "fields": fields}
    if len(fields) > 2:
        info["serial"] = fields[2]
    if len(fields) > 3 and len(fields[3]) == 14 and fields[3].isdigit():
        t = fields[3]
        info["timestamp"] = "%s-%s-%s %s:%s:%s" % (
            t[0:4], t[4:6], t[6:8], t[8:10], t[10:12], t[12:14])
    return info


# Nominal watts per step, used only to translate the TS-2000 PC
# command. The HRI-200 knows nothing but high, mid and low.
PC_WATT = {"high": 50, "mid": 20, "low": 5}


def watt_to_step(w):
    if w <= 10:
        return "low"
    if w <= 30:
        return "mid"
    return "high"


class HRI200(object):
    """Serial link to the HRI-200."""

    def __init__(self, port, freq_mhz, shift_mhz=0.0, verbose=False,
                 power="mid", tone_hz=88.5, sql="none", narrow=False):
        self.verbose = verbose
        self.freq_mhz = freq_mhz
        self.shift_mhz = shift_mhz
        self.power = power
        self.tone_hz = tone_hz
        self.sql = sql
        self.narrow = narrow
        self.lock = threading.Lock()
        self.tx = False
        self.rx_level = 0          # 0..4, from the XX field of D1C
        self.running = False
        self.buf = b""
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = 38400
        self.ser.bytesize = 8
        self.ser.parity = "N"
        self.ser.stopbits = 1
        self.ser.timeout = 0.1
        self.ser.rtscts = False
        self.ser.xonxoff = False
        self.ser.dtr = True
        self.ser.rts = True

    # -- framing --------------------------------------------------------
    def _send(self, cmd):
        self.ser.write(SOH + cmd.encode("ascii") + EOT)
        self.ser.flush()

    def _collect(self, seconds):
        """Read for a while and split the stream into frames.

        The input buffer is never flushed: replies can be delayed and
        would otherwise be lost or attributed to the wrong command.
        """
        end = time.time() + seconds
        out = []
        while time.time() < end:
            data = self.ser.read(256)
            if not data:
                continue
            self.buf += data
            while EOT in self.buf:
                chunk, self.buf = self.buf.split(EOT, 1)
                chunk = chunk.lstrip(SOH)
                if chunk:
                    out.append(chunk)
                    if self.verbose:
                        print("    [hri] %r" % chunk[:70])
        return out

    def _exchange(self, cmd, prefix, wait=1.0, tries=3):
        for _ in range(tries):
            with self.lock:
                self._send(cmd)
                got = self._collect(wait)
            for f in got:
                if f.startswith(prefix):
                    return f
        return None

    def _d1m(self):
        return d1m(self.freq_mhz, self.shift_mhz, power=self.power,
                   tone_hz=self.tone_hz, sql=self.sql, narrow=self.narrow)

    # -- setup and teardown ---------------------------------------------
    def open(self):
        self.ser.open()
        print("HRI-200 on %s @ 38400 8N1" % self.ser.port)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        print("warm-up %.0f s ..." % WARMUP)
        time.sleep(WARMUP)

        if self._exchange("M00", b"M00") is None:
            raise RuntimeError("handshake M00 got no reply")
        ident = self._exchange("R6423", b"R")
        if ident:
            info = parse_r6423(ident)
            if info:
                print("serial number: %s" % info.get("serial", "?"))

        for cmd in ["P010000", "D1V0000", self._d1m(), "D1B00010"]:
            pre = b"B" if cmd.startswith("P") else cmd[:3].encode("ascii")
            if self._exchange(cmd, pre, wait=1.5) is None:
                print("  WARNING: %s got no reply" % cmd[:9])
        print("HRI-200 ready")

        self.running = True
        self.thread = threading.Thread(target=self._poll, daemon=True)
        self.thread.start()

    def close(self):
        self.running = False
        try:
            self.thread.join(timeout=2.0)
        except Exception:
            pass
        try:
            with self.lock:
                self._send(SHUTDOWN)
                self._collect(0.3)
        finally:
            self.ser.close()

    # -- operation ------------------------------------------------------
    def _poll(self):
        """Keep the 1 Hz heartbeat alive and sample the RX level."""
        while self.running:
            with self.lock:
                self._send(PTT_ON if self.tx else PTT_OFF)
                self._collect(0.15)
                self._send(STATUS)
                for f in self._collect(0.25):
                    if f.startswith(b"D1C000B0") and len(f) >= 12:
                        try:
                            self.rx_level = max(int(f[8:9]), int(f[9:10]))
                        except ValueError:
                            pass
            time.sleep(0.55)

    def set_ptt(self, on):
        on = bool(on)
        if on == self.tx:
            return
        self.tx = on
        with self.lock:
            self._send(PTT_ON if on else PTT_OFF)
        print("PTT %s" % ("ON" if on else "off"))

    def _apply(self, what):
        """Re-send D1M; it carries all channel parameters at once."""
        try:
            cmd = self._d1m()
        except ValueError as exc:
            print("%s rejected: %s" % (what, exc))
            return False
        got = self._exchange(cmd, b"D1M", wait=1.5, tries=2)
        print("%s %s" % (what, "ok" if got else "not acknowledged"))
        return got is not None

    def set_freq(self, freq_mhz):
        old = self.freq_mhz
        self.freq_mhz = freq_mhz
        if not self._apply("frequency %.5f MHz" % freq_mhz):
            self.freq_mhz = old
            return False
        return True

    def set_power(self, step):
        if step not in POWER or step == self.power:
            return False
        old = self.power
        self.power = step
        if not self._apply("power %s" % step):
            self.power = old
            return False
        return True


class TS2000(object):
    """Minimal Kenwood TS-2000 CAT server on a serial port."""

    def __init__(self, hri, port, baud=38400, verbose=False, line_ptt="rts"):
        self.hri = hri
        self.verbose = verbose
        self.line_ptt = line_ptt   # off | rts | dtr | both
        self.cat_tx = False        # requested via TX;/RX;
        self.line_tx = False       # requested via a control line
        self.mode = "4"            # 4 = FM
        self.ser = serial.Serial(port, baud, timeout=0.05)

    def read_lines(self):
        """Evaluate the control lines of the other end.

        In a null-modem pair the application's RTS arrives here as CTS
        and its DTR as DSR. With "both", either one is enough.
        """
        try:
            rts = self.ser.cts if self.line_ptt in ("rts", "both") else False
            dtr = self.ser.dsr if self.line_ptt in ("dtr", "both") else False
        except Exception:
            return False
        return rts or dtr

    def apply_ptt(self):
        """Transmit as soon as either path asks for it."""
        self.hri.set_ptt(self.cat_tx or self.line_tx)

    def freq_hz(self):
        return int(round(self.hri.freq_mhz * 1e6))

    def smeter(self):
        """Stretch rx_level 0..4 onto the TS-2000 scale 0..30.
        A coarse step indicator, not a real S-meter."""
        return min(30, self.hri.rx_level * 7)

    def answer(self, cmd):
        c = cmd[:2]
        if cmd == "ID":
            return "ID019;"
        if c == "PS":
            return "PS1;"
        if c == "AI":
            return "AI0;"
        if c in ("FA", "FB"):
            if len(cmd) > 2 and cmd[2:].isdigit():
                self.hri.set_freq(int(cmd[2:]) / 1e6)
                return None
            return "%s%011d;" % (c, self.freq_hz())
        if c == "MD":
            if len(cmd) > 2:
                self.mode = cmd[2]
                return None
            return "MD%s;" % self.mode
        if c == "SM":
            return "SM0%04d;" % self.smeter()
        if cmd.startswith("TX"):
            self.cat_tx = True
            self.apply_ptt()
            return None
        if cmd == "RX":
            self.cat_tx = False
            self.apply_ptt()
            return None
        if c == "IF":
            # 38 characters including "IF" and ";":
            #   P1 freq 11, P2 step 4, P3 RIT 6, P4 RIT, P5 XIT,
            #   P6 bank, P7 channel 2, P8 TX/RX, P9 mode, P10 VFO,
            #   P11 scan, P12 split, P13 tone, P14 tone no. 2, P15
            return ("IF"
                    + "%011d" % self.freq_hz()
                    + "0000"
                    + "+00000"
                    + "0" + "0" + "0" + "00"
                    + ("1" if self.hri.tx else "0")
                    + self.mode
                    + "0" + "0" + "0" + "0" + "00" + "0"
                    + ";")
        if c in ("FR", "FT"):
            return "%s0;" % c
        if c == "AG":
            return "AG0000;"
        if c == "RF":
            return "RF0000;"
        if c == "SQ":
            return "SQ0000;"
        if c == "PC":
            if len(cmd) > 2 and cmd[2:].isdigit():
                self.hri.set_power(watt_to_step(int(cmd[2:])))
                return None
            return "PC%03d;" % PC_WATT[self.hri.power]
        return "?;"

    def serve(self):
        print("CAT server on %s - in flrig pick Rig = TS-2000 and the "
              "other end of the pair" % self.ser.port)
        if self.line_ptt != "off":
            print("PTT also via control line: %s" % self.line_ptt)
            print("  (the application's RTS arrives as CTS, its DTR as DSR)")
            try:
                print("  initial state: CTS=%s  DSR=%s"
                      % (self.ser.cts, self.ser.dsr))
            except Exception as exc:
                print("  lines not readable: %s" % exc)
            if self.read_lines():
                print("  WARNING: the line is already asserted. Either the")
                print("  application is transmitting, or the virtual port")
                print("  pair holds the line high permanently - in which")
                print("  case use --line-ptt off and PTT via CAT.")
        buf = ""
        while True:
            data = self.ser.read(64)
            if data:
                buf += data.decode("ascii", "ignore")
                while ";" in buf:
                    cmd, buf = buf.split(";", 1)
                    cmd = cmd.strip()
                    if not cmd:
                        continue
                    rep = self.answer(cmd)
                    if self.verbose:
                        print("  [cat] %-14s -> %s" % (cmd, rep))
                    if rep:
                        self.ser.write(rep.encode("ascii"))
            else:
                time.sleep(0.01)

            if self.line_ptt != "off":
                line = self.read_lines()
                if line != self.line_tx:
                    self.line_tx = line
                    self.apply_ptt()


def main():
    ap = argparse.ArgumentParser(
        description="Use a Yaesu HRI-200 as a TS-2000 compatible interface")
    ap.add_argument("catport", help="virtual port for flrig, e.g. COM11")
    ap.add_argument("--hri", default="COM7", help="HRI-200 port")
    ap.add_argument("--freq", type=float, default=144.85, help="MHz")
    ap.add_argument("--shift", type=float, default=0.0, help="offset in MHz")
    ap.add_argument("--power", default="mid", choices=["high", "mid", "low"])
    ap.add_argument("--tone", type=float, default=88.5, help="CTCSS in Hz")
    ap.add_argument("--sql", default="none", choices=["none", "tone", "dcs"])
    ap.add_argument("--narrow", action="store_true")
    ap.add_argument("--catbaud", type=int, default=38400)
    ap.add_argument("--line-ptt", default="rts",
                    choices=["off", "rts", "dtr", "both"],
                    help="control line used as PTT: rts (default, arrives "
                         "as CTS), dtr (as DSR), both, or off")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if serial is None:
        sys.exit("pyserial missing:  py -3 -m pip install pyserial")

    hri = HRI200(args.hri, args.freq, args.shift, verbose=args.verbose,
                 power=args.power, tone_hz=args.tone, sql=args.sql,
                 narrow=args.narrow)
    try:
        hri.open()
    except Exception as exc:
        sys.exit("HRI-200 setup failed: %s" % exc)

    try:
        cat = TS2000(hri, args.catport, args.catbaud,
                     verbose=args.verbose, line_ptt=args.line_ptt)
    except Exception as exc:
        hri.close()
        sys.exit("CAT port %s: %s" % (args.catport, exc))

    try:
        cat.serve()
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        hri.set_ptt(False)
        time.sleep(0.3)
        hri.close()
        cat.ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
