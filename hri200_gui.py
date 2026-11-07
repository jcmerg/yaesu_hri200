#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hri200_gui.py - a small control panel for the bridge

Everything the command line tools do, with a knob to turn: pick the
radio, connect, set the frequency, watch the meter, key the transmitter,
and optionally run the TS-2000 CAT server so flrig or fldigi can drive
it as well.

Tkinter is in the Python standard library on Windows and macOS, and
comes with python3-tk on Linux. Nothing else is needed beyond pyserial.

    python3 hri200_gui.py
    python3 hri200_gui.py --radio yaesu --port /dev/ttyUSB0 --freq 145.5

The window is deliberately plain: every control maps to exactly one
thing the radio does, and the log shows the frames that were exchanged,
so a wrong baud rate or a silent radio is visible rather than mysterious.
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

import hri200_radio


BACKEND_LABELS = [
    ("hri200", "HRI-200 (WIRES-X box, 38400)"),
    ("yaesu", "Yaesu 5-byte CAT (FT-817/857/897, 9600)"),
    ("kenwood", "Kenwood CAT (TS-2000, FT-991, 38400)"),
    ("icom", "Icom CI-V (IC-706/7100/7300, 19200)"),
]
LABEL_TO_KIND = {label: kind for kind, label in BACKEND_LABELS}
KIND_TO_LABEL = {kind: label for kind, label in BACKEND_LABELS}

DEFAULT_BAUD = {"hri200": 38400, "yaesu": 9600,
                "kenwood": 38400, "icom": 19200}

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

    def set_default_bg(self, colour):
        self.defbg = colour
        self.configure(bg=colour)


class SMeter(tk.Canvas):
    """A nine-segment bar; the bridge only ever knows 0..30."""

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
        self.minsize(720, 620)

        self.radio = None
        self.cat = None
        self.cat_thread = None
        self.worker = queue.Queue()
        self.log_queue = queue.Queue()
        self.args = args

        self.var_kind = tk.StringVar(value=KIND_TO_LABEL.get(args.radio,
                                                             BACKEND_LABELS[0][1]))
        self.var_port = tk.StringVar(value=args.port or "")
        self.var_baud = tk.StringVar(value=str(DEFAULT_BAUD.get(args.radio,
                                                               38400)))
        self.var_addr = tk.StringVar(value=args.civ_address)
        self.var_freq = tk.StringVar(value="%.5f" % args.freq)
        self.var_shift = tk.StringVar(value="%.5f" % args.shift)
        self.var_power = tk.StringVar(value=args.power)
        self.var_tone = tk.StringVar(value="%.1f" % args.tone)
        self.var_sql = tk.StringVar(value=args.sql)
        self.var_narrow = tk.BooleanVar(value=False)
        self.var_catport = tk.StringVar(value=args.cat or "")
        self.var_status = tk.StringVar(value="not connected")

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._drain)
        self.after(300, self._refresh_meter)

    # -- layout -------------------------------------------------------
    def _build(self):
        pad = {"padx": 6, "pady": 4}

        box = ttk.LabelFrame(self, text="Radio")
        box.pack(fill="x", **pad)

        ttk.Label(box, text="Type").grid(row=0, column=0, sticky="w",
                                         padx=6, pady=4)
        self.cmb_kind = ttk.Combobox(
            box, textvariable=self.var_kind, state="readonly", width=40,
            values=[label for _kind, label in BACKEND_LABELS])
        self.cmb_kind.grid(row=0, column=1, columnspan=3, sticky="we",
                           padx=6, pady=4)
        self.cmb_kind.bind("<<ComboboxSelected>>", self._kind_changed)

        ttk.Label(box, text="Port").grid(row=1, column=0, sticky="w",
                                         padx=6, pady=4)
        self.cmb_port = ttk.Combobox(box, textvariable=self.var_port,
                                     width=28)
        self.cmb_port.grid(row=1, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(box, text="Scan", command=self.scan_ports,
                   width=6).grid(row=1, column=2, padx=2)
        ttk.Label(box, text="Baud").grid(row=1, column=3, sticky="e",
                                         padx=6)
        ttk.Entry(box, textvariable=self.var_baud, width=8).grid(
            row=1, column=4, sticky="w")

        ttk.Label(box, text="CI-V addr").grid(row=2, column=0, sticky="w",
                                              padx=6, pady=4)
        ttk.Entry(box, textvariable=self.var_addr, width=8).grid(
            row=2, column=1, sticky="w", padx=6)
        ttk.Label(box, text="(Icom only, hex)").grid(row=2, column=2,
                                                     columnspan=3,
                                                     sticky="w")

        self.btn_connect = ttk.Button(box, text="Connect",
                                      command=self.toggle_connect)
        self.btn_connect.grid(row=3, column=0, columnspan=2, sticky="we",
                              padx=6, pady=6)
        ttk.Label(box, textvariable=self.var_status).grid(
            row=3, column=2, columnspan=3, sticky="w", padx=6)
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
        self.btn_ptt.set_default_bg("#e0e0e0")
        self.btn_ptt.grid(row=0, column=0, padx=6, pady=6, sticky="we")
        self.meter = SMeter(key)
        self.meter.grid(row=0, column=1, padx=6, pady=6, sticky="we")
        key.columnconfigure(1, weight=1)

        cat = ttk.LabelFrame(self, text="CAT server (optional, for flrig)")
        cat.pack(fill="x", **pad)
        ttk.Label(cat, text="Virtual port").grid(row=0, column=0, sticky="w",
                                                 padx=6, pady=4)
        ttk.Entry(cat, textvariable=self.var_catport, width=28).grid(
            row=0, column=1, sticky="we")
        self.btn_cat = ttk.Button(cat, text="Start",
                                  command=self.toggle_cat)
        self.btn_cat.grid(row=0, column=2, padx=6)
        cat.columnconfigure(1, weight=1)

        log = ttk.LabelFrame(self, text="Log")
        log.pack(fill="both", expand=True, **pad)
        self.txt = tk.Text(log, height=12, wrap="none", state="disabled")
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
        if self.radio is not None:
            self.meter.set(self.radio.smeter())
            self.var_status.set("transmitting" if self.radio.tx
                                else "ready")
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

    def _kind_changed(self, _event=None):
        kind = LABEL_TO_KIND[self.var_kind.get()]
        self.var_baud.set(str(DEFAULT_BAUD.get(kind, 38400)))

    def scan_ports(self):
        ports = hri200_radio.list_serial_ports()
        names = [name for name, _desc in ports]
        self.cmb_port.configure(values=names)
        if ports and not self.var_port.get():
            self.var_port.set(names[0])
        for name, desc in ports:
            if desc:
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

    # -- actions ------------------------------------------------------
    def toggle_connect(self):
        if self.radio is not None:
            self._bg(self._do_disconnect, self._disconnected)
        else:
            self._bg(self._do_connect, self._connected)

    def _do_connect(self):
        kind = LABEL_TO_KIND[self.var_kind.get()]
        port = self.var_port.get().strip()
        if not port:
            raise ValueError("no serial port selected")
        chan = self._channel()
        try:
            address = int(self.var_addr.get().strip() or "88", 16)
        except ValueError:
            raise ValueError("CI-V address must be hex, e.g. 88")
        radio = hri200_radio.create_radio(
            kind, port, verbose=True, baudrate=int(self.var_baud.get()),
            address=address, freq_mhz=chan["freq_mhz"])
        radio.freq_mhz = chan["freq_mhz"]
        radio.shift_mhz = chan["shift_mhz"]
        radio.power = chan["power"]
        radio.tone_hz = chan["tone_hz"]
        radio.sql = chan["sql"]
        radio.narrow = chan["narrow"]
        radio.mode = "fm-n" if chan["narrow"] else "fm"
        radio.open()
        self.radio = radio
        return radio

    def _connected(self, radio):
        self.log("connected: %s on %s" % (radio.name, radio.port))
        self.var_status.set("ready")
        self.btn_connect.configure(text="Disconnect")
        self.cmb_kind.configure(state="disabled")
        self.cmb_port.configure(state="disabled")

    def _do_disconnect(self):
        radio, self.radio = self.radio, None
        if radio is not None:
            radio.close()

    def _disconnected(self, _result=None):
        self.log("disconnected")
        self.var_status.set("not connected")
        self.btn_connect.configure(text="Connect")
        self.cmb_kind.configure(state="readonly")
        self.cmb_port.configure(state="normal")
        self.meter.set(0)

    def apply_channel(self):
        if self.radio is None:
            messagebox.showinfo("HRI-200 bridge", "Connect a radio first.")
            return
        chan = self._channel()
        radio = self.radio

        def work():
            radio.freq_mhz = chan["freq_mhz"]
            radio.shift_mhz = chan["shift_mhz"]
            radio.power = chan["power"]
            radio.tone_hz = chan["tone_hz"]
            radio.sql = chan["sql"]
            radio.narrow = chan["narrow"]
            radio.mode = "fm-n" if chan["narrow"] else "fm"
            radio.apply()
        self._bg(work, lambda _r: self.log("channel %.5f MHz applied"
                                           % chan["freq_mhz"]))

    def ptt_on(self):
        if self.radio is None:
            self.log("PTT ignored: not connected")
            return
        self._bg(lambda: self._set_ptt(True))

    def ptt_off(self):
        if self.radio is None:
            return
        self._bg(lambda: self._set_ptt(False))

    def _set_ptt(self, on):
        self.radio.set_ptt(on)
        self.log("PTT %s" % ("ON" if on else "off"))

    def toggle_cat(self):
        if self.cat is not None:
            self._stop_cat()
            return
        if self.radio is None:
            messagebox.showinfo("HRI-200 bridge",
                                "Connect a radio before starting the CAT "
                                "server.")
            return
        port = self.var_catport.get().strip()
        if not port:
            messagebox.showinfo("HRI-200 bridge",
                                "Name the virtual port the CAT server should "
                                "listen on.")
            return
        try:
            import hri200_cat
        except ImportError as exc:
            self._failed(exc)
            return
        try:
            self.cat = hri200_cat.TS2000(
                hri200_radio.CatHandle(self.radio), port, baud=38400, verbose=True)
        except Exception as exc:
            self.cat = None
            self._failed(exc)
            return
        self.cat_thread = threading.Thread(target=self._run_cat,
                                           daemon=True)
        self.cat_thread.start()
        self.btn_cat.configure(text="Stop")
        self.log("CAT server on %s - in flrig pick TS-2000" % port)

    def _run_cat(self):
        cat = self.cat
        try:
            while self.cat is cat:
                self._cat_step(cat)
        except Exception as exc:                            # pragma: no cover
            self.log("CAT server stopped: %s" % exc)
        finally:
            try:
                cat.ser.close()
            except Exception:
                pass

    def _cat_step(self, cat):
        cat.pump()

    def _stop_cat(self):
        cat, self.cat = self.cat, None
        if cat is not None:
            try:
                cat.ser.close()
            except Exception:
                pass
        self.btn_cat.configure(text="Start")
        self.log("CAT server stopped")

    def on_close(self):
        self._stop_cat()
        if self.radio is not None:
            try:
                self.radio.set_ptt(False)
            except Exception:
                pass
            try:
                self.radio.close()
            except Exception:
                pass
        self.destroy()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Control panel for the bridge")
    ap.add_argument("--radio", default="hri200",
                    choices=sorted(hri200_radio.BACKENDS))
    ap.add_argument("--port", default=None, help="serial port of the radio")
    ap.add_argument("--freq", type=float, default=144.85, help="MHz")
    ap.add_argument("--shift", type=float, default=0.0, help="MHz")
    ap.add_argument("--power", default="mid",
                    choices=["high", "mid", "low"])
    ap.add_argument("--tone", type=float, default=88.5, help="CTCSS Hz")
    ap.add_argument("--sql", default="none", choices=["none", "tone", "dcs"])
    ap.add_argument("--cat", default=None, help="virtual CAT port to offer")
    ap.add_argument("--civ-address", default="88", help="Icom CI-V address")
    args = ap.parse_args(argv)

    if serial is None:
        sys.exit("pyserial missing:  pip install pyserial")

    App(args).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
