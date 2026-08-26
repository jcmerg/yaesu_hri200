# hri200-cat

The **Yaesu HRI-200** is the WIRES-X interface box. It contains a USB
audio device and a serial control channel, but it speaks only Yaesu's own
protocol and is unusable outside the WIRES-X software as shipped. This
project documents that protocol and provides two bridges over it.

**`hri200_cat.py`** makes the box a generic sound card and PTT interface
for digital modes, by making it look like a Kenwood TS-2000 to CAT-aware
software:

    fldigi / Direwolf → flrig → COM10 =[com0com]= COM11 → hri200_cat.py
                                                       → COM7 → HRI-200 → radio

Audio is untouched: the HRI-200 registers as a standard USB audio
device, so pick it directly in your application.

The radio on RADIO 1 is an FM set, so this covers what works over FM:
packet and APRS, SSTV, the fldigi modes. There is no SSB in the path,
and the weak-signal modes that assume it do not belong here.

**`hri200_ysf.py`** carries a YSF reflector to the radio in C4FM, with no
WIRES-X in the path:

    YSF reflector =[UDP]= hri200_ysf.py → COM7 → HRI-200 → radio

It works in one direction only; see [YSF gateway](#ysf-gateway).

## Requirements

- Python 3.6+
- [pyserial](https://pypi.org/project/pyserial/)
- The HRI-200 and a compatible radio on RADIO 1
- For `hri200_cat.py` only: a virtual null-modem pair, e.g.
  [com0com](https://sourceforge.net/projects/com0com/)

## Usage

```
pip install pyserial
python hri200_cat.py COM11 --hri COM7 --freq 144.85 --power mid -v
```

In flrig: **Rig = TS-2000**, **Port = COM10** (the other end of the
pair). The baud rate does not matter. `-v` prints every CAT command
with its reply, which is useful the first time flrig initialises.

| Option | Default | Meaning |
| --- | --- | --- |
| `catport` | — | virtual port this script listens on |
| `--hri` | `COM7` | HRI-200 serial port |
| `--freq` | `144.85` | frequency in MHz |
| `--shift` | `0.0` | offset in MHz |
| `--power` | `mid` | `high`, `mid` or `low` |
| `--tone` | `88.5` | CTCSS frequency in Hz |
| `--sql` | `none` | `none`, `tone` (CTCSS) or `dcs` |
| `--narrow` | off | narrow deviation |
| `--line-ptt` | `rts` | `off`, `rts`, `dtr` or `both` |
| `--catbaud` | `38400` | baud rate of the emulated CAT port |

### PTT paths

Either or both may be used; the radio keys as soon as one of them asks
for it.

- **CAT** — set flrig to "PTT via CAT", which sends `TX;` and `RX;`.
  Works regardless of whether the virtual port pair passes modem
  control lines.
- **Control line** — `--line-ptt rts` (default). The application's RTS
  arrives here as CTS, its DTR as DSR. com0com wires it that way;
  verify before relying on it with other virtual port drivers. The
  script prints the initial line state at start-up and warns if a line
  is already asserted.

## YSF gateway

`hri200_ysf.py` links to a YSF reflector and pushes what arrives into the
radio in C4FM. No vocoder is involved and no AMBE licence question
arises: what the reflector sends and what the HRI-200 wants are the same
bytes, and the gateway only moves them.

```
python3 hri200_ysf.py --hri COM7 --call DL4JC \
    --host ysf.example.org --port 42000 --freq 144.85
```

`--dry-run` skips the serial port and prints the frames instead, which
needs neither the hardware nor pyserial.

| Option | Default | Meaning |
| --- | --- | --- |
| `--call` | — | your callsign, sent to the reflector |
| `--host` / `--port` | — | the reflector |
| `--hri` | `COM7` | HRI-200 serial port |
| `--freq` | `144.85` | frequency in MHz |
| `--shift` | `0.0` | offset in MHz |
| `--power` | `mid` | `high`, `mid` or `low` |
| `--dgid` | `0` | DG-ID 0..99 |
| `--prefill` | `5` | frames buffered before playout starts |
| `--dry-run` | off | print frames, open no port |

**Only the reflector reaches the radio.** Nothing goes back. Building the
other direction needs a capture of what the HRI-200 sends to the PC when
someone talks on the node frequency, and no such capture exists — see
[Known gaps](#known-gaps).

Header and terminator frames carry no voice and have to be kept out of
the stream. Rather than decode the FICH for it, the gateway runs the
triplet test described under [D1E](#d1e--voice-payload) over the five
blocks and passes only what survives. Voice passes at 100 %, everything
else at the 25 % a coin toss gives: of 20000 random blocks none were
accepted, and neither were all-zero, all-one or whitening-pattern
blocks, while all 5100 blocks from the capture were.

## Protocol notes

Reconstructed from serial captures of the original software. Anything
not backed by a capture is marked as unverified, both here and in the
source.

**Link:** 38400 baud, 8N1, no flow control, DTR and RTS asserted.

**Framing:** `0x01` (SOH) + ASCII command + `0x04` (EOT), in both
directions. Without the leading SOH the box discards the frame
silently — no reply, no error.

The device never transmits unsolicited. Every reply follows a command
by roughly 30 ms.

**Cold start.** After opening the port the original software waits
about seven seconds (6.90 s and 7.08 s in two captures) before the
first command. Anything sent earlier is ignored silently.

```
M00        → M00
R6423      → R<hex>      device identity, ASCII-hex CSV
P010000    → B0 0    0000000
D1V0000    → D1V0030<radio type and firmware>
D1M<...>   → echo        channel configuration
D1B00010   → D1B00010
```

**Operation.**

| Command | Meaning |
| --- | --- |
| `P100000` | PTT on — repeat at 1 Hz while the state holds |
| `P010000` | PTT off — likewise |
| `D1C0000` | status → `D1C000B0XXYYFD2wr0` |
| `D1V0000` | radio type and firmware |
| `P010010` | shut down |

In the status reply `YY` ending in `5` means transmitting and `00`
means receiving. `XX` is an RX level, observed range 0..4 per digit.

`D1P` frames arrive unsolicited on state changes and can be used
instead of polling `D1C0000`.

### D1M — frequency and channel command

```
D1M <length, 4 hex digits> <mode, 4> <RX block, 32> <TX block, 32> F
```

The length field is the number of characters that follow it; a single
character too many and the radio discards the frame without a reply.

The mode field read `4000` while the node ran in FM and `7000` after the
operator switched it to digital. Position `31` of the TX block moved from
`0` to `1` in the same step, so the two cannot be told apart from a single
transition.

Block layout (32 characters):

| Offset | Field | Notes |
| --- | --- | --- |
| `0:9` | frequency | `144.85000` |
| `9:19` | offset | `+000.00000`; `-` means reverse |
| `19` | narrow | `0` wide, `1` narrow |
| `20` | squelch | `1` none, `2` CTCSS, `3` DCS |
| `21:24` | tone | `077` = 77.0 Hz, `254` = 254.1 Hz |
| `24:27` | DCS code | `023`, `754`; retained when DCS is off |
| `27:30` | DG-ID | RX block, as hex: `000` = 0, `063` = 99 |
| `30` | power | RX block: `0` high, `1` mid, `2` low |
| `31` | digital | TX block: `0` FM, `1` digital |

Verified by capture comparison: frequency, power, narrow deviation,
CTCSS up to 254.1 Hz, DCS with two codes, reverse, and a 1 MHz offset.
The DG-ID rests on a single observed change, 99 to `063` and back, which
is consistent with hex but not yet distinguishable from other encodings
that agree on that one value. The TX block carries its own CTCSS and DCS
fields, which held different values from the RX block throughout, so the
two are not mirrored.

### D1E — voice payload

```
D1E 0089 2 <counter, 4 hex digits> 00 <130 hex characters>
```

`0089` is the usual length field, 137 characters following it. The
counter advances by exactly 5 per frame and restarts at the beginning of
a transmission; frames arrive every 100 ms, so it counts 20 ms voice
subframes rather than frames or bytes. Positions 12 and 13 read `00` in
1017 of 1020 observed frames and `40` or `C0` in the remaining three,
which is unexplained.

The 130 hex characters decode to 65 bytes, and those 65 bytes are the
five VCH sections of a **YSF V/D mode 2 frame**, on-air form, 13 bytes
each, in order. Nothing else: sync, FICH and the DCH data channel have
already been stripped by the radio, which is why the callsigns arrive
separately over `D1F`.

This is not an inference from the size. After de-interleaving with
`INTERLEAVE_TABLE_26_4` and removing the whitening, bits `0..80` of every
block resolve into 27 triplets that carry each data bit three times, as
the V/D mode 2 FEC requires. Across 5100 blocks all 137,700 triplets are
internally consistent, against a 25 % baseline for unrelated data. Any
other interleave or whitening order scores at the baseline.

Moving between the two is therefore a byte copy, with no vocoder
involved. Measured against the 120-byte air frame, which starts at offset
35 of a 155-byte YSFD packet:

```
frame[35 + 18*i : 48 + 18*i]  ==  payload[13*i : 13*i + 13]    for i in 0..4
```

Coming from a reflector, the other 55 bytes — 5 sync, 25 FICH, and the 5
bytes of DCH ahead of each block — are simply dropped, which is all
`hri200_ysf.py` does. Going the other way they have to be generated, and
the callsigns for the DCH would come from `D1F`.

### R6423 — device identity

`R` + one format digit + ASCII-hex of a CSV line:

```
00000,00000,XXXXXXXX,20231120153554
```

Field 2 is the serial number the original software shows as
unchangeable. Field 3 looks like a manufacturing timestamp. The two
leading fields were empty on the device used here.

## Known gaps

- Position `30` of the **TX** block never changed across 21 observed
  configuration changes and stays at `2`. In the RX block it is the
  power setting.
- Position `27:30` of the **TX** block stayed `000` while the RX block
  carried the DG-ID, so whether the two can differ is untested.
- No capture so far contains the read direction, HRI-200 to PC. Every
  frame recovered from the `.dmslog8` logs is a write. Without it the
  path from the radio into a YSF network cannot be built.
- Whether the DCS code and the CTCSS tone can be set independently is
  untested; they were never changed at the same time.
- Whether `D1M` actually retunes the radio, as opposed to only
  informing the interface, has not been confirmed.
- The `SM` reading is a coarse four-step indicator, not an S-meter.
  Power output, SWR and ALC are not available from the hardware.
- Node registration is tied to the serial number on Yaesu's servers,
  so this cannot be used to join the real WIRES-X network — nor is it
  intended to.

## Reading captures

`tools/dmslog.py` pulls the frames out of a Device Monitoring Studio
`.dmslog8`. The container is proprietary, but the captured bytes sit in
it verbatim, so the SOH/EOT framing can simply be scanned for.

```
python3 tools/dmslog.py capture.dmslog8 --only D1M
```

Records appear twice, as IRP request and completion, and identical
adjacent frames are collapsed.

## Contributing

Protocol gaps are best closed the same way the rest was: capture the
original software with a serial monitor while changing exactly one
setting, and diff the `IRP_MJ_WRITE` lines. Captures in plain text are
far more useful than binary formats.

## Licence

MIT
