#!/usr/bin/env python3
"""Verify generated boot config, partition bounds, chip and app identity."""
from pathlib import Path
import struct
from runpy import run_path
root=Path(__file__).resolve().parents[1]
folder=root/'firmware/sensor'
config=(folder/'sdkconfig.esp32-c3-supermini').read_text()
assert 'CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y' in config
assert 'CONFIG_NEWLIB_TIME_SYSCALL_USE_RTC_HRT=y' in config
assert 'CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y' in config
image=(folder/'.pio/build/esp32-c3-supermini/firmware.bin').read_bytes()
assert 256<=len(image)<=0x1c0000
assert image[0]==0xe9 and struct.unpack_from('<H',image,12)[0]==5
assert struct.unpack_from('<I',image,32)[0]==0xabcd5432
assert image[80:112].split(b'\0',1)[0]==b'termokontur-sensor'
version=run_path(str(root/'termokontur/__init__.py'))['__version__']
assert image[48:80].split(b'\0',1)[0].decode()==version
assert 'CONFIG_PARTITION_TABLE_CUSTOM=y' in config
actual={}
table=(folder/'.pio/build/esp32-c3-supermini/partitions.bin').read_bytes()
for pos in range(0,len(table)-31,32):
    magic,kind,subtype,start,size,name,flags=struct.unpack_from('<HBBII16sI',table,pos)
    if magic!=0x50aa:break
    actual[name.split(b'\0',1)[0].decode()]=(start,size)
expected={}
previous=0x9000
for line in (folder/'partitions.csv').read_text().splitlines():
    if not line.strip() or line.startswith('#'):continue
    name,kind,subtype,offset,size=line.split(',')
    start,length=int(offset,0),int(size,0)
    assert start>=previous and start+length<=4*1024*1024
    if kind=='app':assert start%0x10000==0
    previous=start+length
    expected[name]=(start,length)
assert actual==expected,(actual,expected)
print(f'ESP32-C3 {version}, {len(image)} bytes; app descriptor, partition bounds and rollback bootloader config verified')
