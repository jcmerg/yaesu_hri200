#!/usr/bin/env python3
"""Bridge a YSF reflector and an HRI-200, both ways.

    YSF reflector =[UDP]= hri200_ysf.py =[COM7]= HRI-200 -> radio

The HRI-200 does not hand the C4FM air interface to the PC. What travels
over the serial link is 65 bytes per 100 ms, and those 65 bytes are the
five VCH sections of a YSF V/D mode 2 frame in on-air form, 13 bytes
each. Sync, FICH and the DCH data channel have already been removed by
the radio. The frame is `D1E` outbound and `D1R` inbound, identical apart
from the letter.

Downlink is therefore a byte copy: take the voice out of a YSFD packet
and hand it over. Uplink has to put back what the radio stripped. No
vocoder is involved in either direction.

    python3 hri200_ysf.py --hri COM7 --call DL4JC \
        --host ysf.example.org --port 42000 --freq 144.85

    python3 hri200_ysf.py --dry-run --call DL4JC --host ... --port ...

The radio reports its position when it has GPS switched on, and the
gateway drops it unless `--gps` says otherwise.
"""

import argparse
import socket
import types
import sys
import threading
import time
from collections import deque

import hri200_cat
from hri200_cat import HRI200, EOT, SOH

# ======================================================================
# Part one: the YSF air frame
#
# A YSFD packet is 35 bytes of header followed by the 120-byte air
# frame: 5 sync, 25 FICH, then five 18-byte blocks of 5 bytes DCH and
# 13 bytes VCH. The radio hands over only the VCH sections, so the
# encoders below put the rest back.
#
# Everything in this part is a port of the encoders in MMDVMHost, by way
# of urfd, and is checked against them: tests/test_py replays
# 400 vectors from urfd's own CYSFFICH::encode, writeVDMode2Data and
# writeHeader and requires every byte to match.
# ======================================================================

# ----------------------------------------------------------------------
# frame geometry

FRAME_LENGTH = 120
SYNC = bytes([0xD4, 0x71, 0xC9, 0x63, 0x4D])
SYNC_LENGTH = 5
FICH_LENGTH = 25
BLOCK_STRIDE = 18
DCH_LENGTH = 5
VCH_OFFSET = 35
VCH_LENGTH = 13
VCH_COUNT = 5

FI_HEADER, FI_COMMUNICATIONS, FI_TERMINATOR, FI_TEST = 0, 1, 2, 3
DT_VD_MODE1, DT_DATA_FR_MODE, DT_VD_MODE2, DT_VOICE_FR_MODE = 0, 1, 2, 3
MR_BUSY = 2

# Every interleaver here is the same read-out with a different width.
def _interleave(columns):
    return [2 * row + 40 * col for row in range(20) for col in range(columns)]


INTERLEAVE_5 = _interleave(5)      # FICH and the V/D mode 2 DCH
INTERLEAVE_9 = _interleave(9)      # the header and terminator payload

# The VCH interleaver is a different shape: 104 bits, four ways.
INTERLEAVE_VCH = [4 * k + base for base in range(4) for k in range(26)]

PAYLOAD_LENGTH = VCH_LENGTH * VCH_COUNT      # 65, what a D1E or D1R carries

WHITENING = bytes([0x93, 0xD7, 0x51, 0x21, 0x9C, 0x2F, 0x6C, 0xD0, 0xEF,
                   0x0F, 0xF8, 0x3D, 0xF1, 0x73, 0x20, 0x94, 0xED, 0x1E,
                   0x7C, 0xD8])

CALLSIGN_LENGTH = 10


# ----------------------------------------------------------------------
# bit helpers, MSB first throughout

def get_bit(data, i):
    return (data[i >> 3] >> (7 - (i & 7))) & 1


def set_bit(data, i, value):
    if value:
        data[i >> 3] |= 1 << (7 - (i & 7))
    else:
        data[i >> 3] &= ~(1 << (7 - (i & 7))) & 0xFF


def is_voice(block):
    """True if a 13-byte block is a well-formed V/D mode 2 VCH.

    After de-interleaving and removing the whitening, bits 0..80 fall into
    27 triplets that carry each data bit three times. Voice passes this at
    100 %, unrelated data at the 25 % a coin toss gives, which makes it a
    reliable filter for frames that hold no voice at all.
    """
    raw = [get_bit(block, i) for i in range(104)]
    vch = [raw[INTERLEAVE_VCH[i]] ^ get_bit(WHITENING, i) for i in range(104)]
    return all(vch[3 * i] == vch[3 * i + 1] == vch[3 * i + 2] for i in range(27))


def split_vch(payload):
    """Cut a 65-byte payload into its five 13-byte sections."""
    return [payload[VCH_LENGTH * i:VCH_LENGTH * (i + 1)]
            for i in range(VCH_COUNT)]


def is_voice_payload(payload):
    return (len(payload) == PAYLOAD_LENGTH
            and all(is_voice(b) for b in split_vch(payload)))


# ----------------------------------------------------------------------
# Golay (24,12)

_GENPOL = 0xC75
_X22 = 0x00400000
_X11 = 0x00000800
_MASK12 = 0xFFFFF800


def _syndrome_23127(pattern):
    aux = _X22
    if pattern >= _X11:
        while pattern & _MASK12:
            while not (aux & pattern):
                aux >>= 1
            pattern ^= (aux // _X11) * _GENPOL
    return pattern


def golay_23127(data):
    """12 data bits to a 23-bit codeword, data in the high bits."""
    code = (data << 11) & 0x7FFFFF
    return code | _syndrome_23127(code)


def golay_24128(data):
    """The same with an overall parity bit appended."""
    code = golay_23127(data)
    parity = bin(code).count("1") & 1
    return (code << 1) | parity


# ----------------------------------------------------------------------
# CRC-16/CCITT, the variant that stores the result low byte last

_CRC_TABLE = []
for _b in range(256):
    _c = _b << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1021) & 0xFFFF if _c & 0x8000 else (_c << 1) & 0xFFFF
    _CRC_TABLE.append(_c)


def add_ccitt162(data, length):
    """Write the CRC of data[:length-2] into the last two bytes."""
    crc = 0
    for i in range(length - 2):
        crc = ((crc & 0xFF) << 8) ^ _CRC_TABLE[((crc >> 8) & 0xFF) ^ data[i]]
    crc = ~crc & 0xFFFF
    data[length - 1] = crc & 0xFF
    data[length - 2] = (crc >> 8) & 0xFF


# ----------------------------------------------------------------------
# rate 1/2, constraint length 5 convolutional code

def convolve(data, nbits):
    """Encode nbits of data into 2*nbits bits."""
    out = bytearray((2 * nbits + 7) // 8)
    d1 = d2 = d3 = d4 = 0
    k = 0
    for i in range(nbits):
        d = get_bit(data, i)
        set_bit(out, k, (d + d3 + d4) & 1)
        k += 1
        set_bit(out, k, (d + d1 + d2 + d4) & 1)
        k += 1
        d4, d3, d2, d1 = d3, d2, d1, d
    return out


def _spread(convolved, table, nbytes):
    """Interleave bit pairs into the field they are sent in."""
    out = bytearray(nbytes)
    j = 0
    for n in table:
        set_bit(out, n, get_bit(convolved, j))
        set_bit(out, n + 1, get_bit(convolved, j + 1))
        j += 2
    return bytes(out)


# ----------------------------------------------------------------------
# FICH

class Fich(object):
    """The 25-byte frame information channel.

    Field names follow the YSF specification; the bit positions are those
    MMDVMHost uses.
    """

    def __init__(self, fi=FI_COMMUNICATIONS, cs=2, cm=0, bn=0, bt=0, fn=0,
                 ft=6, dt=DT_VD_MODE2, mr=MR_BUSY, dev=False, sql=False,
                 sq=0, voip=False):
        self.fi, self.cs, self.cm = fi, cs, cm
        self.bn, self.bt = bn, bt
        self.fn, self.ft = fn, ft
        self.dt, self.mr = dt, mr
        self.dev, self.sql, self.sq, self.voip = dev, sql, sq, voip

    def _raw(self):
        f = bytearray(6)
        f[0] = ((self.fi << 6) & 0xC0) | ((self.cs << 4) & 0x30) \
            | ((self.cm << 2) & 0x0C) | (self.bn & 0x03)
        f[1] = ((self.bt << 6) & 0xC0) | ((self.fn << 3) & 0x38) \
            | (self.ft & 0x07)
        f[2] = (0x40 if self.dev else 0) | ((self.mr << 3) & 0x38) \
            | (0x04 if self.voip else 0) | (self.dt & 0x03)
        f[3] = (0x80 if self.sql else 0) | (self.sq & 0x7F)
        return f

    def encode(self):
        f = self._raw()
        add_ccitt162(f, 6)

        blocks = [((f[0] << 4) & 0xFF0) | ((f[1] >> 4) & 0x00F),
                  ((f[1] << 8) & 0xF00) | f[2],
                  ((f[3] << 4) & 0xFF0) | ((f[4] >> 4) & 0x00F),
                  ((f[4] << 8) & 0xF00) | f[5]]

        conv = bytearray(13)
        for i, block in enumerate(blocks):
            code = golay_24128(block)
            conv[3 * i] = (code >> 16) & 0xFF
            conv[3 * i + 1] = (code >> 8) & 0xFF
            conv[3 * i + 2] = code & 0xFF
        return _spread(convolve(conv, 100), INTERLEAVE_5, 25)


# ----------------------------------------------------------------------
# DCH

def encode_dch(text):
    """Encode ten characters into the 5 bytes each block carries.

    Returns the 25 bytes that go in front of the five voice sections, five
    at a time.
    """
    field = bytearray(13)
    raw = text if isinstance(text, bytes) else \
        ("%-10s" % text)[:CALLSIGN_LENGTH].encode("ascii", "replace")
    raw = (raw + b" " * CALLSIGN_LENGTH)[:CALLSIGN_LENGTH]
    for i in range(CALLSIGN_LENGTH):
        field[i] = raw[i] ^ WHITENING[i]
    add_ccitt162(field, 12)
    field[12] = 0x00
    return _spread(convolve(field, 100), INTERLEAVE_5, 25)


def encode_fr_data(dt):
    """Encode 20 bytes into the 9 bytes each block carries in FR mode.

    Header and terminator frames carry no voice: both halves of every
    block are data, nine bytes from each of two of these.
    """
    field = bytearray(25)
    raw = ("%-20s" % dt)[:20].encode("ascii", "replace")
    for i in range(20):
        field[i] = raw[i] ^ WHITENING[i]
    add_ccitt162(field, 22)
    field[22] = 0x00
    return _spread(convolve(field, 180), INTERLEAVE_9, 45)


# ----------------------------------------------------------------------

def build_header_frame(source, gateway, terminator=False, dgid=0):
    """Assemble the frame that opens or closes a transmission.

    Neither carries voice. The first ten characters name the gateway and
    the next ten the station being heard, which is what a reflector reads
    to label the stream.
    """
    frame = bytearray(FRAME_LENGTH)
    frame[0:SYNC_LENGTH] = SYNC

    fich = Fich(fi=FI_TERMINATOR if terminator else FI_HEADER, fn=0, ft=7,
                sql=dgid > 0, sq=dgid)
    frame[SYNC_LENGTH:SYNC_LENGTH + FICH_LENGTH] = fich.encode()

    csd1 = ("%-10s" % gateway)[:10] + ("%-10s" % source)[:10]
    first = encode_fr_data(csd1)
    second = encode_fr_data(" " * 20)

    base = SYNC_LENGTH + FICH_LENGTH
    for i in range(VCH_COUNT):
        at = base + BLOCK_STRIDE * i
        frame[at:at + 9] = first[9 * i:9 * (i + 1)]
        frame[at + 9:at + 18] = second[9 * i:9 * (i + 1)]
    return bytes(frame)


def dch_from_d1g(body):
    """Pull the data-channel fields out of a D1G frame.

    The tail behind the radio ID is already shaped like the 10-byte
    fields the data channel carries: the short frame holds one, padded
    out with spaces, and the long one holds two. They are passed on as
    they arrive rather than taken apart, so nothing is invented.
    """
    tail = body[75:]
    if not tail or len(tail) % 2:
        return []
    try:
        raw = bytes.fromhex(tail)
    except ValueError:
        return []
    fields = []
    for at in range(0, len(raw), CALLSIGN_LENGTH):
        field = raw[at:at + CALLSIGN_LENGTH]
        if len(field) < CALLSIGN_LENGTH:
            field += b" " * (CALLSIGN_LENGTH - len(field))
        fields.append(field)
    return fields


def dch_for_frame(fn, source, gateway, gps=None):
    """Which of the data-channel fields a given frame number carries.

    The rotation follows what MMDVMHost and urfd emit, so a reflector sees
    the fields where it expects them.

    Slot 6 is where a station with GPS puts its position. Without `gps` it
    carries the blank MMDVMHost uses for unassigned slots, which is the
    default: the radio does report its position, but forwarding it is the
    operator's decision. urfd fills the slot with a fixed byte string
    instead, which has the shape of a position lifted from somebody's
    capture, so it is not copied here.
    """
    if fn == 0:
        return "**********"
    if fn == 1:
        return source
    if fn in (2, 5):
        return gateway
    if fn == 6 and gps is not None:
        return gps
    return " " * 10


def build_frame(voice, fi=FI_COMMUNICATIONS, fn=0, ft=6, dch="**********",
                dgid=0):
    """Assemble a 120-byte V/D mode 2 frame.

    voice: the 65 bytes the HRI-200 delivers in a D1R frame, which are the
    five VCH sections already in on-air form and go in untouched.
    """
    if len(voice) != VCH_LENGTH * VCH_COUNT:
        raise ValueError("voice is %d bytes, expected %d"
                         % (len(voice), VCH_LENGTH * VCH_COUNT))

    frame = bytearray(FRAME_LENGTH)
    frame[0:SYNC_LENGTH] = SYNC

    fich = Fich(fi=fi, fn=fn, ft=ft, sql=dgid > 0, sq=dgid)
    frame[SYNC_LENGTH:SYNC_LENGTH + FICH_LENGTH] = fich.encode()

    data = encode_dch(dch)
    base = SYNC_LENGTH + FICH_LENGTH
    for i in range(VCH_COUNT):
        at = base + BLOCK_STRIDE * i
        frame[at:at + DCH_LENGTH] = data[DCH_LENGTH * i:DCH_LENGTH * (i + 1)]
        frame[at + DCH_LENGTH:at + BLOCK_STRIDE] = \
            voice[VCH_LENGTH * i:VCH_LENGTH * (i + 1)]
    return bytes(frame)


# ======================================================================
# Part two: the gateway
# ======================================================================

YSFD_LENGTH = 155
FRAME_OFFSET = 35        # where the 120-byte air frame starts in a packet
DEST = b"ALL" + b" " * 7

FRAME_INTERVAL = 0.100   # one voice frame per 100 ms
COUNTER_STEP = 5         # the D1E and D1R counter runs per 20 ms subframe
POLL_INTERVAL = 5.0      # YSFP keepalive
UPLINK_GAP = 0.4         # silence after which a received stream is over


def voice_from_ysfd(packet):
    """Extract the 65-byte D1E payload from a YSFD packet, or None."""
    if len(packet) != YSFD_LENGTH or not packet.startswith(b"YSFD"):
        return None
    frame = packet[FRAME_OFFSET:]
    blocks = [frame[VCH_OFFSET + BLOCK_STRIDE * i:
                    VCH_OFFSET + BLOCK_STRIDE * i
                    + VCH_LENGTH]
              for i in range(VCH_COUNT)]
    if not all(is_voice(b) for b in blocks):
        return None
    return b"".join(blocks)


def parse_voice_frame(body):
    """Pull the 65 bytes out of a D1E or D1R frame, or return None."""
    if len(body) != 144 or body[:7] not in ("D1E0089", "D1R0089"):
        return None
    try:
        payload = bytes.fromhex(body[14:])
    except ValueError:
        return None
    return payload if is_voice_payload(payload) else None


def d1e(counter, payload):
    """Build a D1E voice frame.

    The header is a constant "2", a 16-bit counter, and "00". Positions
    12 and 13 read "40" or "C0" in three of 1020 captured frames for
    reasons that are not understood; "00" is what the rest carry.
    """
    if len(payload) != PAYLOAD_LENGTH:
        raise ValueError("payload is %d bytes, expected %d"
                         % (len(payload), PAYLOAD_LENGTH))
    body = "2%04X00%s" % (counter & 0xFFFF, payload.hex().upper())
    return "D1E%04X%s" % (len(body), body)


# ----------------------------------------------------------------------
# reflector link

class YSFClient(object):
    """Client side of the YSFReflector protocol."""

    def __init__(self, host, port, callsign, verbose=False, dgid=0):
        self.addr = (host, port)
        self.gateway = ("%-10s" % callsign.upper())[:10]
        self.callsign = self.gateway.encode("ascii")
        self.dgid = dgid
        self.verbose = verbose
        self.gps = deque(maxlen=8)     # position fields waiting for slot 6
        self.source = None
        self.fn = 0
        self.counter = 0
        self.sent = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.2)
        self.queue = deque(maxlen=100)
        self.running = False
        self.packets = 0
        self.skipped = 0

    def start(self):
        self.sock.sendto(b"YSFP" + self.callsign, self.addr)
        print("linked to %s:%d as %s"
              % (self.addr[0], self.addr[1], self.callsign.decode().strip()))
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        try:
            self.thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            self.sock.sendto(b"YSFU" + self.callsign, self.addr)
        except OSError:
            pass
        self.sock.close()

    def _run(self):
        next_poll = time.time() + POLL_INTERVAL
        while self.running:
            now = time.time()
            if now >= next_poll:
                try:
                    self.sock.sendto(b"YSFP" + self.callsign, self.addr)
                except OSError as exc:
                    print("poll failed: %s" % exc)
                next_poll = now + POLL_INTERVAL
            try:
                packet, _ = self.sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                continue
            if not packet.startswith(b"YSFD"):
                continue
            self.packets += 1
            payload = voice_from_ysfd(packet)
            if payload is None:
                self.skipped += 1
                continue
            self.queue.append(payload)

    # -- uplink ---------------------------------------------------------
    def _emit(self, frame, counter):
        packet = (b"YSFD" + self.callsign
                  + ("%-10s" % self.source)[:10].encode("ascii", "replace")
                  + DEST + bytes([counter & 0x7F]) + frame)
        try:
            self.sock.sendto(packet, self.addr)
        except OSError as exc:
            print("uplink failed: %s" % exc)
            return
        self.sent += 1

    def start_stream(self, source):
        """Open a transmission with a header frame."""
        self.source = source or "?"
        self.fn = 0
        self.counter = 0
        self._emit(build_header_frame(self.source, self.gateway,
                                                dgid=self.dgid), 0)
        self.counter = 1
        self.gps.clear()

    def send_voice(self, payload):
        gps = None
        if self.fn == 6 and self.gps:
            gps = self.gps.popleft()
        dch = dch_for_frame(self.fn, self.source, self.gateway, gps=gps)
        frame = build_frame(payload, fi=FI_COMMUNICATIONS,
                                      fn=self.fn, ft=6, dch=dch,
                                      dgid=self.dgid)
        self._emit(frame, self.counter)
        self.counter += 1
        self.fn = (self.fn + 1) % 7

    def end_stream(self):
        """Close it with a terminator, which is what a reflector waits for."""
        self._emit(build_header_frame(self.source, self.gateway,
                                                terminator=True,
                                                dgid=self.dgid), 0)
        self.source = None


# ----------------------------------------------------------------------
# HRI-200 in digital mode

class HRI200Digital(HRI200):
    """HRI-200 driven at the 100 ms C4FM cadence instead of 1 Hz CAT polling.

    The inherited poll loop runs every 550 ms and holds the port lock for
    most of it, which would starve the voice pump, so the loop is
    replaced rather than extended.
    """

    def __init__(self, *args, **kwargs):
        self.dgid = kwargs.pop("dgid", 0)
        self.source = kwargs.pop("source", None)
        self.prefill = kwargs.pop("prefill", 5)
        self.tone_tx_hz = kwargs.pop("tone_tx_hz", None)
        HRI200.__init__(self, *args, **kwargs)
        self.counter = 0
        self.streaming = False
        self.sent = 0
        self.total = 0
        self.underruns = 0
        self.uplink = None          # set by main() to the reflector client
        self.forward_gps = False    # --gps
        self.heard = None           # callsign from D1H or D1G
        self.receiving = False
        self.last_rx = 0.0
        self.rx_frames = 0

    def _d1m(self):
        return hri200_cat.d1m(self.freq_mhz, self.shift_mhz, power=self.power,
                              tone_hz=self.tone_hz, sql=self.sql,
                              narrow=self.narrow, digital=True, dgid=self.dgid,
                              tone_tx_hz=self.tone_tx_hz)

    def _poll(self):
        """Send one D1E every 100 ms and keep the PTT heartbeat alive.

        Playout waits for `prefill` frames so network jitter does not tear
        the stream apart, and stops after three empty ticks.
        """
        tick = time.time()
        beat = 0
        empty = 0
        while self.running:
            tick += FRAME_INTERVAL
            delay = tick - time.time()
            if delay > 0:
                time.sleep(delay)
            elif delay < -FRAME_INTERVAL:
                tick = time.time()          # fell behind, resynchronise

            payload = None
            if self.streaming:
                if self.source:
                    payload = self.source()
                if payload is None:
                    empty += 1
                    if empty >= 3:
                        self._stop_stream()
                else:
                    empty = 0
            elif self.source and len(self.source.queue) >= self.prefill:
                self._start_stream()
                payload = self.source()

            with self.lock:
                if payload is not None:
                    self._send(d1e(self.counter, payload))
                    self.counter = (self.counter + COUNTER_STEP) & 0xFFFF
                    self.sent += 1
                    self.total += 1
                beat += 1
                if beat >= 10:
                    beat = 0
                    self._send("P100000" if self.tx else "P010000")
                self._read()
            self._expire_rx()

    def _read(self):
        """Take in whatever the box sent and split it into frames.

        Called with the port lock held.
        """
        try:
            waiting = self.ser.in_waiting
        except Exception:
            return
        if waiting:
            self.buf += self.ser.read(waiting)
        while EOT in self.buf:
            chunk, self.buf = self.buf.split(EOT, 1)
            chunk = chunk.lstrip(SOH)
            if chunk:
                self._handle(chunk.decode("ascii", "replace"))

    def _handle(self, body):
        if body.startswith("D1R"):
            self._on_voice(body)
        elif body.startswith("D1H") and len(body) >= 37:
            self._on_callsign(body[27:37])
        elif body.startswith("D1G") and len(body) >= 35:
            self._on_callsign(body[25:35])
            if self.forward_gps and self.uplink is not None:
                self.uplink.gps.extend(dch_from_d1g(body))

    def _on_callsign(self, field):
        name = field.strip()
        if name and name.strip("*"):
            self.heard = name

    def _on_voice(self, body):
        """Pass a received frame up to the reflector.

        Ignored while the radio is transmitting: the link is half duplex
        and anything arriving then is not a station on the node frequency.
        """
        if self.streaming or self.uplink is None:
            return
        payload = parse_voice_frame(body)
        if payload is None:
            return
        self.last_rx = time.time()
        if not self.receiving:
            self.receiving = True
            self.rx_frames = 0
            self.uplink.start_stream(self.heard or "?")
            print("heard %s" % (self.heard or "unknown"))
        self.uplink.send_voice(payload)
        self.rx_frames += 1

    def _expire_rx(self):
        """End an uplink stream once the frames stop coming."""
        if self.receiving and time.time() - self.last_rx > UPLINK_GAP:
            self.receiving = False
            self.uplink.end_stream()
            print("heard end (%d frames)" % self.rx_frames)

    def _start_stream(self):
        self.streaming = True
        self.counter = 0
        self.tx = True
        self._send_locked("P100000")
        print("stream start")

    def _stop_stream(self):
        self.streaming = False
        self.underruns += 1
        self.tx = False
        self._send_locked("P010000")
        print("stream end (%d frames)" % self.sent)
        self.sent = 0

    def _send_locked(self, cmd):
        with self.lock:
            self._send(cmd)


class _NullSerial(object):
    """Stand-in for the serial port: prints writes and fakes the replies.

    Enough of the HRI-200 to walk through the start-up handshake, so the
    frame generation can be exercised without hardware. The replies are
    the ones documented in README.md, not recorded ones — no capture of
    the read direction exists.
    """

    REPLIES = {
        "M00": "M00",
        "R6423": "R6" + "00000,00000,XXXXXXXX,20231120153554".encode().hex().upper(),
        "D1V0000": "D1V0030DRY-RUN",
        "D1B00010": "D1B00010",
        "D1C0000": "D1C000B00000FD2wr0",
    }

    def __init__(self, verbose=False):
        self.port = "dry-run"
        self.verbose = verbose
        self.frames = 0
        self.pending = b""

    def open(self):
        pass

    def close(self):
        pass

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return len(self.pending)

    def read(self, n=1):
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def reset_input_buffer(self):
        self.pending = b""

    def reset_output_buffer(self):
        pass

    def write(self, data):
        body = data.strip(SOH + EOT).decode("ascii", "replace")
        self.frames += 1
        if self.verbose:
            print("  -> %s" % body)
        elif not body.startswith(("D1E", "P0", "P1", "D1C")):
            print("  -> %s" % (body if len(body) < 60 else body[:57] + "..."))
        reply = self.REPLIES.get(body)
        if reply is None and body.startswith("D1M"):
            reply = body
        elif reply is None and body.startswith("P"):
            reply = "B0 0    0000000"
        if reply is not None:
            self.pending += SOH + reply.encode("ascii") + EOT
        return len(data)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hri", default="COM7", help="HRI-200 serial port")
    ap.add_argument("--call", required=True, help="your callsign")
    ap.add_argument("--host", required=True, help="reflector hostname")
    ap.add_argument("--port", type=int, required=True, help="reflector port")
    ap.add_argument("--freq", type=float, default=144.85, help="MHz")
    ap.add_argument("--shift", type=float, default=0.0, help="offset in MHz")
    ap.add_argument("--power", default="mid", choices=["high", "mid", "low"])
    ap.add_argument("--dgid", type=int, default=0, help="DG-ID 0..99")
    ap.add_argument("--prefill", type=int, default=5,
                    help="frames buffered before playout starts")
    ap.add_argument("--gps", action="store_true",
                    help="pass the radio's position on to the reflector")
    ap.add_argument("--dry-run", action="store_true",
                    help="do not open the serial port, print frames instead")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and hri200_cat.serial is None:
        sys.exit("pyserial missing:  py -3 -m pip install pyserial")

    if args.dry_run:
        hri200_cat.serial = types.SimpleNamespace(
            Serial=lambda: _NullSerial(verbose=args.verbose))
        hri200_cat.WARMUP = 0.0

    client = YSFClient(args.host, args.port, args.call, verbose=args.verbose,
                       dgid=args.dgid)

    def pull():
        try:
            return client.queue.popleft()
        except IndexError:
            return None
    pull.queue = client.queue

    hri = HRI200Digital(args.hri, args.freq, args.shift, verbose=args.verbose,
                        power=args.power, dgid=args.dgid, source=pull,
                        prefill=args.prefill)

    hri.uplink = client
    hri.forward_gps = args.gps
    if args.gps:
        print("GPS: the position your radio reports goes out to the network")

    client.start()
    try:
        hri.open()
    except Exception as exc:
        client.stop()
        sys.exit("HRI-200: %s" % exc)

    print("running, Ctrl-C to stop")
    try:
        while True:
            time.sleep(5.0)
            print("  down: %d YSFD in, %d skipped, %d D1E out"
                  "   up: %d YSFD out"
                  % (client.packets, client.skipped, hri.total, client.sent))
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        hri.set_ptt(False)
        time.sleep(0.2)
        hri.close()
        client.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
