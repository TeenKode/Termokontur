#!/usr/bin/env python3
"""Build, verify and package the sensor bench kit from a clean checkout."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]

def run(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()

if run('git', 'status', '--porcelain', '--untracked-files=no'):
    sys.exit('Commit tracked changes before packaging for unambiguous provenance.')
commit = run('git', 'rev-parse', 'HEAD')
subprocess.run([sys.executable, '-m', 'platformio', 'run', '-d', 'firmware/sensor'], cwd=ROOT, check=True)
subprocess.run([sys.executable, 'scripts/check-sensor-build.py'], cwd=ROOT, check=True)
build = ROOT / 'firmware/sensor/.pio/build/esp32-c3-supermini'
image = (build / 'firmware.bin').read_bytes()
version = image[48:80].split(b'\0', 1)[0].decode()
payload = {name: (build / name).read_bytes() for name in (
    'firmware.bin', 'bootloader.bin', 'partitions.bin', 'ota_data_initial.bin')}
payload['flash-sensor.py'] = (ROOT / 'scripts/flash-sensor.py').read_bytes()
for name in ('BENCH.md', 'README.md', 'HARDWARE.md', 'PROTOCOL.md', 'VALIDATION.md'):
    payload[name] = (ROOT / 'docs/sensors' / name).read_bytes()
manifest = {'version': version, 'source_commit': commit, 'chip': 'esp32c3',
            'flash_size': '4MB', 'files': {
                name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                for name, data in payload.items()}}
payload['manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
payload['SHA256SUMS'] = ''.join(
    f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in sorted(payload.items())).encode()
output = ROOT / 'dist' / f'termokontur-sensor-{version}-{commit[:7]}-bench.zip'
output.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for name, data in sorted(payload.items()):
        archive.writestr(name, data)
# Read every member back and validate the packaged checksums.
with zipfile.ZipFile(output) as archive:
    assert archive.testzip() is None
    for line in archive.read('SHA256SUMS').decode().splitlines():
        sha, name = line.split(maxsplit=1)
        assert hashlib.sha256(archive.read(name)).hexdigest() == sha, name
print(f'{output}\nSHA256: {hashlib.sha256(output.read_bytes()).hexdigest()}')
