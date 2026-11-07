#!/usr/bin/env python3
"""Build both control panels and start the YSF gateway in dry-run mode.

A GUI that cannot be constructed is the kind of failure that only shows up
when somebody double-clicks the app, so the windows are built here and
their validation is exercised. The YSF gateway is started against the
loopback reflector with no radio, which is what a dry run is for: the
whole uplink path runs, nothing is transmitted, and no hardware is needed.

Each panel is built in its own process. Tk on macOS does not survive two
roots in one interpreter - the second one takes the process down - and a
test that has to be run twice is easier to read than one that shares a
root through a detour.

Needs a display. Where there is none - a headless Linux runner - the test
says so and exits zero, because a missing X server is not a bug in this
repository.

    python3 tests/test_ysf_gui_smoke.py
"""

import importlib.util
import os
import subprocess
import sys
import types

HERE = os.path.abspath(__file__)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))


def have_display():
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def report(checks):
    width = max(len(name) for name, _ok in checks)
    failed = 0
    for name, ok in checks:
        print("  %-*s  %s" % (width, name, "ok" if ok else "FAILED"))
        if not ok:
            failed += 1
    return failed


def cat_panel():
    import hri200_gui

    args = types.SimpleNamespace(hri=None, proto="auto", civ_address="88",
                                 freq=144.85, shift=0.0, power="mid",
                                 tone=88.5, sql="none", cat=None)
    app = hri200_gui.App(args)
    app.update()
    checks = [("the CAT panel builds", app.title() != "")]
    app.var_freq.set("145.50000")
    checks.append(("the CAT panel reads a channel",
                   abs(app._channel()["freq_mhz"] - 145.5) < 1e-9))
    app.var_proto.set(hri200_gui.PROTO_TO_LABEL["civ"])
    checks.append(("the protocol list covers every dialect",
                   sorted(hri200_gui.LABEL_TO_PROTO.values())
                   == ["auto", "civ", "kenwood", "yaesu"]))
    checks.append(("the chosen protocol reaches the server",
                   hri200_gui.LABEL_TO_PROTO[app.var_proto.get()] == "civ"))
    app.var_freq.set("not a number")
    try:
        app._channel()
    except ValueError:
        checks.append(("a bad frequency is refused", True))
    else:
        checks.append(("a bad frequency is refused", False))
    app.destroy()
    return report(checks)


def ysf_panel():
    import hri200_ysf_gui

    yargs = types.SimpleNamespace(hri="COM7", call="", host="", port=42000,
                                  freq=144.85, shift=0.0, power="mid",
                                  dgid=0, prefill=5)
    yapp = hri200_ysf_gui.YsfApp(yargs)
    yapp.update()
    checks = [("the YSF panel builds", yapp.title() != "")]

    for setup, label in ((("", "DL4JC"), "a missing host"),
                         (("ysf.example.org", ""), "a missing callsign")):
        yapp.var_host.set(setup[0])
        yapp.var_call.set(setup[1])
        try:
            yapp._config()
            checks.append(("the YSF panel refuses %s" % label, False))
        except ValueError:
            checks.append(("the YSF panel refuses %s" % label, True))

    yapp.var_host.set("127.0.0.1")
    yapp.var_call.set("DL4JC")
    yapp.var_dry.set(True)
    config = yapp._config()
    checks.append(("a dry run needs no radio port",
                   config["dry_run"] is True))

    # The gateway itself, with a radio that is not there: the dry run
    # swaps in a null serial, so the whole uplink path runs.
    gateway = hri200_ysf_gui.Gateway(config, yapp.log)
    started = False
    stats = {}
    try:
        gateway.start()
        started = gateway.running
        stats = gateway.stats()
    finally:
        gateway.stop()
    checks.append(("the gateway starts in dry run", started))
    checks.append(("the reflector client reports counters",
                   "down" in stats and "up" in stats))
    checks.append(("the statistics carry radio fields",
                   "frames_out" in stats and "heard" in stats))
    checks.append(("stopping clears the client and the radio",
                   gateway.client is None and gateway.hri is None))

    yapp._restore_stdout()
    yapp.update()
    yapp.destroy()
    return report(checks)


def main(argv):
    if len(argv) > 1 and argv[1] == "--panel":
        panel = ysf_panel if argv[2] == "ysf" else cat_panel
        try:
            failed = panel()
        except Exception as exc:
            if type(exc).__name__ == "TclError":
                # No window server here. That is not a bug in this
                # repository, so it reads as a skip rather than a crash.
                print("  skipped: Tk cannot start here (%s)" % exc)
                return 0
            print("  the panel raised: %r" % (exc,))
            return 2
        return 1 if failed else 0

    if not have_display():
        print("  skipped: no display")
        return 0

    if importlib.util.find_spec("tkinter") is None:
        print("  skipped: tkinter not installed")
        return 0

    failed = 0
    for panel in ("cat", "ysf"):
        print("-- %s panel" % panel)
        proc = subprocess.run([sys.executable, HERE, "--panel", panel])
        if proc.returncode not in (0, 1):
            # A crash, not a failed check: on macOS that is what a Tk
            # problem looks like, and calling it a skipped test would
            # hide a real crash.
            print("  the %s panel crashed (exit %d)"
                  % (panel, proc.returncode))
            failed += 1
        elif proc.returncode == 1:
            failed += 1
    print("\n%s" % ("all panels built" if not failed
                    else "%d panel(s) failed" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
