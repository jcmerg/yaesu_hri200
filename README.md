# hri200-cat

Use a **Yaesu HRI-200** as a generic sound card + PTT interface for
digital modes, by making it look like a Kenwood TS-2000 to CAT-aware
software.

The HRI-200 is Yaesu's WIRES-X interface box. It contains a USB audio
device and a serial control channel, but it speaks only Yaesu's own
protocol and is unusable outside the WIRES-X software as shipped. This
project documents that protocol and provides a single-file bridge.

    fldigi / WSJT-X → flrig → COM10 =[com0com]= COM11 → hri200_cat.py
                                                     → COM7 → HRI-200 → radio

Audio is untouched: the HRI-200 registers as a standard USB audio
device, so pick it directly in your application.

## Requirements

- Python 3.6+
- [pyserial](https://pypi.org/project/pyserial/)
- A virtual null-modem pair, e.g. [com0com](https://sourceforge.net/projects/com0com/)
- The HRI-200 and a compatible radio on RADIO 1

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
D1M <length, 4 hex digits> 4000 <RX block, 32> <TX block, 32> F
```

The length field is the number of characters that follow it; a single
character too many and the radio discards the frame without a reply.

Block layout (32 characters):

| Offset | Field | Notes |
| --- | --- | --- |
| `0:9` | frequency | `144.85000` |
| `9:19` | offset | `+000.00000`; `-` means reverse |
| `19` | narrow | `0` wide, `1` narrow |
| `20` | squelch | `1` none, `2` CTCSS, `3` DCS |
| `21:24` | tone | `077` = 77.0 Hz, `254` = 254.1 Hz |
| `24:27` | DCS code | `023`, `754`; retained when DCS is off |
| `27:30` | — | constant `000` |
| `30` | power | RX block: `0` high, `1` mid, `2` low |
| `31` | — | constant `0` |

Verified by capture comparison: frequency, power, narrow deviation,
CTCSS up to 254.1 Hz, DCS with two codes, reverse, and a 1 MHz offset.

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
- Whether the DCS code and the CTCSS tone can be set independently is
  untested; they were never changed at the same time.
- Whether `D1M` actually retunes the radio, as opposed to only
  informing the interface, has not been confirmed.
- The `SM` reading is a coarse four-step indicator, not an S-meter.
  Power output, SWR and ALC are not available from the hardware.
- Node registration is tied to the serial number on Yaesu's servers,
  so this cannot be used to join the real WIRES-X network — nor is it
  intended to.

## Contributing

Protocol gaps are best closed the same way the rest was: capture the
original software with a serial monitor while changing exactly one
setting, and diff the `IRP_MJ_WRITE` lines. Captures in plain text are
far more useful than binary formats.

## Licence

MIT
