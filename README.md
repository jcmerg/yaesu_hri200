# hri200

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

**`hri200_ysf.py`** bridges a YSF reflector and the radio in C4FM, with no
WIRES-X in the path:

    YSF reflector =[UDP]= hri200_ysf.py → COM7 → HRI-200 → radio

See [YSF gateway](#ysf-gateway).

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

`hri200_ysf.py` links to a YSF reflector and carries traffic both ways. No
vocoder is involved and no AMBE licence question arises: what the
reflector sends and what the HRI-200 wants are the same bytes.

Downlink is a byte copy. Uplink has to put back the sync, FICH and DCH
the radio stripped; see [Building a YSF frame](#building-a-ysf-frame).

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
| `--node` | off | WIRES-X node number; switches on the identification burst |
| `--node-name` | `<call>-ND` | node name in that burst |
| `--node-city` | — | where the node is |
| `--gps` | off | pass the radio's position on to the reflector |
| `--dry-run` | off | print frames, open no port |

Header and terminator frames carry no voice and have to be kept out of
the stream in both directions. Rather than decode the FICH for it, the
gateway runs the triplet test described under [D1E](#d1e--voice-payload)
over the five blocks and passes only what survives. Voice passes at
100 %, everything else at the 25 % a coin toss gives: of 20000 random
blocks none were accepted, and neither were all-zero, all-one or
whitening-pattern blocks, while all 5100 blocks from the capture were.

The link is half duplex, so anything the radio reports while it is
transmitting is ignored.

### Position

A radio with GPS switched on reports where it is, in the tail of a `D1G`
frame. The gateway drops that unless `--gps` says otherwise, because
putting an operator on the map is their decision and not a default.

With the switch on it is a hand-off rather than a translation. The tail
is already shaped like the 10-byte fields the data channel carries — the
82-character frame holds one, padded with spaces, the 108-character one
holds two — so the fields are queued and go out in slot 6 in the order
they arrived. Nothing is decoded and nothing is reassembled, which
matters because how the longitude is packed is still unknown.

urfd fills that slot with a fixed byte string of its own. It has the
shape of a position taken from somebody's capture, so this does not copy
it: without `--gps` the slot carries the blank MMDVMHost uses for
unassigned fields.

### Building a YSF frame

The first half of `hri200_ysf.py` holds the encoders the uplink needs:
the FICH with its Golay blocks, CRC and convolutional code, the data
channel, and the header and terminator payloads. They sit in the same
file as the gateway on purpose — the tools here get copied to whatever
machine the HRI-200 is plugged into, and a script that needs a sibling
module is a script that breaks on arrival.

It is a port of MMDVMHost's encoders, and it is checked against them
rather than against a reading of them. 400 vectors generated by urfd's
own `CYSFFICH::encode`, `writeVDMode2Data` and `writeHeader` are replayed
and every byte has to match; all 400 do. The packets the gateway builds
are then fed back through urfd's `CYSFFICH::decode`, `processHeaderData`
and `readVDMode2Data`, which is what a reflector does with them: all 52
packets of a captured transmission come back `DT=2` with the callsigns
where they belong. See `tests/README.md`.

## Protocol notes

Reconstructed from serial captures of the original software. Anything
not backed by a capture is marked as unverified, both here and in the
source.

**Link:** 38400 baud, 8N1, no flow control, DTR and RTS asserted.

**Framing:** `0x01` (SOH) + ASCII command + `0x04` (EOT), in both
directions. Without the leading SOH the box discards the frame
silently — no reply, no error.

Replies follow a command by roughly 30 ms. The box also speaks on its
own while the radio is receiving — `D1P`, `D1H`, `D1R` and `D1G` all
arrived in a capture whose only outgoing traffic was `P010000` once a
second.

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
| `D1C0000` | status → `D1C000B0XXYY<radio ID>0` |
| `D1V0000` | radio type and firmware |
| `P010010` | shut down |

The five characters behind `YY` are the **radio ID of the set on RADIO
1**, not part of a constant tail: they have the same shape as the IDs the
read direction carries for other radios, `FDQTv` from a handheld and
`G0f4e` in the WIRES-X capture, and the same five turn up again in the
`D1V` identity reply. One box on the bench made them look fixed. The
gateway reads them out of the status reply and puts them in the `D1F`
header.

In the status reply the second digit of `YY` says what the radio is
doing — `0` idle, `1` receiving, `5` transmitting — and the first looks
like a phase within that: reception went `11` then `31` within 200 ms,
carrier and then digital sync, and a transmission runs `05`, `25`, `45`
and back down through `25` to `00`. The gateway names the ones both
captures show and prints any other value it meets, because a state
nobody has seen is worth more than a silent one. `00` is idle:
during actual reception it read `11` and then `31` within 200 ms, which
looks like carrier first and digital sync after, though only one capture
shows it. `XX` is a signal level and reaches at least `AA`; the 0..4 seen
earlier was the range of one quiet capture, not the field.

`D1P` carries the same body as the `D1C` reply and arrives unsolicited on
state changes, so polling `D1C0000` is not needed to follow the state —
but it is still what the idle traffic consists of. The original software
alternates `P010000` and `D1C0000`, once a second each and half a second
apart, from the end of the start-up handshake to the end of the session.
A gateway that sent only the PTT heartbeat had the radio fall back to the
WIRES-X start screen within seconds of coming up, so the box appears to
want the poll and not merely tolerate it.

The reply to `P010000` turns from `B0 0    0000000` into
`B1 0    0000000` while a signal is present.

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

### D1F — the transmission header

The radio strips sync, FICH and DCH on the way in and builds them again
on the way out, so the callsigns for an outgoing transmission have to be
handed over separately. `D1F` is where they go:

```
D1F 0052 <prefix, 8> <six 10-character fields> <counter, 2> <position, 12>
```

Field for field it is the write-direction twin of `D1G`, in the order the
YSF data channel carries them:

| Offset | Field | Write | Read |
| --- | --- | --- | --- |
| `0:8` | prefix | `21016000` | `21002000` |
| `8:18` | Dest | `28054G0f4e` | `*****FDQTv` |

| `18:28` | Src | `W9NJP-JIM` | `DL4JC` |
| `28:38` | Downlink | `DL4JC` | blank |
| `38:48` | Uplink | `W9CEQ` | blank |
| `48:58` | Rem1 | `9720193753` | blank |
| `58:68` | Rem2 | `28054G0f4e` | `     FDQTv` |
| `68:70` | flags | `09`, `0A` | `0C`, `0D`, `0F` |
| `70:82` | position | zeroed | zeroed |

Both directions put a five-character group in front of a five-character
radio ID in Dest, and repeat the ID in Rem2: a handheld calling CQ sends
`*****FDQTv`, the WIRES-X node sends its room number and `28054G0f4e`.
The gateway builds the same shape out of `*****` and the radio ID the
status reply gives it, and fills Src with the station the reflector
names, Downlink with itself and Uplink with the reflector's gateway.
Rem1 holds a node number in the capture and stays blank, which is what
the read direction shows for unassigned fields. What the two differing
prefix digits mean is unverified.

The two characters at `68:70` looked like a counter and are not one:
three transmissions in a row carried `09` before it moved to `0A`, and
the read direction never left `0C` to `0F`. Nothing in either capture
says what moves it, so the gateway sends the `09` the write direction
showed rather than a number of its own.

Order matters at the start of a transmission. The original sends the
header, then the first `D1E`, and only then `P100000` — the PTT follows
the voice rather than leading it — and repeats the header once after five
frames.

### The node identification

Ahead of a transmission the original software identifies the node:
`D1B00010`, a 250-character `D1F`, then `P110000`, which keys the radio
for a data burst rather than voice, and `P010000` about two seconds
later. The long frame carries the same six-field header as the short one
— node name and callsign where a transmission has Src and Downlink, the
node number in the two fields behind them — and then an ASCII-hex
payload:

```
]A_5 <number,5> <name,10> <city,14> <status,2>
     <room number,5> <room name,16> <count,3> <10 blank>
     <room city,14> <5> ETX <checksum>
```

The three captured frames say what the fields are, because the operator
left the room between them: status went from `05` to `02`, the count
from `123` to `000`, and everything naming the room went blank. That
last frame is the only form this gateway can honestly send, and
`tests/test_node_id.py` rebuilds it byte for byte.

The checksum is the sum of the header characters and of the payload
including its ETX, plus `0x29`. Where the constant comes from is not
known — but a one-character change in one frame and a wholesale change
in another both come out right, so it is an additive sum and not
something with more structure to it.

`--node` switches the burst on, because the number belongs to a
registered node and inventing one would put somebody else's identity on
the air. With it the gateway identifies five seconds after start-up and
every ten minutes it spends idle, never across a transmission.

### The read direction

What the box sends while the radio receives mirrors the write direction
one for one. Voice arrives as `D1R`, in the same frame as `D1E` down to
the length field, the constant `2`, the 16-bit counter and its step of
five:

```
D1R 0089 2 <counter, 4 hex digits> 00 <130 hex characters>
```

All 50 frames captured are 144 characters, all 250 blocks pass the
triplet test, and the counter steps by exactly five with no jitter across
both transmissions. Nothing about the payload differs from the write
direction, so the same 65 bytes go into a YSF frame the same way.

Two further frames come with it.

`D1H` carries the DCH callsign fields, one pair per frame:

```
D1H 001E 21002000 <slot> <10 characters> <10 characters>
```

`slot` is two digits, the first `1` to `3` and the second `0` to `2`,
which cycles through the fields the way the YSF data channel does. The
first field held `*****FDQTv` and the second `DL4JC     `, the
transmitting station. `FDQTv` is the radio ID of the
transmitting set.

`D1G` is the read-direction twin of `D1F`: the same `21002000` prefix,
five 10-character fields, then a tail with the radio ID, a counter and a
position field. The 108-character variant carries twelve bytes more than
the 82-character one, and those hold the position of the transmitting
station when it has GPS switched on. The low nibbles of the first five
are the latitude as decimal digits, degrees then minutes to one place,
which places a station within a few hundred metres. The longitude is in
the bytes that follow but does not come out of the same rule, so how it
is packed is still open.

**The whole field is zeroed in both captures.** Where the position ends
and a format marker begins is not settled: the four bytes ahead of it are
constant for one radio and different for another, which is what a coarse
position would also look like. Rather than argue it, all of it is treated
as position. That is the only redaction in either file; everything else
is as it came off the wire.

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
- The uplink has been verified against urfd's decoders but never against
  a radio: no reflector has yet heard a transmission through it.
- The downlink has not been heard on the air either. A radio keyed
  without a `D1F` header, and keyed ahead of the first voice frame,
  answered with **TX PROHIBIT**; sending the header first and the PTT
  behind the first frame is what the capture shows, but that it is the
  cure has not been confirmed on hardware.
- Whether `P110000` is specifically "key for data" or something wider is
  a reading of three occurrences, each following the node-information
  frame.
- The box acknowledges `D1F` with `D1F00010`, the same short form it
  answers `D1E` with, so a header it will not use looks exactly like one
  it will. What the radio does with the fields is unverified: a
  transmission carrying a correct `D1F`, radio ID and all, reached the
  other set with clean audio and no callsign on the display. Whether the
  identification burst is what the radio waits for before it fills the
  data channel is the open question `--node` exists to answer.
- The radio on the bench is an FTM-510D on experimental firmware, and
  the radio ID in the write-direction capture is not its own, so the
  capture and the hardware are not the same set. Where the two disagree,
  a firmware difference is as good an explanation as a misreading.
- What the `slot` digits in `D1H` count, and how the tail of `D1G` is
  laid out, is only sketched. The gateway takes the callsign from the
  second field and ignores the rest.
- The byte at offset 34 of a YSFD packet is a frame counter that the
  reflectors seen here do not check; this sends what urfd sends.
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

### Probing the read direction

`tools/rxprobe.py` listens to the HRI-200 and logs everything it sends,
which is the capture the read direction still needs. Start it, then key a
second radio on the node frequency in C4FM.

```
python3 tools/rxprobe.py --hri COM7 --freq 144.85 --out rx.txt
```

It assumes nothing about how a receive frame is laid out. Every run of
hex characters in every frame is cut into 65-byte windows at each offset
and put through the triplet test, so voice is found whatever command
letter, counter or header the box wrapped it in — and the offset it sat
at is reported, which is the layout a gateway would need. On 300 random
400-character hex runs, about 42000 windows, nothing was mistaken for
voice, and neither were the `D1C` status reply or the `D1F` node frame.

If nothing arrives at all, `--poll` asks with `D1C0000` at 10 Hz instead
of idling at 1 Hz. Whether the box pushes voice or waits to be asked is
open: the README says the device never transmits unsolicited, but `D1P`
frames contradict that, and nobody transmitted on the node frequency
while the existing captures were taken.

## Contributing

Protocol gaps are best closed the same way the rest was: capture the
original software with a serial monitor while changing exactly one
setting, and diff the `IRP_MJ_WRITE` lines. Captures in plain text are
far more useful than binary formats.

## Licence

MIT
