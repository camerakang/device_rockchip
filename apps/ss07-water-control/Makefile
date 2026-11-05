PYTHON ?= python3
NODE ?= node
export PYTHONDONTWRITEBYTECODE := 1
export PYTHONPATH := $(CURDIR)/src

.PHONY: help validate test run probe package stage native
help:
	@echo 'validate | test | run | probe | native | package OUTPUT=/new/directory | stage DESTDIR=/new/directory'
native:
	cmake -S . -B build/native -DCMAKE_BUILD_TYPE=Release
	cmake --build build/native -j2
validate:
	$(PYTHON) -m ss07 validate
test:
	$(PYTHON) -m unittest discover -s tests -v
	$(NODE) tests/test-direct-session.cjs
run:
	$(PYTHON) -m ss07 serve
probe:
	$(PYTHON) -m ss07 probe
package:
	@test -n "$(OUTPUT)" || (echo 'OUTPUT is required'; exit 2)
	$(PYTHON) packaging/build-app.py --output "$(OUTPUT)"
stage:
	@test -n "$(DESTDIR)" || (echo 'DESTDIR is required'; exit 2)
	$(PYTHON) packaging/stage-rootfs.py --destdir "$(DESTDIR)"
