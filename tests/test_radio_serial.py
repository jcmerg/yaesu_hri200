#!/usr/bin/env python3
"""Run a backend against a pretend radio on a pseudo terminal.

The frame builders are checked on their own in test_radio_frames.py;
this is the other half - that the serial plumbing actually gets those
frames out, that the answers are understood, and that the polling thread
leaves the port in a sane state.

A Kenwood is imitated here because its dialect is plain ASCII, which
makes a failure readable in the output rather than a hex dump.

Needs a POSIX pseudo terminal, which is why it is skipped on Windows.

    python3 tests/test_radio_serial.py
"""

import os
import select
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hri200_radio


class FakeKenwood(threading.Thread):
    """Answers the four commands the backend sends, nothing more."""

    def __init__(self, fd):
        threading.Thread.__init__(self, daemon=True)
        self.fd = fd
        self.stop = threading.Event()
        self.seen = []
        self.meter = 21
        self.tx = False

    def run(self):
        buf = b""
        while not self.stop.is_set():
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if not ready:
                continue
            try:
                buf += os.read(self.fd, 256)
            except OSError:
                return
            while b";" in buf:
                cmd, buf = buf.split(b";", 1)
                cmd = cmd.strip().decode("ascii", "ignore")
                if not cmd:
                    continue
                self.seen.append(cmd)
                self.answer(cmd)

    def answer(self, cmd):
        reply = None
        if cmd == "FA":
            reply = "FA00144850000;"
        elif cmd.startswith("FA"):
            reply = cmd + ";"
        elif cmd == "MD":
            reply = "MD4;"
        elif cmd.startswith("MD"):
            reply = cmd + ";"
        elif cmd == "PC":
            reply = "PC020;"
        elif cmd.startswith("PC"):
            reply = cmd + ";"
        elif cmd == "SM0":
            reply = "SM0%04d;" % self.meter
        elif cmd == "TX":
            self.tx = True
        elif cmd == "RX":
            self.tx = False
        if reply:
            os.write(self.fd, reply.encode("ascii"))


def wait_for(predicate, timeout=1.5):
    """Wait until ``predicate()`` is true.

    The pretend radio runs in its own thread, so a command this process
    has written is not necessarily a command it has read yet. Checking
    immediately makes the test race itself rather than the code.
    """
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def main():
    if os.name != "posix":
        print("  skipped: needs a pseudo terminal")
        return 0

    master, slave = os.openpty()
    slave_name = os.ttyname(slave)
    os.close(slave)

    radio = hri200_radio.KenwoodCat(slave_name, verbose=False)
    radio.freq_mhz = 144.85
    radio.power = "mid"

    fake = FakeKenwood(master)
    fake.start()

    checks = []
    try:
        radio.open()
        # The background poll would race with the explicit calls below;
        # stop it and drive poll_once by hand so the test is repeatable.
        radio._running = False
        if radio._poll_thread is not None:
            radio._poll_thread.join(timeout=2.0)

        checks.append(("open pushed the frequency",
                       "FA00144850000" in fake.seen))
        checks.append(("open pushed the mode", "MD4" in fake.seen))
        checks.append(("open pushed the power", "PC020" in fake.seen))

        fake.seen = []
        radio.set_freq(145.5)
        checks.append(("set_freq sends one new frequency",
                       wait_for(lambda: fake.seen == ["FA00145500000"])))

        fake.seen = []
        radio.set_ptt(True)
        checks.append(("set_ptt sends TX;",
                       wait_for(lambda: fake.seen == ["TX"])))
        checks.append(("the pretend radio keyed",
                       wait_for(lambda: fake.tx is True)))

        fake.seen = []
        radio.set_ptt(False)
        checks.append(("set_ptt sends RX;",
                       wait_for(lambda: fake.seen == ["RX"])))
        checks.append(("the pretend radio unkeyed",
                       wait_for(lambda: fake.tx is False)))

        fake.seen = []
        radio.poll_once()
        checks.append(("poll asks for the meter", "SM0" in fake.seen))
        checks.append(("the meter reading lands", radio.rx_level == 21))
        checks.append(("the meter maps onto the bar", radio.smeter() == 21))

        fake.meter = 7
        radio.poll_once()
        checks.append(("the meter follows the radio",
                       radio.rx_level == 7))

        # A radio that says nothing must not hang the caller.
        fake.stop.set()
        fake.join(timeout=2.0)
        started = time.time()
        radio.apply()
        elapsed = time.time() - started
        checks.append(("a silent radio times out rather than hangs",
                       elapsed < 3.0))
    finally:
        fake.stop.set()
        try:
            radio.close()
        except Exception:
            pass
        try:
            os.close(master)
        except OSError:
            pass

    width = max(len(name) for name, _ok in checks)
    failed = 0
    for name, ok in checks:
        print("  %-*s  %s" % (width, name, "ok" if ok else "FAILED"))
        if not ok:
            failed += 1
    print("\n%d checks, %d failed" % (len(checks), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
