# Build, test and package the HRI-200 bridge.
#
#   make test        the checks that need no hardware
#   make icons       draw the application icon (PNG, ICO, ICNS)
#   make app         PyInstaller executables for this machine
#   make dist        zip / tarball ready to hand to somebody
#   make run         the control panel from source
#   make bridge      the CAT bridge from source
#   make ports       list the serial ports the system sees
#   make clean       remove build output
#
# PYTHON selects the interpreter. On macOS with Homebrew, the python3 on
# PATH may be built without Tk; the one from python-tk has it.

PYTHON ?= python3
PYINSTALLER ?= $(PYTHON) -m PyInstaller
DIST ?= dist
BUILD ?= build
OUT ?= out
VERSION ?= 1.0

.PHONY: all test icons app dist run bridge ports clean distclean

all: test

test:
	$(PYTHON) tests/test_radio_frames.py
	$(PYTHON) tests/test_cat_server.py
	$(PYTHON) tests/test_ysf_frame.py
	$(PYTHON) tests/test_node_id.py
	$(PYTHON) tests/test_ysf_gui_smoke.py

icons:
	$(PYTHON) packaging/make_icons.py

app: icons
	$(PYINSTALLER) packaging/hri200.spec --noconfirm \
		--distpath $(DIST) --workpath $(BUILD)

dist: app
	$(PYTHON) packaging/make_dist.py --dist $(DIST) --out $(OUT)

run:
	$(PYTHON) hri200_gui.py

bridge:
	$(PYTHON) hri200_cat.py $(CATPORT) --hri $(HRIPORT)

ports:
	$(PYTHON) -c "import hri200_radio as r; [print('%-32s %s' % p) \
		for p in r.list_serial_ports()]"

clean:
	rm -rf $(BUILD) $(DIST) $(OUT) __pycache__ tests/__pycache__ \
		packaging/__pycache__

distclean: clean
	rm -rf venv .venv packaging/icons
