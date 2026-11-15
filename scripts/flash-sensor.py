#!/usr/bin/env python3
"""Initial ESP32-C3 flash. Disconnect TP4056 external positive before USB flashing."""
import argparse
import hashlib
from pathlib import Path
import shlex
import struct
import subprocess
import sys

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--port',required=True,help='COM5, /dev/ttyACM0, etc.')
p.add_argument('--baud',type=int,default=115200)
p.add_argument('--directory',type=Path)
p.add_argument('--dry-run',action='store_true')
a=p.parse_args()
if a.baud<=0:p.error('Invalid baud rate')
folder=a.directory or (Path(__file__).parent if (Path(__file__).parent/'firmware.bin').exists() else Path(__file__).resolve().parents[1]/'firmware/sensor/.pio/build/esp32-c3-supermini')
images=[('0x0','bootloader.bin'),('0x8000','partitions.bin'),('0xf000','ota_data_initial.bin'),('0x20000','firmware.bin')]
for _,name in images:
    if not (folder/name).is_file():p.error(f'Missing {folder/name}')
checksums=folder/'SHA256SUMS'
if checksums.exists():
    for line in checksums.read_text().splitlines():
        sha,name=line.split(maxsplit=1)
        if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=sha:p.error(f'Checksum mismatch: {name}')
image=(folder/'firmware.bin').read_bytes()
if len(image)<256 or len(image)>0x1c0000 or image[0]!=0xe9 or struct.unpack_from('<H',image,12)[0]!=5:p.error('Not a valid-sized ESP32-C3 application')
if image[80:112].split(b'\0',1)[0]!=b'termokontur-sensor':p.error('Wrong application project')
command=[sys.executable,'-m','esptool','--chip','esp32c3','--port',a.port,'--baud',str(a.baud),'--before','default_reset','--after','hard_reset','write_flash','--flash_mode','dio','--flash_freq','80m','--flash_size','4MB']
for offset,name in images:command.extend([offset,str(folder/name)])
print(shlex.join(command))
if not a.dry_run:subprocess.run(command,check=True)
