#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hri200_gui.py - a small control panel for the HRI-200 bridge

Everything the command line tool does, with a knob to turn: open the
box, set the frequency, watch the meter, key the transmitter, and offer a
CAT port in whichever dialect the software on the other side expects.

The HRI-200 is the radio. What the emulated CAT port answers as is a
choice - Kenwood, Yaesu, Icom, or left to auto-detection - and it can be
switched while the bridge is running.

Tkinter is in the Python standard library on Windows and macOS, and
comes with python3-tk on Linux. Nothing else is needed beyond pyserial.

    python3 hri200_gui.py
    python3 hri200_gui.py --hri /dev/ttyUSB0 --proto kenwood --freq 145.5

The window is deliberately plain: every control maps to exactly one
thing the box does, and the log shows the bytes that were exchanged, so
a wrong port or a silent box is visible rather than mysterious.
"""

import sys

if sys.version_info < (3, 6):
    sys.exit("Python 3.6 or newer required")

import argparse
import queue
import threading

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:                                        # pragma: no cover
    sys.exit("Tkinter is missing. On Debian/Ubuntu:  apt install python3-tk\n"
             "On macOS with Homebrew:  brew install python-tk")

try:
    import serial                                              # noqa: F401
except ImportError:                                        # pragma: no cover
    serial = None

import hri200_cat


PROTO_CHOICES = [
    ("auto", "auto-detect"),
    ("kenwood", "Kenwood TS-2000 (ASCII)"),
    ("yaesu", "Yaesu FT-817/857 (5-byte)"),
    ("civ", "Icom CI-V"),
]
LABEL_TO_PROTO = {label: name for name, label in PROTO_CHOICES}
PROTO_TO_LABEL = {name: label for name, label in PROTO_CHOICES}

CTCSS = [67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
         94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0,
         127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 162.2, 167.9,
         173.8, 179.9, 186.2, 192.8, 203.5, 210.7, 218.1, 225.7, 233.6,
         241.8, 250.3, 254.1]


class PttButton(tk.Button):
    """A push-to-talk button: keyed while the button is held down.

    A keyboard press, a mouse press and a lost focus all have to end in
    the same state, otherwise a stuck carrier is one Alt-Tab away.
    """

    def __init__(self, master, on_key, on_release, **kw):
        tk.Button.__init__(self, master, **kw)
        self._on_key = on_key
        self._on_release = on_release
        self._held = False
        self.defbg = "#e0e0e0"
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<KeyPress-space>", self._press)
        self.bind("<KeyRelease-space>", self._release)
        self.bind("<FocusOut>", self._release)
        self.bind("<Leave>", self._release)

    def _press(self, _event=None):
        if self._held:
            return "break"
        self._held = True
        self.configure(relief="sunken", bg="#c62828", fg="white")
        self._on_key()
        return "break"

    def _release(self, _event=None):
        if not self._held:
            return "break"
        self._held = False
        self.configure(relief="raised", bg=self.defbg, fg="black")
        self._on_release()
        return "break"


class SMeter(tk.Canvas):
    """A fifteen-segment bar; the box only ever knows 0..30."""

    SEGMENTS = 15

    def __init__(self, master, **kw):
        kw.setdefault("height", 18)
        kw.setdefault("width", 300)
        tk.Canvas.__init__(self, master, **kw)
        self.value = 0
        self.bind("<Configure>", lambda _e: self.redraw())

    def set(self, value):
        value = max(0, min(30, int(value)))
        if value != self.value:
            self.value = value
            self.redraw()

    def redraw(self):
        self.delete("all")
        width = max(60, self.winfo_width())
        height = max(12, self.winfo_height())
        step = width / float(self.SEGMENTS)
        lit = int(round(self.value / 30.0 * self.SEGMENTS))
        for i in range(self.SEGMENTS):
            x0 = i * step + 1
            x1 = (i + 1) * step - 1
            if i < lit:
                colour = "#43a047" if i < 9 else ("#fb8c00" if i < 12
                                                  else "#e53935")
            else:
                colour = "#e0e0e0"
            self.create_rectangle(x0, 2, x1, height - 2, fill=colour,
                                  outline="")


class App(tk.Tk):
    def __init__(self, args):
        tk.Tk.__init__(self)
        self.title("HRI-200 bridge")
        self.minsize(720, 640)

        self.hri = None
        self.cat = None
        self.cat_thread = None
        self.cat_running = False
        self.worker = queue.Queue()
        self.log_queue = queue.Queue()
        self.args = args

        self.var_port = tk.StringVar(value=args.hri or "")
        self.var_freq = tk.StringVar(value="%.5f" % args.freq)
        self.var_shift = tk.StringVar(value="%.5f" % args.shift)
        self.var_power = tk.StringVar(value=args.power)
        self.var_tone = tk.StringVar(value="%.1f" % args.tone)
        self.var_sql = tk.StringVar(value=args.sql)
        self.var_narrow = tk.BooleanVar(value=False)
        self.var_catport = tk.StringVar(value=args.cat or "")
        self.var_proto = tk.StringVar(value=PROTO_TO_LABEL.get(
            args.proto, PROTO_CHOICES[0][1]))
        self.var_addr = tk.StringVar(value=args.civ_address)
        self.var_status = tk.StringVar(value="not connected")

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._drain)
        self.after(300, self._refresh_meter)

    # -- layout -------------------------------------------------------
    def _build(self):
        pad = {"padx": 6, "pady": 4}

        box = ttk.LabelFrame(self, text="HRI-200")
        box.pack(fill="x", **pad)
        ttk.Label(box, text="Serial port").grid(row=0, column=0,
                                                sticky="w", padx=6, pady=4)
        self.cmb_port = ttk.Combobox(box, textvariable=self.var_port,
                                     width=28)
        self.cmb_port.grid(row=0, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(box, text="Scan", width=6,
                   command=self.scan_ports).grid(row=0, column=2, padx=2)
        self.btn_connect = ttk.Button(box, text="Open", width=10,
                                      command=self.toggle_connect)
        self.btn_connect.grid(row=0, column=3, padx=6)
        ttk.Label(box, textvariable=self.var_status).grid(
            row=1, column=0, columnspan=4, sticky="w", padx=6)
        box.columnconfigure(1, weight=1)

        chan = ttk.LabelFrame(self, text="Channel")
        chan.pack(fill="x", **pad)
        ttk.Label(chan, text="Frequency (MHz)").grid(row=0, column=0,
                                                     sticky="w", padx=6,
                                                     pady=4)
        ttk.Entry(chan, textvariable=self.var_freq, width=14).grid(
            row=0, column=1, sticky="w")
        ttk.Label(chan, text="Offset (MHz)").grid(row=0, column=2,
                                                  sticky="e", padx=6)
        ttk.Entry(chan, textvariable=self.var_shift, width=10).grid(
            row=0, column=3, sticky="w")

        ttk.Label(chan, text="Power").grid(row=1, column=0, sticky="w",
                                           padx=6, pady=4)
        ttk.Combobox(chan, textvariable=self.var_power, state="readonly",
                     width=8,
                     values=["high", "mid", "low"]).grid(row=1, column=1,
                                                         sticky="w")
        ttk.Label(chan, text="Squelch").grid(row=1, column=2, sticky="e",
                                             padx=6)
        ttk.Combobox(chan, textvariable=self.var_sql, state="readonly",
                     width=8,
                     values=["none", "tone", "dcs"]).grid(row=1, column=3,
                                                          sticky="w")

        ttk.Label(chan, text="CTCSS (Hz)").grid(row=2, column=0, sticky="w",
                                                padx=6, pady=4)
        ttk.Combobox(chan, textvariable=self.var_tone, width=10,
                     values=["%.1f" % t for t in CTCSS]).grid(
            row=2, column=1, sticky="w")
        ttk.Checkbutton(chan, text="narrow", variable=self.var_narrow).grid(
            row=2, column=2, columnspan=2, sticky="w", padx=6)

        ttk.Button(chan, text="Apply", command=self.apply_channel).grid(
            row=3, column=0, columnspan=2, sticky="we", padx=6, pady=6)

        key = ttk.LabelFrame(self, text="PTT and meter")
        key.pack(fill="x", **pad)
        self.btn_ptt = PttButton(key, self.ptt_on, self.ptt_off,
                                 text="PTT  (hold, or hold Space)",
                                 height=2, width=32)
        self.btn_ptt.grid(row=0, column=0, padx=6, pady=6, sticky="we")
        self.meter = SMeter(key)
        self.meter.grid(row=0, column=1, padx=6, pady=6, sticky="we")
        key.columnconfigure(1, weight=1)

        cat = ttk.LabelFrame(self, text="CAT port for the software")
        cat.pack(fill="x", **pad)
        ttk.Label(cat, text="Virtual port").grid(row=0, column=0,
                                                 sticky="w", padx=6, pady=4)
        ttk.Entry(cat, textvariable=self.var_catport, width=24).grid(
            row=0, column=1, sticky="we")
        self.btn_cat = ttk.Button(cat, text="Start", width=8,
                                  command=self.toggle_cat)
        self.btn_cat.grid(row=0, column=2, padx=6)

        ttk.Label(cat, text="Answer as").grid(row=1, column=0, sticky="w",
                                              padx=6, pady=4)
        ttk.Combobox(cat, textvariable=self.var_proto, state="readonly",
                     values=[label for _name, label in PROTO_CHOICES]).grid(
            row=1, column=1, sticky="we")
        ttk.Label(cat, text="CI-V addr").grid(row=1, column=2, sticky="e",
                                              padx=6)
        ttk.Entry(cat, textvariable=self.var_addr, width=6).grid(
            row=1, column=3, sticky="w")
        cat.columnconfigure(1, weight=1)

        log = ttk.LabelFrame(self, text="Log")
        log.pack(fill="both", expand=True, **pad)
        self.txt = tk.Text(log, height=11, wrap="none", state="disabled")
        scroll = ttk.Scrollbar(log, orient="vertical",
                               command=self.txt.yview)
        self.txt.configure(yscrollcommand=scroll.set)
        self.txt.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.scan_ports()

    # -- helpers ------------------------------------------------------
    def log(self, text):
        self.log_queue.put(text)

    def _drain(self):
        while True:
            try:
                text = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.txt.configure(state="normal")
            self.txt.insert("end", text + "\n")
            self.txt.see("end")
            self.txt.configure(state="disabled")
        while True:
            try:
                fn = self.worker.get_nowait()
            except queue.Empty:
                break
            fn()
        self.after(120, self._drain)

    def _refresh_meter(self):
        if self.hri is not None:
            self.meter.set(self.hri.smeter())
            if self.hri.tx:
                self.var_status.set("transmitting")
            elif self.cat_running:
                self.var_status.set("CAT port open")
            else:
                self.var_status.set("open")
        self.after(300, self._refresh_meter)

    def _bg(self, fn, done=None):
        """Run ``fn`` off the UI thread and hand the result to ``done``."""
        def runner():
            try:
                result = fn()
            except Exception as exc:
                self.worker.put(lambda e=exc: self._failed(e))
                return
            if done is not None:
                self.worker.put(lambda: done(result))
        threading.Thread(target=runner, daemon=True).start()

    def _failed(self, exc):
        self.log("ERROR: %s" % exc)
        messagebox.showerror("HRI-200 bridge", str(exc))

    def scan_ports(self):
        ports = hri200_cat.list_serial_ports()
        names = [name for name, _desc in ports]
        self.cmb_port.configure(values=names)
        if ports and not self.var_port.get():
            self.var_port.set(names[0])
        for name, desc in ports:
            if desc and desc != "n/a":
                self.log("port %s  %s" % (name, desc))
        if not ports:
            self.log("no serial ports found")

    def _channel(self):
        return {
            "freq_mhz": float(self.var_freq.get().replace(",", ".")),
            "shift_mhz": float(self.var_shift.get().replace(",", ".")),
            "power": self.var_power.get(),
            "tone_hz": float(self.var_tone.get().replace(",", ".")),
            "sql": self.var_sql.get(),
            "narrow": bool(self.var_narrow.get()),
        }

    # -- the box ------------------------------------------------------
    def toggle_connect(self):
        if self.hri is not None:
            self._bg(self._do_close, self._closed)
        else:
            self._bg(self._do_open, self._opened)

    def _do_open(self):
        port = self.var_port.get().strip()
        if not port:
            raise ValueError("no serial port selected")
        chan = self._channel()
        hri = hri200_cat.HRI200(port, chan["freq_mhz"], chan["shift_mhz"],
                                verbose=True, power=chan["power"],
                                tone_hz=chan["tone_hz"], sql=chan["sql"],
                                narrow=chan["narrow"])
        hri.open()
        self.hri = hri
        return hri

    def _opened(self, hri):
        self.log("HRI-200 open on %s" % hri.ser.port)
        self.var_status.set("open")
        self.btn_connect.configure(text="Close")
        self.cmb_port.configure(state="disabled")

    def _do_close(self):
        hri, self.hri = self.hri, None
        if hri is not None:
            hri.set_ptt(False)
            hri.close()

    def _closed(self, _result=None):
        self.log("HRI-200 closed")
        self.var_status.set("not connected")
        self.btn_connect.configure(text="Open")
        self.cmb_port.configure(state="normal")
        self.meter.set(0)

    def apply_channel(self):
        if self.hri is None:
            messagebox.showinfo("HRI-200 bridge", "Open the box first.")
            return
        chan = self._channel()
        hri = self.hri

        def work():
            hri.freq_mhz = chan["freq_mhz"]
            hri.shift_mhz = chan["shift_mhz"]
            hri.power = chan["power"]
            hri.tone_hz = chan["tone_hz"]
            hri.sql = chan["sql"]
            hri.narrow = chan["narrow"]
            hri._apply("channel %.5f MHz" % chan["freq_mhz"])
        self._bg(work, lambda _r: self.log("channel %.5f MHz applied"
                                           % chan["freq_mhz"]))

    def ptt_on(self):
        if self.hri is None:
            self.log("PTT ignored: the box is not open")
            return
        self._bg(lambda: self.hri.set_ptt(True))

    def ptt_off(self):
        if self.hri is None:
            return
        self._bg(lambda: self.hri.set_ptt(False))

    # -- the CAT port -------------------------------------------------
    def toggle_cat(self):
        if self.cat is not None:
            self._stop_cat()
            return
        if self.hri is None:
            messagebox.showinfo("HRI-200 bridge",
                                "Open the box before offering a CAT port.")
            return
        port = self.var_catport.get().strip()
        if not port:
            messagebox.showinfo("HRI-200 bridge",
                                "Name the virtual port the software should "
                                "connect to.")
            return
        proto = LABEL_TO_PROTO.get(self.var_proto.get(), "auto")
        try:
            address = int(self.var_addr.get().strip() or "88", 16)
        except ValueError:
            messagebox.showerror("HRI-200 bridge",
                                 "CI-V address must be hex, e.g. 88")
            return
        try:
            self.cat = hri200_cat.CatServer(
                self.hri, port, proto=proto, verbose=True,
                address=address)
        except Exception as exc:
            self.cat = None
            self._failed(exc)
            return
        self.cat_running = True
        self.cat_thread = threading.Thread(target=self._run_cat,
                                           daemon=True)
        self.cat_thread.start()
        self.btn_cat.configure(text="Stop")
        self.log("CAT port %s open, answering as %s"
                 % (port, self.cat.describe()))

    def _run_cat(self):
        cat = self.cat
        while self.cat_running and self.cat is cat:
            try:
                cat.pump()
            except Exception as exc:                       # pragma: no cover
                self.log("CAT port: %s" % exc)
                break
        self.cat_running = False

    def _stop_cat(self):
        self.cat_running = False
        cat, self.cat = self.cat, None
        if cat is not None:
            try:
                cat.protocol.close()
            except Exception:
                pass
            try:
                cat.ser.close()
            except Exception:
                pass
        if self.cat_thread is not None:
            try:
                self.cat_thread.join(timeout=1.0)
            except Exception:
                pass
            self.cat_thread = None
        self.btn_cat.configure(text="Start")
        self.log("CAT port closed")

    def on_close(self):
        self._stop_cat()
        if self.hri is not None:
            try:
                self.hri.set_ptt(False)
            except Exception:
                pass
            try:
                self.hri.close()
            except Exception:
                pass
        self.destroy()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Control panel for the HRI-200 bridge")
    ap.add_argument("--hri", default=None, help="HRI-200 serial port")
    ap.add_argument("--proto", default="auto",
                    choices=["auto", "kenwood", "yaesu", "civ"],
                    help="dialect the CAT port answers in")
    ap.add_argument("--civ-address", default="88",
                    help="CI-V address the emulated radio answers to, hex")
    ap.add_argument("--freq", type=float, default=144.85, help="MHz")
    ap.add_argument("--shift", type=float, default=0.0, help="MHz")
    ap.add_argument("--power", default="mid",
                    choices=["high", "mid", "low"])
    ap.add_argument("--tone", type=float, default=88.5, help="CTCSS Hz")
    ap.add_argument("--sql", default="none", choices=["none", "tone", "dcs"])
    ap.add_argument("--cat", default=None, help="virtual CAT port to offer")
    args = ap.parse_args(argv)

    if serial is None:
        sys.exit("pyserial missing:  pip install pyserial")

    App(args).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
