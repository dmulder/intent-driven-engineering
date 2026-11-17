PYTHON ?= python3
PREFIX ?= $(HOME)/.local

.PHONY: install uninstall test
install:
	$(PYTHON) scripts/install.py --prefix "$(PREFIX)"

uninstall:
	$(PYTHON) scripts/install.py --prefix "$(PREFIX)" --uninstall

test:
	$(PYTHON) -m unittest discover -s tests -v
