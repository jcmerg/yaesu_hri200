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
and hand it over. Uplink has to put back what the radio stripped, which
`ysf_frame` does. No vocoder is involved in either direction.

    python3 hri200_ysf.py --hri COM7 --call DL4JC \
        --host ysf.example.org --port 42000 --freq 144.85

    python3 hri200_ysf.py --dry-run --call DL4JC --host ... --port ...
"""

import argparse
import socket
import types
import sys
import threading
import time
from collections import deque

import hri200_cat
import ysf_frame
from hri200_cat import HRI200, EOT, SOH
from ysf_frame import (PAYLOAD_LENGTH, VCH_COUNT, VCH_LENGTH, is_voice,
                       is_voice_payload)

# ----------------------------------------------------------------------
# YSF frame geometry
#
# A YSFD packet is 35 bytes of header followed by the 120-byte air
# frame: 5 sync, 25 FICH, then five 18-byte blocks of 5 bytes DCH and
# 13 bytes VCH.

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
    blocks = [frame[ysf_frame.VCH_OFFSET + ysf_frame.BLOCK_STRIDE * i:
                    ysf_frame.VCH_OFFSET + ysf_frame.BLOCK_STRIDE * i
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
        self._emit(ysf_frame.build_header_frame(self.source, self.gateway,
                                                dgid=self.dgid), 0)
        self.counter = 1

    def send_voice(self, payload):
        dch = ysf_frame.dch_for_frame(self.fn, self.source, self.gateway)
        frame = ysf_frame.build_frame(payload, fi=ysf_frame.FI_COMMUNICATIONS,
                                      fn=self.fn, ft=6, dch=dch,
                                      dgid=self.dgid)
        self._emit(frame, self.counter)
        self.counter += 1
        self.fn = (self.fn + 1) % 7

    def end_stream(self):
        """Close it with a terminator, which is what a reflector waits for."""
        self._emit(ysf_frame.build_header_frame(self.source, self.gateway,
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
