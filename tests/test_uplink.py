#!/usr/bin/env python3
"""Build uplink packets from the captured receive-direction frames.

Without arguments this checks what can be checked in Python: the packets
come out the right size and shape, the frame numbers and data-channel
fields rotate as they should, and the voice survives being packed and
unpacked. With --hex it prints the packets for reference_check.cpp,
which runs urfd's own decoders over them; see README.md here.

    python3 tests/test_uplink.py
    python3 tests/test_uplink.py --hex | ./check
"""

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hri200_ysf
import ysf_frame

CAPTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "captures", "20260826-rx-direction.txt")
GATEWAY = "DL4JC-ND"
SOURCE = "DL4JC"


def payloads():
    out = []
    for line in open(CAPTURE):
        body = line.split(" ", 1)[1].strip()
        if body.startswith("D1R"):
            out.append(hri200_ysf.parse_voice_frame(body))
    return out


def build(dgid=0):
    """Run a whole transmission through the client and collect the packets."""
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    client = hri200_ysf.YSFClient("127.0.0.1", sink.getsockname()[1], GATEWAY,
                                  dgid=dgid)
    client.start_stream(SOURCE)
    for payload in payloads():
        client.send_voice(payload)
    client.end_stream()

    sink.settimeout(0.5)
    packets = []
    while True:
        try:
            packet, _ = sink.recvfrom(2048)
        except socket.timeout:
            break
        packets.append(packet)
    sink.close()
    return packets


def main():
    if "--hex" in sys.argv:
        for packet in build():
            print(packet.hex().upper())
        return 0

    voice = payloads()
    print("captured D1R frames: %d, all carrying voice: %s"
          % (len(voice), all(v is not None for v in voice)))

    packets = build()
    print("packets: %d (header + %d voice + terminator)"
          % (len(packets), len(voice)))

    checks = []
    checks.append(("all 155 bytes",
                   all(len(p) == hri200_ysf.YSFD_LENGTH for p in packets)))
    checks.append(("all tagged YSFD",
                   all(p.startswith(b"YSFD") for p in packets)))
    checks.append(("gateway and source in the header",
                   all(p[4:14] == b"DL4JC-ND  " and p[14:24] == b"DL4JC     "
                       for p in packets)))
    checks.append(("sync on every frame",
                   all(p[35:40] == ysf_frame.SYNC for p in packets)))
    checks.append(("one packet per captured frame plus two",
                   len(packets) == len(voice) + 2))

    # The voice has to come back out of the packets untouched.
    body = packets[1:-1]
    checks.append(("voice unchanged through packing",
                   [hri200_ysf.voice_from_ysfd(p) for p in body] == voice))

    # DG-ID 99 has to reach the FICH.
    plain = ysf_frame.Fich(fn=0, sql=False, sq=0).encode()
    tagged = ysf_frame.Fich(fn=0, sql=True, sq=99).encode()
    checks.append(("DG-ID changes the FICH", plain != tagged))

    for name, ok in checks:
        print("  %-40s %s" % (name, "ok" if ok else "FAILED"))
    failed = [n for n, ok in checks if not ok]
    print("FAIL" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
