import asyncio
import copy
import hashlib
import io
import json
import subprocess
import struct
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from termokontur import __version__, db, sensor_firmware, sensor_onboarding as onboarding

BUNDLED = "/api/sensors/firmware/bundled"


def identity():
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint).hex()
    return private, {'public_key': public, 'hardware_id':'0123456789ab', 'sensor_type':'sht4x', 'firmware':'0.1.4'}


def proof(client, private, ident):
    response = client.post('/api/sensors/auth/challenge', json=ident)
    assert response.status_code == 200, response.text
    challenge = response.json()
    signature = private.sign(challenge['message'].encode(), ec.ECDSA(hashes.SHA256())).hex()
    return {**ident, 'challenge':challenge['challenge'], 'signature':signature}


def room():
    return db.create_room('Школа','1','101','Кабинет',18,24)


def test_unknown_device_needs_admin_approval(admin):
    private, ident = identity()
    p = proof(admin, private, ident)
    response = admin.post('/api/sensors/auth/prove', json=p)
    assert response.json()['status'] == 'pending'
    assert 'token' not in response.json()
    assert admin.get('/api/sensors/devices').json() == []
    fp = onboarding.fingerprint(ident['public_key'])
    claims = admin.get('/api/sensors/claims').json()
    assert claims[0]['fingerprint'] == fp
    assert 'public_key' not in claims[0]
    r = room()
    response = admin.post(f'/api/sensors/claims/{fp}/approve', json={'name':'Датчик','room_id':r['id']})
    assert response.status_code == 200, response.text
    authorized = admin.post('/api/sensors/auth/prove', json=proof(admin, private, ident)).json()
    assert authorized['status'] == 'approved' and len(authorized['token']) == 64
    assert admin.get('/api/sensors/claims').json() == []


def test_usb_autoapproves_exact_signed_key_and_is_idempotent(admin):
    private, ident = identity()
    r = room()
    payload = {**proof(admin, private, ident), 'name':'По USB','room_id':r['id'], 'config':{'measure_s':60,'send_s':600,'cool_s':300}}
    result = admin.post('/api/sensors/onboard', json=payload)
    assert result.status_code == 200, result.text
    first = result.json()
    assert first['status'] == 'approved' and first['config']['measure_s'] == 60
    assert 'token' not in first
    # No separate pending-list approval or pre-generated code is used.
    assert admin.get('/api/sensors/claims').json() == []
    payload.update(proof(admin, private, ident))
    again = admin.post('/api/sensors/onboard', json=payload)
    assert again.json()['device_id'] == first['device_id']
    assert len(admin.get('/api/sensors/devices').json()) == 1
    response = admin.post('/api/sensors/auth/prove', json=proof(admin, private, ident))
    assert response.json()['device_id'] == first['device_id']
    assert len(response.json()['token']) == 64
    # Token is only returned to a holder of the private key, never in admin listings.
    listing = admin.get('/api/sensors/devices').json()[0]
    assert 'token_hash' not in listing and 'token' not in listing


def test_usb_approval_requires_admin_and_proof(client):
    private, ident = identity()
    p = proof(client, private, ident)
    assert client.post('/api/sensors/onboard', json={**p,'name':'x','room_id':1}).status_code == 401
    assert client.get('/api/sensors/claims').status_code == 401
    assert client.get('/api/sensors/installer').status_code == 401
    assert client.get('/api/sensors/installer/1/factory.bin').status_code == 401
    assert client.get(BUNDLED).status_code == 401
    assert client.get(f"{BUNDLED}/{__version__}/esp32-c3.bin").status_code == 401
    assert client.get("/sensors/firmware-debug").status_code == 401


def test_firmware_debug_uses_panel_version(admin):
    page = admin.get("/sensors/firmware-debug")
    assert page.status_code == 200
    assert f'id="debug-panel-version">{__version__}</strong>' in page.text
    assert "/static/firmware-debug.mjs?v=" in page.text
    assert page.headers["cache-control"] == "no-store"


def test_signature_replay_metadata_substitution_and_expiry(admin):
    private, ident = identity()
    p = proof(admin, private, ident)
    other, _ = identity()
    wrong = {**p, 'signature':other.sign(b'wrong',ec.ECDSA(hashes.SHA256())).hex()}
    assert admin.post('/api/sensors/auth/prove', json=wrong).status_code == 401
    assert admin.post('/api/sensors/auth/prove', json={**p,'sensor_type':'dht22'}).status_code == 401
    assert admin.post('/api/sensors/auth/prove', json={**p,'hardware_id':'1123456789ab'}).status_code == 401
    assert admin.post('/api/sensors/auth/prove', json=p).status_code == 200
    assert admin.post('/api/sensors/auth/prove', json=p).status_code == 401
    p = proof(admin, private, ident)
    with db.connect() as conn:
        conn.execute('UPDATE sensor_auth_challenges SET expires=0')
    assert admin.post('/api/sensors/auth/prove', json=p).status_code == 401


def test_wrong_board_and_disabled_access_do_not_auto_authorize(admin):
    private, ident = identity()
    r = room()
    approved = admin.post('/api/sensors/onboard', json={**proof(admin,private,ident),'name':'x','room_id':r['id']}).json()
    assert admin.get('/api/sensors/usb/check/'+ident['hardware_id']).status_code == 409
    other, second = identity()
    conflict = admin.post('/api/sensors/onboard', json={**proof(admin,other,second),'name':'other','room_id':r['id']})
    assert conflict.status_code == 409
    device = approved['device_id']
    assert admin.patch('/api/sensors/devices/'+device,json={'name':'x','room_id':r['id'],'enabled':False}).status_code == 200
    assert admin.post('/api/sensors/auth/prove',json=proof(admin,private,ident)).status_code == 403


def test_retired_identity_cannot_take_over_replacement_board(admin):
    original, ident = identity()
    r = room()
    def onboard(private, data):
        return admin.post('/api/sensors/onboard',json={**proof(admin,private,data),'name':'board','room_id':r['id']})
    first = onboard(original, ident).json()['device_id']
    assert admin.patch('/api/sensors/devices/'+first,json={'name':'old','room_id':r['id'],'enabled':False}).status_code == 200
    replacement, second = identity()
    new = onboard(replacement, second)
    assert new.status_code == 200
    assert onboard(original, ident).status_code == 409
    # The current approved key keeps its access after the failed takeover.
    assert admin.post('/api/sensors/auth/prove',json=proof(admin,replacement,second)).json()['status'] == 'approved'


def test_rekey_revokes_signed_renewal_and_rejection_stays_rejected(admin):
    private, ident = identity()
    r = room()
    d = admin.post('/api/sensors/onboard',json={**proof(admin,private,ident),'name':'x','room_id':r['id']}).json()['device_id']
    assert admin.post('/api/sensors/devices/'+d+'/rekey').status_code == 200
    result = admin.post('/api/sensors/auth/prove',json=proof(admin,private,ident)).json()
    assert result['status'] == 'pending' and 'token' not in result
    fp = onboarding.fingerprint(ident['public_key'])
    assert admin.post('/api/sensors/claims/'+fp+'/reject').status_code == 200
    assert admin.post('/api/sensors/auth/prove',json=proof(admin,private,ident)).json()['status'] == 'rejected'
    archived = admin.get('/api/sensors/devices').json()[0]
    assert archived['status'] == 'rejected' and not archived['enabled']
    assert archived['next_sync_at'] is None
    assert admin.get('/api/sensors/devices/'+d+'/records').status_code == 200
    assert admin.post('/api/sensors/claims/'+fp+'/approve',json={'name':'x','room_id':r['id']}).status_code == 200
    assert admin.get('/api/sensors/devices').json()[0]['enabled']


def test_challenge_rate_and_invalid_curve(admin):
    _, ident = identity()
    assert admin.post('/api/sensors/auth/challenge',json={**ident,'public_key':'04'+'00'*64}).status_code == 422
    for _ in range(8):
        assert admin.post('/api/sensors/auth/challenge',json=ident).status_code == 200
    assert admin.post('/api/sensors/auth/challenge',json=ident).status_code == 429


def kit(version='0.1.4', mutate=None):
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
        kind = {'data':1,'app':0}[kind]
        subtype = named_subtypes[subtype] if subtype in named_subtypes else int(subtype,0)
        struct.pack_into('<HBBII16sI',image,0x8000+32*i,0x50aa,kind,subtype,int(offset,0),int(size,0),name.encode().ljust(16,b'\0'),0)
        i += 1
    if mutate: mutate(image)
    manifest={'version':version,'source_commit':'a'*40,'files':{'factory.bin':{'sha256':hashlib.sha256(image).hexdigest(),'size':len(image)}}}
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('factory.bin',image);z.writestr('manifest.json',json.dumps(manifest))
    return out.getvalue()


def test_fetch_cache_and_download_current_firmware(admin, monkeypatch, tmp_path):
    data = kit(__version__)
    stale = kit("0.8.0")
    asset = tmp_path / "firmware" / f"{__version__}.zip"
    client_class = httpx.AsyncClient
    replies = iter([httpx.Response(404), httpx.Response(200, content=stale), httpx.Response(200, content=data)])

    def respond(request):
        assert str(request.url) == sensor_firmware.RELEASE_URL
        return next(replies)

    def mocked_client(**kwargs):
        return client_class(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mocked_client)
    monkeypatch.setattr(sensor_firmware, "_download_git_bundle", lambda: stale)
    assert admin.get(BUNDLED).json() is None
    for _ in range(2):
        assert not asyncio.run(sensor_firmware.fetch_current())
        assert not asset.exists()
    assert asyncio.run(sensor_firmware.fetch_current())
    assert asset.read_bytes() == data
    assert asyncio.run(sensor_firmware.fetch_current())

    bundle = sensor_firmware.read_release(data)
    meta = admin.get(BUNDLED).json()
    assert meta["version"] == __version__
    assert meta["esp32"] == {"sha256": bundle.esp32_sha256, "size": len(bundle.esp32)}
    for target, image in [("esp32-c3", bundle.esp32), ("esp32-c3-factory", bundle.factory.image)]:
        downloaded = admin.get(f"{BUNDLED}/{__version__}/{target}.bin")
        assert downloaded.content == image
        assert f"termokontur-{target}-{__version__}.bin" in downloaded.headers["content-disposition"]
    assert admin.get(f"{BUNDLED}/0.8.0/esp32-c3.bin").status_code == 404


def test_retry_downloads_exact_release_from_git_when_http_fails(admin, monkeypatch, tmp_path):
    data = kit(__version__)
    remote = tmp_path / "firmware.git"
    subprocess.run(["git", "init", "--quiet", str(remote)], check=True)
    (remote / sensor_firmware.RELEASE_FILE).write_bytes(data)
    subprocess.run(["git", "-C", str(remote), "add", sensor_firmware.RELEASE_FILE], check=True)
    subprocess.run(["git", "-C", str(remote), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                    "commit", "--quiet", "-m", "Test firmware"], check=True)
    subprocess.run(["git", "-C", str(remote), "tag", sensor_firmware.BUNDLE_TAG], check=True)
    monkeypatch.setattr(sensor_firmware, "REPOSITORY", (tmp_path / "firmware").as_uri())

    def unavailable_client(**_):
        raise ImportError("Proxy transport cannot start")

    monkeypatch.setattr(httpx, "AsyncClient", unavailable_client)
    assert admin.post(BUNDLED + "/retry").json() == {"status": "ready"}
    cached = tmp_path / "firmware" / f"{__version__}.zip"
    assert cached.read_bytes() == data
    assert admin.get(BUNDLED).json()["version"] == __version__


def test_manually_uploaded_current_zip_enables_both_binary_downloads(admin, tmp_path):
    assert admin.post("/api/sensors/installer", files={"file": ("old.zip", kit("1.0.0"))}).status_code == 200
    assert admin.get(BUNDLED).json() is None
    data = kit(__version__)
    assert admin.post("/api/sensors/installer", files={"file": ("current.zip", data)}).status_code == 200
    assert admin.get(BUNDLED).json()["version"] == __version__
    assert not (tmp_path / "firmware").exists()
    bundle = sensor_firmware.read_release(data)
    assert admin.get(f"{BUNDLED}/{__version__}/esp32-c3-factory.bin").content == bundle.factory.image
    assert admin.get(f"{BUNDLED}/{__version__}/esp32-c3.bin").content == bundle.esp32


def test_assign_bundled_firmware_without_manual_upload(admin, tmp_path):
    from test_sensors import batch, setup_sensor, sync

    data = kit(__version__)
    asset = tmp_path / "firmware" / f"{__version__}.zip"
    asset.parent.mkdir()
    asset.write_bytes(data)
    device, _, headers, _ = setup_sensor(admin)
    route = f'/api/sensors/devices/{device["device_id"]}'
    assert admin.get("/api/sensors/firmware").json() == []
    assert sync(admin, device, headers, batch()).json()["ota"] is None
    assert admin.put(f"{route}/ota?bundled_version=0.8.0").status_code == 404
    for _ in range(2):
        reply = admin.put(f"{route}/ota?bundled_version={__version__}")
        assert reply.status_code == 200, reply.text
    offer = sync(admin, device, headers, batch()).json()["ota"]
    bundle = sensor_firmware.read_release(data)
    assert offer["version"] == __version__
    assert offer["sha256"] == bundle.esp32_sha256
    assert len(admin.get("/api/sensors/firmware").json()) == 1
    # A pending OTA keeps its own copy if the panel later changes version.
    asset.unlink()
    assert admin.get(f'{route}/firmware/{offer["id"]}', headers=headers).content == bundle.esp32


def test_installer_upload_integrity_version_and_authenticated_download(admin, client):
    data = kit()
    response=admin.post('/api/sensors/installer',files={'file':('kit.zip',data,'application/zip')})
    assert response.status_code==200,response.text
    ident=response.json()['id']
    info=admin.get('/api/sensors/installer').json()
    downloaded=admin.get(f'/api/sensors/installer/{ident}/factory.bin')
    assert hashlib.sha256(downloaded.content).hexdigest()==info['sha256']
    assert downloaded.headers['cache-control']=='no-store'
    assert admin.post('/api/sensors/installer',files={'file':('kit.zip',data)}).json()['id']==ident
    for invalid in [b'not a zip',kit('0.1.3'),kit(mutate=lambda b:b.__setitem__(0x9000,1)),kit(mutate=lambda b:b.__setitem__(12,4)),kit(mutate=lambda b:b.__setitem__(0x8002,0))]:
        assert admin.post('/api/sensors/installer',files={'file':('kit.zip',invalid)}).status_code==422
    # Valid but different content cannot replace an already installed version.
    changed=kit(mutate=lambda b:b.__setitem__(0x20,1))
    assert admin.post('/api/sensors/installer',files={'file':('kit.zip',changed)}).status_code==409


@pytest.mark.parametrize('problem', ['encrypted', 'compression', 'deflate'])
def test_installer_rejects_unreadable_zip_as_validation_error(admin, problem):
    data = bytearray(kit())
    if problem == 'deflate':
        # Corrupt the first DEFLATE stream without changing its ZIP envelope.
        offset = 30 + struct.unpack_from('<H', data, 26)[0] + struct.unpack_from('<H', data, 28)[0]
        data[offset] = (data[offset] & ~6) | 6  # Reserved DEFLATE block type.
    else:
        offset = data.index(b'PK\x01\x02')
        if problem == 'encrypted':
            struct.pack_into('<H', data, 6, 1)
            struct.pack_into('<H', data, offset + 8, 1)
        else:
            struct.pack_into('<H', data, 8, 99)
            struct.pack_into('<H', data, offset + 10, 99)
    response = admin.post('/api/sensors/installer', files={'file': ('invalid.zip', bytes(data))})
    assert response.status_code == 422, response.text
    assert admin.get('/api/sensors/installer').json() is None


def test_invalid_signatures_are_rate_limited_without_consuming_challenge(admin):
    private, ident = identity()
    payload = proof(admin, private, ident)
    invalid = {**payload, 'signature': private.sign(b'wrong-message', ec.ECDSA(hashes.SHA256())).hex()}
    for _ in range(32):
        assert admin.post('/api/sensors/auth/prove', json=invalid).status_code == 401
    assert admin.post('/api/sensors/auth/prove', json=invalid).status_code == 429
    with db.connect() as conn:
        assert conn.execute('SELECT 1 FROM sensor_auth_challenges WHERE nonce=?', (payload['challenge'],)).fetchone()
        assert conn.execute('SELECT count(*) FROM sensor_identities').fetchone()[0] == 0


def test_bootstrap_uses_server_time_and_active_public_certificate(client, monkeypatch):
    from termokontur import tls
    monkeypatch.setattr(onboarding.time, 'time', lambda: 1794700100)
    monkeypatch.setattr(tls, '_managed', True)
    monkeypatch.setattr(tls, '_running_certificate', 'active-public-chain')
    for response in (client.get('/api/sensors/bootstrap', headers={'Origin':'http://192.168.4.1'}),
                     client.post('/api/sensors/bootstrap', json={})):
        assert response.status_code == 200
        assert response.json() == {'time':1794700100, 'ca':'active-public-chain', 'sync_replay':True,
                                   'usb_powered_sync':True}
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['access-control-allow-origin'] == '*'
        assert 'access-control-allow-credentials' not in response.headers


def test_bootstrap_fails_closed_for_invalid_private_proxy_ca(client, monkeypatch, tmp_path):
    from termokontur import tls
    path = tmp_path / 'proxy-ca.pem'
    monkeypatch.setenv('TERMOKONTUR_SENSOR_CA_FILE', str(path))
    for content in (b'broken', b'PRIVATE KEY', b'x'*8001):
        path.write_bytes(content)
        response = client.get('/api/sensors/bootstrap')
        assert response.status_code == 503
        assert str(path) not in response.text
    path.unlink()
    assert client.get('/api/sensors/bootstrap').status_code == 503
