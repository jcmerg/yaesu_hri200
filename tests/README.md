# Tests

`test_ysf_frame.py` compares the frame encoders in `hri200_ysf.py`
against the encoders in MMDVMHost, by way of urfd. `ysf_reference_vectors.txt` holds 400 vectors
produced by `reference_gen.cpp`, which calls `CYSFFICH::encode`,
`CYSFPayload::writeVDMode2Data` and `CYSFPayload::writeHeader` directly.
Every byte has to match.

`test_uplink.py` builds packets the way the gateway does and takes them
apart again, which catches a frame that is self-consistent but wrong.
The stronger check is `reference_check.cpp`: it runs urfd's own
`CYSFFICH::decode`, `processHeaderData` and `readVDMode2Data` over the
packets, which is what a reflector does with them.

`test_radio_frames.py` checks the CAT dialects in `hri200_proto.py`
without any hardware. The bridge makes the HRI-200 answer as a Kenwood,
a Yaesu or an Icom, and a nibble in the wrong place does not raise - it
tells the software the radio is on 14 MHz. So the encodings are compared
against values worked out by hand: Yaesu's big-endian BCD and its 10 Hz
escape above 100 MHz, Kenwood's 11-digit ASCII, and CI-V's
little-endian BCD, which is the reverse of Yaesu's and a classic source
of "the radio went to 5 MHz" bugs. Detection and the auto-detect
behaviour are covered there too.

`test_cat_server.py` drives the server over a pseudo terminal in all
four modes: Kenwood `ID;`, `TX;`, `FA;`, `SM0;` and a command split
across two reads; then the same port answering Yaesu 5-byte and CI-V
frames; then `auto` recognising each of them from the traffic. It needs
a POSIX pseudo terminal and is skipped on Windows.

`test_ysf_gui_smoke.py` builds both control panels and starts the YSF
gateway in dry-run mode against the loopback reflector, which exercises
the whole uplink path with no radio. Each panel is built in its own
process: Tk on macOS takes the interpreter down with it when a second
root is created, and a test that runs twice is easier to read than one
that fights that. Where there is no display the test says so and passes.

`test_node_id.py` needs neither urfd nor hardware: it rebuilds the node
identification frame from the capture byte for byte and checks the
checksum against all three captured frames, including the two taken
while the node was still connected to a WIRES-X room.

Both C++ programs need a checkout of urfd:

```
URFD=/path/to/urfd/reflector
g++ -std=c++17 -O1 -I$URFD -o gen tests/reference_gen.cpp \
    $URFD/YSFFich.cpp $URFD/YSFConvolution.cpp $URFD/CRC.cpp \
    $URFD/Golay24128.cpp $URFD/YSFPayload.cpp $URFD/Utils.cpp
./gen > tests/ysf_reference_vectors.txt

g++ -std=c++17 -O1 -I$URFD -o check tests/reference_check.cpp \
    $URFD/YSFFich.cpp $URFD/YSFConvolution.cpp $URFD/CRC.cpp \
    $URFD/Golay24128.cpp $URFD/YSFPayload.cpp $URFD/Utils.cpp
python3 tests/test_uplink.py --hex | ./check
```

Every line should read `fich=ok` with `DT=2`, and `header=1` or `dch=1`.
