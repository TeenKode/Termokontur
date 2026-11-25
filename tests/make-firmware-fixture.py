"""Synthetic firmware archive for native updater tests; never flash it."""
import hashlib, io, json, struct, sys, zipfile
from pathlib import Path

def kit(version, commit):
    image = bytearray(b'\xff'*0x21000)
    image[0]=0xe9
    struct.pack_into('<H',image,12,5)
    image[0x20000]=0xe9
    struct.pack_into('<H',image,0x20000+12,5)
    struct.pack_into('<I',image,0x20000+32,0xabcd5432)
    image[0x20000+48:0x20000+80]=version.encode().ljust(32,b'\0')
    image[0x20000+80:0x20000+112]=b'termokontur-sensor'.ljust(32,b'\0')
    # Use the real firmware layout, so the server test catches missing partitions.
    rows = (Path(__file__).resolve().parents[1]/'firmware/sensor/partitions.csv').read_text().splitlines()
    named_subtypes = {'nvs':2,'ota':0,'phy':1,'ota_0':16,'ota_1':17}
    i = 0
    for row in rows:
        if not row.strip() or row.startswith('#'):
            continue
        name,kind,subtype,offset,size = [field.strip() for field in row.split(',')]
        if name == 'factory_setup' and tuple(map(int, version.split('.'))) < (1,1,5):
            continue
        kind = {'data':1,'app':0}[kind]
        subtype = named_subtypes[subtype] if subtype in named_subtypes else int(subtype,0)
        struct.pack_into('<HBBII16sI',image,0x8000+32*i,0x50aa,kind,subtype,int(offset,0),int(size,0),name.encode().ljust(16,b'\0'),0)
        i += 1
    manifest={'version':version,'source_commit':commit,'files':{'factory.bin':{'sha256':hashlib.sha256(image).hexdigest(),'size':len(image)}}}
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('factory.bin',image);z.writestr('manifest.json',json.dumps(manifest))
    return out.getvalue()


if __name__ == '__main__':
    Path(sys.argv[3]).write_bytes(kit(sys.argv[1], sys.argv[2]))
