#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hri200_ysf_gui.py - control panel for the YSF gateway

hri200_ysf.py links a YSF reflector to the radio in C4FM. On the command
line that is one long-running process with a five-second status line. On
a screen it is the same process with the status visible while it happens,
which is what you want while working out why a reflector is not answering.

    python3 hri200_ysf_gui.py
    python3 hri200_ysf_gui.py --call DL4JC --host ysf.example.org --port 42000

What it does not do is change the gateway: the frames, the timings and the
protocol are hri200_ysf.py's, and this file only starts it, stops it and
shows what it reports. The log pane is the gateway's own output, so
anything printed there can also be found in the console version.

"Dry run" skips the serial port entirely and prints the frames instead,
so the reflector side can be tried without a radio on the desk.
"""

import sys

if sys.version_info < (3, 6):
    sys.exit("Python 3.6 or newer required")

import argparse
import queue
import threading
import time
import types

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

import hri200_ysf


class QueueWriter(object):
    """Stand-in for sys.stdout that feeds the log pane.

    The gateway prints from its own threads, and Tk may only be touched
    from the main one, so the lines are queued and drained by the UI.
    """

    def __init__(self, sink, echo=False, original=None):
        self.sink = sink
        self.echo = echo
        self.original = original
        self.lock = threading.Lock()

    def write(self, text):
        if not text:
            return
        with self.lock:
            for line in text.splitlines():
                if line.strip():
                    self.sink(line)
        if self.echo and self.original is not None:
            self.original.write(text)

    def flush(self):
        if self.echo and self.original is not None:
            self.original.flush()

    def isatty(self):
        return False


class Gateway(object):
    """The gateway process, wrapped so a window can drive it."""

    WARMUP_TIMEOUT = 60.0

    def __init__(self, config, log):
        self.cfg = config
        self.log = log
        self.client = None
        self.hri = None
        self.running = False
        self.error = None
        self.started_at = None
        self._watch = None

    # -- lifecycle ----------------------------------------------------
    def start(self):
        cfg = self.cfg
        if cfg["dry_run"]:
            if hri200_ysf.hri200_cat.serial is None:
                raise RuntimeError("pyserial is needed even for a dry run")
            hri200_ysf.hri200_cat.serial = types.SimpleNamespace(
                Serial=lambda: hri200_ysf._NullSerial(verbose=cfg["verbose"]))
            hri200_ysf.hri200_cat.WARMUP = 0.0

        client = hri200_ysf.YSFClient(cfg["host"], cfg["port"], cfg["call"],
                                      verbose=cfg["verbose"], dgid=cfg["dgid"])
        self.client = client

        def pull():
            try:
                return client.queue.popleft()
            except IndexError:
                return None
        pull.queue = client.queue

        hri = hri200_ysf.HRI200Digital(
            cfg["hri"], cfg["freq"], cfg["shift"], verbose=cfg["verbose"],
            power=cfg["power"], dgid=cfg["dgid"], source=pull,
            prefill=cfg["prefill"], call=cfg["call"].upper())
        hri.uplink = client
        hri.forward_gps = cfg["gps"]
        if cfg["node"]:
            hri.node = (cfg["node"], cfg["node_name"] or
                        (cfg["call"].upper() + "-ND"), cfg["node_city"])
        self.hri = hri

        client.start()
        try:
            hri.open()
        except Exception:
            client.stop()
            self.client = None
            self.hri = None
            raise

        self.running = True
        self.started_at = time.time()
        self._watch = threading.Thread(target=self._watch_errors, daemon=True)
        self._watch.start()

    def _watch_errors(self):
        """Notice a gateway thread that died, without polling it blind."""
        while self.running:
            client = self.client
            hri = self.hri
            if client is not None and not client.running:
                self.error = "the reflector link stopped"
                break
            if hri is not None and not hri.running:
                self.error = "the radio loop stopped"
                break
            time.sleep(0.5)

    def stop(self):
        self.running = False
        hri, self.hri = self.hri, None
        client, self.client = self.client, None
        if hri is not None:
            try:
                hri.set_ptt(False)
            except Exception:
                pass
            time.sleep(0.2)
            try:
                hri.close()
            except Exception as exc:
                self.log("closing the radio: %s" % exc)
        if client is not None:
            try:
                client.stop()
            except Exception as exc:
                self.log("closing the reflector: %s" % exc)

    # -- status -------------------------------------------------------
    def stats(self):
        client, hri = self.client, self.hri
        out = {
            "running": self.running,
            "uptime": (time.time() - self.started_at) if self.started_at
                      else 0.0,
            "down": 0, "skipped": 0, "up": 0, "queued": 0,
            "frames_out": 0, "frames_in": 0, "underruns": 0,
            "heard": "", "state": "", "radio_id": "", "source": "",
            "via": "", "receiving": False, "tx": False,
        }
        if client is not None:
            out.update(down=client.packets, skipped=client.skipped,
                       up=client.sent, queued=len(client.queue),
                       source=client.down_src, via=client.down_via)
        if hri is not None:
            out.update(frames_out=hri.total, frames_in=hri.rx_frames,
                       underruns=hri.underruns, heard=hri.heard or "",
                       state=hri.state, radio_id=hri.radio_id,
                       receiving=hri.receiving, tx=hri.tx)
        return out


class YsfApp(tk.Tk):
    def __init__(self, args):
        tk.Tk.__init__(self)
        self.title("HRI-200 YSF gateway")
        self.minsize(700, 620)

        self.gateway = None
        self.worker = queue.Queue()
        self.log_queue = queue.Queue()
        self.args = args
        self._writer = None
        self._saved_stdout = None

        self.var_hri = tk.StringVar(value=args.hri)
        self.var_call = tk.StringVar(value=args.call or "")
        self.var_host = tk.StringVar(value=args.host or "")
        self.var_port = tk.StringVar(value=str(args.port or 42000))
        self.var_freq = tk.StringVar(value="%.5f" % args.freq)
        self.var_shift = tk.StringVar(value="%.5f" % args.shift)
        self.var_power = tk.StringVar(value=args.power)
        self.var_dgid = tk.StringVar(value=str(args.dgid))
        self.var_prefill = tk.StringVar(value=str(args.prefill))
        self.var_node = tk.StringVar(value="")
        self.var_node_name = tk.StringVar(value="")
        self.var_node_city = tk.StringVar(value="")
        self.var_gps = tk.BooleanVar(value=False)
        self.var_dry = tk.BooleanVar(value=False)
        self.var_verbose = tk.BooleanVar(value=True)

        self.var_status = tk.StringVar(value="idle")
        self.var_link = tk.StringVar(value="reflector: -")
        self.var_radio = tk.StringVar(value="radio: -")
        self.var_counters = tk.StringVar(value="")

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self._drain)
        self.after(500, self._refresh)

    # -- layout -------------------------------------------------------
    def _build(self):
        pad = {"padx": 6, "pady": 4}

        ref = ttk.LabelFrame(self, text="Reflector")
        ref.pack(fill="x", **pad)
        ttk.Label(ref, text="Host").grid(row=0, column=0, sticky="w",
                                         padx=6, pady=4)
        ttk.Entry(ref, textvariable=self.var_host, width=32).grid(
            row=0, column=1, sticky="we")
        ttk.Label(ref, text="Port").grid(row=0, column=2, sticky="e",
                                         padx=6)
        ttk.Entry(ref, textvariable=self.var_port, width=8).grid(
            row=0, column=3, sticky="w")

        ttk.Label(ref, text="Callsign").grid(row=1, column=0, sticky="w",
                                             padx=6, pady=4)
        ttk.Entry(ref, textvariable=self.var_call, width=16).grid(
            row=1, column=1, sticky="w")
        ttk.Label(ref, text="DG-ID").grid(row=1, column=2, sticky="e",
                                          padx=6)
        ttk.Spinbox(ref, from_=0, to=99, textvariable=self.var_dgid,
                    width=6).grid(row=1, column=3, sticky="w")
        ref.columnconfigure(1, weight=1)

        radio = ttk.LabelFrame(self, text="Radio")
        radio.pack(fill="x", **pad)
        ttk.Label(radio, text="HRI-200 port").grid(row=0, column=0,
                                                   sticky="w", padx=6,
                                                   pady=4)
        self.cmb_port = ttk.Combobox(radio, textvariable=self.var_hri,
                                     width=24)
        self.cmb_port.grid(row=0, column=1, sticky="we")
        ttk.Button(radio, text="Scan", width=6,
                   command=self.scan_ports).grid(row=0, column=2, padx=2)
        ttk.Checkbutton(radio, text="dry run (no radio)",
                        variable=self.var_dry).grid(row=0, column=3,
                                                    sticky="w", padx=6)

        ttk.Label(radio, text="Frequency").grid(row=1, column=0, sticky="w",
                                                padx=6, pady=4)
        ttk.Entry(radio, textvariable=self.var_freq, width=12).grid(
            row=1, column=1, sticky="w")
        ttk.Label(radio, text="Offset").grid(row=1, column=2, sticky="e",
                                             padx=6)
        frame = ttk.Frame(radio)
        frame.grid(row=1, column=3, sticky="w")
        ttk.Entry(frame, textvariable=self.var_shift, width=8).pack(
            side="left")
        ttk.Label(frame, text="Power").pack(side="left", padx=6)
        ttk.Combobox(frame, textvariable=self.var_power, state="readonly",
                     width=6, values=["high", "mid", "low"]).pack(side="left")
        radio.columnconfigure(1, weight=1)

        opts = ttk.LabelFrame(self, text="Optional")
        opts.pack(fill="x", **pad)
        ttk.Label(opts, text="Node number").grid(row=0, column=0,
                                                 sticky="w", padx=6, pady=4)
        ttk.Entry(opts, textvariable=self.var_node, width=14).grid(
            row=0, column=1, sticky="w")
        ttk.Label(opts, text="Node name").grid(row=0, column=2, sticky="e",
                                               padx=6)
        ttk.Entry(opts, textvariable=self.var_node_name, width=16).grid(
            row=0, column=3, sticky="w")
        ttk.Label(opts, text="Prefill").grid(row=1, column=0, sticky="w",
                                             padx=6, pady=4)
        ttk.Spinbox(opts, from_=1, to=20, textvariable=self.var_prefill,
                    width=6).grid(row=1, column=1, sticky="w")
        ttk.Checkbutton(opts, text="pass on GPS", variable=self.var_gps).grid(
            row=1, column=2, sticky="w", padx=6)
        ttk.Checkbutton(opts, text="verbose", variable=self.var_verbose).grid(
            row=1, column=3, sticky="w", padx=6)
        opts.columnconfigure(3, weight=1)

        ctl = ttk.Frame(self)
        ctl.pack(fill="x", **pad)
        self.btn_start = ttk.Button(ctl, text="Start", width=14,
                                    command=self.toggle)
        self.btn_start.pack(side="left")
        ttk.Label(ctl, textvariable=self.var_status).pack(side="left",
                                                          padx=12)

        stat = ttk.LabelFrame(self, text="Status")
        stat.pack(fill="x", **pad)
        ttk.Label(stat, textvariable=self.var_link).grid(
            row=0, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(stat, textvariable=self.var_radio).grid(
            row=1, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(stat, textvariable=self.var_counters).grid(
            row=2, column=0, sticky="w", padx=6, pady=2)
        stat.columnconfigure(0, weight=1)

        log = ttk.LabelFrame(self, text="Log")
        log.pack(fill="both", expand=True, **pad)
        self.txt = tk.Text(log, height=12, wrap="none", state="disabled")
        scroll = ttk.Scrollbar(log, orient="vertical",
                               command=self.txt.yview)
        self.txt.configure(yscrollcommand=scroll.set)
        self.txt.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.scan_ports()

    # -- plumbing -----------------------------------------------------
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
        self.after(150, self._drain)

    def _bg(self, fn, done=None):
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
        if self.gateway is not None:
            self.gateway.stop()
            self.gateway = None
        self._restore_stdout()
        self.var_status.set("stopped")
        self.btn_start.configure(text="Start")
        messagebox.showerror("YSF gateway", str(exc))

    def scan_ports(self):
        ports = hri200_ysf.hri200_cat.list_serial_ports()
        names = [name for name, _d in ports]
        self.cmb_port.configure(values=names)
        if ports and not self.var_hri.get():
            self.var_hri.set(names[0])
        if not ports:
            self.log("no serial ports found")

    def _config(self):
        host = self.var_host.get().strip()
        call = self.var_call.get().strip().upper()
        if not host:
            raise ValueError("reflector host is required")
        if not call:
            raise ValueError("callsign is required")
        try:
            port = int(self.var_port.get())
        except ValueError:
            raise ValueError("reflector port must be a number")
        try:
            dgid = int(self.var_dgid.get())
        except ValueError:
            raise ValueError("DG-ID must be a number 0..99")
        if not 0 <= dgid <= 99:
            raise ValueError("DG-ID must be 0..99")
        try:
            prefill = int(self.var_prefill.get())
        except ValueError:
            raise ValueError("prefill must be a number")
        dry = bool(self.var_dry.get())
        hri = self.var_hri.get().strip()
        if not dry and not hri:
            raise ValueError("radio port is required unless you dry run")
        return {
            "host": host, "port": port, "call": call,
            "hri": hri, "freq": float(self.var_freq.get().replace(",", ".")),
            "shift": float(self.var_shift.get().replace(",", ".")),
            "power": self.var_power.get(), "dgid": dgid,
            "prefill": prefill, "node": self.var_node.get().strip(),
            "node_name": self.var_node_name.get().strip(),
            "node_city": self.var_node_city.get().strip(),
            "gps": bool(self.var_gps.get()), "dry_run": dry,
            "verbose": bool(self.var_verbose.get()),
        }

    # -- actions ------------------------------------------------------
    def toggle(self):
        if self.gateway is not None:
            self._bg(self._stop)
        else:
            try:
                config = self._config()
            except ValueError as exc:
                messagebox.showerror("YSF gateway", str(exc))
                return
            self.var_status.set("starting ...")
            self.btn_start.configure(text="Stop")
            self._bg(lambda: self._start(config), self._started)

    def _install_stdout(self):
        """Send the gateway's printing to the log pane.

        The gateway narrates what it is doing with print(), and in a
        windowed application there is no console to narrate to. Rather
        than teach hri200_ysf.py a callback for every message, its output
        is redirected while it runs - the pane then shows exactly what
        the command line version would have shown.
        """
        if self._writer is not None:
            return
        self._saved_stdout = sys.stdout
        self._writer = QueueWriter(self.log, echo=False,
                                   original=self._saved_stdout)
        sys.stdout = self._writer

    def _restore_stdout(self):
        if self._writer is None:
            return
        try:
            sys.stdout.flush()
        except Exception:
            pass
        sys.stdout = self._saved_stdout or sys.__stdout__
        self._writer = None
        self._saved_stdout = None

    def _start(self, config):
        self._install_stdout()
        gateway = Gateway(config, self.log)
        gateway.start()
        self.gateway = gateway
        return gateway

    def _started(self, gateway):
        config = gateway.cfg
        if config["dry_run"]:
            self.var_status.set("running (dry run, no radio)")
        else:
            self.var_status.set("running")
        if config["node"]:
            self.log("node identification %s is on" % config["node"])
        if config["gps"]:
            self.log("GPS: the radio's position goes out to the network")

    def _stop(self):
        gateway, self.gateway = self.gateway, None
        if gateway is not None:
            gateway.stop()
        self._restore_stdout()

    def _refresh(self):
        gateway = self.gateway
        if gateway is None:
            self.after(500, self._refresh)
            return
        stats = gateway.stats()
        self.var_link.set("reflector: %s  %d in, %d skipped, %d out, "
                          "%d queued"
                          % (stats["source"] or "-", stats["down"],
                             stats["skipped"], stats["up"],
                             stats["queued"]))
        self.var_radio.set("radio: %s  state %s  %s  heard %s  %d frames "
                           "out, %d in, %d underruns"
                           % (stats["radio_id"] or "-",
                              stats["state"] or "--",
                              "TX" if stats["tx"] else
                              ("RX" if stats["receiving"] else "idle"),
                              stats["heard"] or "-", stats["frames_out"],
                              stats["frames_in"], stats["underruns"]))
        self.var_counters.set("uptime %d:%02d"
                              % (stats["uptime"] // 60, stats["uptime"] % 60))
        if gateway.error:
            self.log("stopped: %s" % gateway.error)
            self._bg(self._stop)
            self.var_status.set("stopped")
            self.btn_start.configure(text="Start")
        self.after(500, self._refresh)

    def on_close(self):
        self._restore_stdout()
        if self.gateway is not None:
            try:
                self.gateway.stop()
            except Exception:
                pass
            self.gateway = None
        self.destroy()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Control panel for the YSF gateway")
    ap.add_argument("--hri", default="COM7", help="HRI-200 serial port")
    ap.add_argument("--call", default="", help="your callsign")
    ap.add_argument("--host", default="", help="reflector hostname")
    ap.add_argument("--port", type=int, default=42000, help="reflector port")
    ap.add_argument("--freq", type=float, default=144.85, help="MHz")
    ap.add_argument("--shift", type=float, default=0.0, help="offset in MHz")
    ap.add_argument("--power", default="mid", choices=["high", "mid", "low"])
    ap.add_argument("--dgid", type=int, default=0, help="DG-ID 0..99")
    ap.add_argument("--prefill", type=int, default=5,
                    help="frames buffered before playout starts")
    args = ap.parse_args(argv)

    YsfApp(args).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
