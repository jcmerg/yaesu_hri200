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
