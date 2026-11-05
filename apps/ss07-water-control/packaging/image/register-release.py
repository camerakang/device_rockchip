#!/usr/bin/env python3
"""Called inside the isolated factory rootfs builder, never against the host root."""
import json
from pathlib import Path
import sys

if not Path('/build-inputs/image.lock.json').is_file() or not Path('/build-app/SS07-IMPORT.json').is_file():
    raise SystemExit('factory builder inputs required')
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path('/opt/ss07/current').resolve() / 'src'))
from ss07.release_install import Installer


class FactoryBackend:
    def __getattr__(self, name): raise RuntimeError('factory registration must not perform service actions')


with Installer(backend=FactoryBackend()) as installer:
    print(json.dumps(installer.register_image(Path('/build-app'))))
