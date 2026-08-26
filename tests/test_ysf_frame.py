#!/usr/bin/env python3
"""Check ysf_frame.py against MMDVMHost's encoders.

The vectors in ysf_reference_vectors.txt were produced by compiling
urfd's CYSFFICH::encode and CYSFPayload::writeVDMode2Data and dumping
their output for random inputs, so this is a comparison against the
implementation the reflectors actually run, not against a reading of it.

    python3 tests/test_ysf_frame.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ysf_frame

VECTORS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "ysf_reference_vectors.txt")


def check_fich(fields, expected):
    fi, cs, cm, bn, bt, fn, ft, dt, mr, sq, dev, sql, voip = fields
    fich = ysf_frame.Fich(fi=fi, cs=cs, cm=cm, bn=bn, bt=bt, fn=fn, ft=ft,
                          dt=dt, mr=mr, sq=sq, dev=bool(dev), sql=bool(sql),
                          voip=bool(voip))
    return fich.encode() == expected


def check_dch(text, expected):
    """The reference writes the DCH into an otherwise empty 120-byte frame."""
    data = ysf_frame.encode_dch(text)
    frame = bytearray(ysf_frame.FRAME_LENGTH)
    base = ysf_frame.SYNC_LENGTH + ysf_frame.FICH_LENGTH
    for i in range(ysf_frame.VCH_COUNT):
        at = base + ysf_frame.BLOCK_STRIDE * i
        frame[at:at + ysf_frame.DCH_LENGTH] = \
            data[ysf_frame.DCH_LENGTH * i:ysf_frame.DCH_LENGTH * (i + 1)]
    return bytes(frame) == expected


def check_header(csd1, csd2, expected):
    first = ysf_frame.encode_fr_data(csd1)
    second = ysf_frame.encode_fr_data(csd2)
    frame = bytearray(ysf_frame.FRAME_LENGTH)
    base = ysf_frame.SYNC_LENGTH + ysf_frame.FICH_LENGTH
    for i in range(ysf_frame.VCH_COUNT):
        at = base + ysf_frame.BLOCK_STRIDE * i
        frame[at:at + 9] = first[9 * i:9 * (i + 1)]
        frame[at + 9:at + 18] = second[9 * i:9 * (i + 1)]
    return bytes(frame) == expected


def main():
    fich_ok = fich_n = dch_ok = dch_n = hdr_ok = hdr_n = 0
    for line in open(VECTORS):
        parts = line.split()
        if parts[0] == "FICH":
            fich_n += 1
            fich_ok += check_fich([int(x) for x in parts[1:14]],
                                  bytes.fromhex(parts[14]))
        elif parts[0] == "DCH":
            dch_n += 1
            text = bytes.fromhex(parts[1]).decode("ascii")
            dch_ok += check_dch(text, bytes.fromhex(parts[2]))
        elif parts[0] == "HDR":
            hdr_n += 1
            hdr_ok += check_header(bytes.fromhex(parts[1]).decode("ascii"),
                                   bytes.fromhex(parts[2]).decode("ascii"),
                                   bytes.fromhex(parts[3]))

    print("FICH: %d/%d identical" % (fich_ok, fich_n))
    print("DCH:  %d/%d identical" % (dch_ok, dch_n))
    print("HDR:  %d/%d identical" % (hdr_ok, hdr_n))
    failed = (fich_ok != fich_n) or (dch_ok != dch_n) or (hdr_ok != hdr_n) \
        or not fich_n or not dch_n or not hdr_n
    print("FAIL" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
