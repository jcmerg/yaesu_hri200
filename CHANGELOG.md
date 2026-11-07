# Changelog

All notable changes to this project. The version numbers match the
release tags; `packaging/make_dist.py` puts the same number in the
archive names.

## 2.0 — 2026-11-07

The right idea, this time. 1.0 and 1.1 went the wrong way round: they
drove *other* radios over their own CAT ports and left the HRI-200 as one
option among several. That is backwards. The box is the radio - it is
the thing with the sound card and the PTT - and what has to vary is what
the software on the other side is told it is talking to.

### Changed

- **`hri200_proto.py` replaces `hri200_radio.py`.** Three CAT dialects
  are now emulated *on the virtual port*, all driven by the same HRI-200:

  | `--proto` | Dialect | Emulates |
  | --- | --- | --- |
  | `kenwood` | ASCII, `FA00014485000;` | TS-2000 profile |
  | `yaesu` | 5-byte binary | FT-817/857 profile |
  | `civ` | Icom CI-V, `FE FE .. FD` | IC-706/7300-shaped |
  | `auto` | picks from the traffic | — |

- **`--proto`, `--civ-address` and `--yaesu-unit`** replace `--radio` and
  `--radiobaud`. `--proto` defaults to `auto`.
- **Auto-detection.** CI-V frames start with `0xFE`, Kenwood commands are
  ASCII letters, and a Yaesu frame is five bytes of neither, so the three
  cannot be confused. Nothing is sent until the dialect is known: a wrong
  guess is a stream of bytes the other end will not understand, while
  silence costs one timeout and a retry.
- The CAT server class is now `CatServer` and takes a protocol object.
  Its `pump()` and `serve()` remain, so the GUI still drives it without
  owning a thread.
- The control panel offers the dialect as a dropdown instead of a radio
  type, and opens the CAT port as a separate step from the box.

### Notes

- `hri200_radio.py` and `tests/test_radio_serial.py` are gone. What was
  worth keeping from them - the encodings and the checks that pin them -
  lives in `hri200_proto.py` and `tests/test_radio_frames.py`.
- The Yaesu frequency field still has four BCD bytes, so above 100 MHz it
  is written in 10 Hz steps; `--yaesu-unit` overrides the guess.

## 1.1 — 2026-11-07

### Added

- **`hri200_ysf_gui.py`** — control panel for the YSF gateway: reflector,
  callsign, channel, DG-ID, the optional node identification and GPS
  pass-through, and a status line with what arrives from the reflector
  and what goes out to the radio. A dry-run switch skips the serial port
  so the reflector side can be tried without hardware.
- The YSF panel ships as a second application:
  `HRI-200 YSF gateway.app` on macOS, `hri200-ysf-gui` elsewhere, with
  its own `.desktop` entry.
- `tests/test_ysf_gui_smoke.py` builds both panels and runs the gateway
  in dry-run mode against the loopback reflector. Each panel is built in
  its own process, because Tk on macOS does not survive two roots in one
  interpreter.

### Changed

- Every archive now holds a single top folder, the same name on all three
  platforms, so unpacking never scatters files into the current
  directory. The macOS archive is built by running `ditto` on that
  folder, which keeps the symlinks in both bundles intact.
- The macOS packaging check in CI verifies the signature of every app in
  the unpacked archive, not just the bridge.

## 1.0 — 2026-11-07

First release with a packaged application. Until now this was a set of
scripts you ran from a checkout; now it is also a program you can hand
to somebody who does not have Python.

### Added

- **`hri200_radio.py`** — one interface over four radio backends, so the
  bridge no longer requires the WIRES-X box:
  - `hri200`: the box, unchanged protocol
  - `yaesu`: 5-byte binary CAT, FT-817/818/857/897
  - `kenwood`: ASCII CAT, TS-2000/FT-991/FTDX
  - `icom`: CI-V, IC-706/7100/7300/9700
- **`hri200_gui.py`** — Tkinter control panel: backend and port
  selection, channel controls, hold-to-talk, S-meter, live frame log,
  and the optional TS-2000 server for flrig.
- **`--radio`, `--radiobaud`, `--civ-address`** in `hri200_cat.py`. The
  default path is unchanged.
- **`packaging/`** — PyInstaller spec, a dependency-free icon
  generator (PNG, ICO, ICNS), and `make_dist.py`, which turns the build
  into the archive the platform expects:
  - Windows: `hri200-gui.exe` plus the console bridge, in a zip
  - Linux: both binaries, a `.desktop` file and `install.sh`, in a tarball
  - macOS: `HRI-200 bridge.app` plus the console bridge, in a zip
- **`.github/workflows/build.yml`** — tests and builds on Linux, macOS
  (arm64 and x86_64) and Windows; tagged pushes attach the archives to
  a release.
- **`Makefile`** — `test`, `icons`, `app`, `dist`, `run`, `ports`, `clean`.
- Tests that need no hardware: `tests/test_radio_frames.py` (37 checks
  on the CAT encodings), `tests/test_cat_server.py` (14 checks driving
  the TS-2000 server over a pseudo terminal), and
  `tests/test_radio_serial.py` (13 checks running a backend against a
  pretend Kenwood).

### Changed

- `hri200_cat.TS2000` grew a `pump()` method, so the server can be
  driven by a caller that owns its own event loop. `serve()` is now a
  loop around it. Behaviour is unchanged.
- The serial read in `SerialRadio` drains until the line goes quiet
  instead of stopping at a byte count. A count limit cut buffered
  answers in half and left the remainder for the next poll, which made
  the meter lag by one reading.
- Writes no longer call `flush()`. Flushing waits for the line to
  drain, which never returns if the far end stops reading; a write
  timeout now bounds it instead.

### Known limits

- The Yaesu 5-byte dialect has four BCD bytes for the frequency, which
  in Hz stops below 100 MHz. Above that the field is treated as 10 Hz
  steps. Which one a given radio expects has to be settled on the
  radio, not in this repository.
- Kenwood power is a convention: `high`, `mid` and `low` become 50, 20
  and 5 W. It is not read back from the radio.
- Icom has no power control that all models share, so the field is
  ignored rather than guessed at.
- The `hri200` backend keeps its seven-second warm-up; the others do
  not wait.
- The macOS archive is made with `ditto`, not `zipfile`. Python's
  zipfile cannot store a symlink, and the framework inside an app bundle
  is a tree of them: the archive unpacked into a bundle with a broken
  code signature, which macOS reports as "damaged". Extended attributes
  are left out for the same reason - they reappear as `._name` files
  inside the signed bundle.
- The Linux archives are built on Ubuntu 22.04 and need glibc 2.35 or
  newer. A binary links against the C library it was built with; built
  on the newest runner it would only start on the newest distributions.

## Earlier work

Before 1.0 the repository was the protocol documentation and two
scripts, and the history is the story of that:

- the HRI-200 serial protocol, reconstructed from captures: framing,
  cold start, `D1M`/`D1C`/`D1V`/`D1B`, the `D1E` voice payload
- the YSF gateway, including the FICH, data-channel and header
  encoders, checked against MMDVMHost by way of urfd
- the node identification burst, rebuilt byte for byte from three
  captured frames

`git log` is the better record of that; nothing here needs repeating.
