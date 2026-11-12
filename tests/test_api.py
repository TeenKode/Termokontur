from termokontur import db

def room_payload(**kwargs):
    data = {"building":"Старшая школа","floor":"2","name":"Кабинет 201",
            "kind":"Кабинет","min_temp":18,"max_temp":24}
    data.update(kwargs)
    return data

def test_auth_and_cookie(client):
    assert client.get("/api/rooms").status_code == 401
    assert client.post("/api/login",json={"password":"incorrect"}).status_code == 401
    assert client.post("/api/login",json={"password":"this-is-a-test-admin-password"}).status_code == 200
    assert client.get("/api/rooms").status_code == 200
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/rooms").status_code == 401

def test_room_readings_and_dashboard(admin):
    created = admin.post("/api/rooms",json=room_payload())
    assert created.status_code == 201, created.text
    room = created.json()
    assert admin.post("/api/rooms",json=room_payload()).status_code == 409
    assert admin.get("/api/dashboard").json()["counts"]["missing"] == 1
    reading = admin.post("/api/readings",json={"room_id":room["id"],"value":17.5})
    assert reading.status_code == 201, reading.text
    assert admin.get("/api/dashboard").json()["counts"]["below"] == 1
    assert len(admin.get("/api/readings").json()) == 1
    assert admin.patch("/api/rooms/"+str(room["id"]),json={"min_temp":17}).status_code == 200
    assert admin.get("/api/dashboard").json()["counts"]["normal"] == 1
    assert admin.get("/api/export.csv").status_code == 200

def test_reject_bad_inputs_and_injection(admin):
    assert admin.post("/api/rooms",json=room_payload(min_temp=24,max_temp=18)).status_code == 422
    assert admin.post("/api/rooms",json=room_payload(name="   ")).status_code == 422
    room = admin.post("/api/rooms",json=room_payload(building="=1+1")).json()
    assert admin.post("/api/readings",json={"room_id":room["id"],"value":999}).status_code == 422
    assert admin.patch("/api/rooms/"+str(room["id"]),json={"max_temp":14}).status_code == 422
    admin.post("/api/readings",json={"room_id":room["id"],"value":20})
    assert "'=1+1" in admin.get("/api/export.csv").text

def test_ingest_authorization_and_dedup(admin):
    rid=admin.post("/api/rooms",json=room_payload()).json()["id"]
    path="/api/ingest"
    data={"room_id":rid,"value":22.1,"device":"zigbee:test","event_id":"r1"}
    assert admin.post(path,json=data).status_code == 401
    assert admin.post(path,json=data,headers={"X-Api-Key":"bad"}).status_code == 401
    headers={"X-Api-Key":"this-is-a-long-device-ingest-key-1234567890"}
    assert admin.post(path,json=data,headers=headers).json()["inserted"]
    assert admin.post(path,json=data,headers=headers).json()["inserted"] is False
    assert len(db.recent_readings()) == 1

def test_pairing_and_disabled_room(admin):
    rid=admin.post("/api/rooms",json=room_payload()).json()["id"]
    code=admin.post(f"/api/rooms/{rid}/pairing").json()["code"]
    assert db.bind_code("12345",code)["room_id"] == rid
    assert db.bind_code("98765",code) is None
    assert db.bound_rooms("12345")[0]["id"] == rid
    assert admin.patch(f"/api/rooms/{rid}",json={"active":False}).status_code == 200
    assert admin.post("/api/readings",json={"room_id":rid,"value":22}).status_code == 422
    assert admin.get("/api/dashboard").json()["rooms"] == []
