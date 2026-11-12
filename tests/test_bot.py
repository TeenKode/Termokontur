from termokontur import db
from termokontur.bot import interpret, process_update

def test_bot_flow(client):
    room=db.create_room("Главный корпус","1","101","Кабинет",18,24)
    assert "привяжите" in interpret("777","/temp 22","m0").lower()
    code=db.issue_code(room["id"])
    assert "привязан" in interpret("777","/bind "+code,"m1")
    assert "101" in interpret("777","/rooms","m2")
    assert "Сохранено" in interpret("777","/temp 21,5","m3")
    assert "уже записано" in interpret("777","/temp 21,5","m3")
    assert len(db.recent_readings()) == 1
    assert "отвязан" in interpret("777","/unbind "+str(room["id"]),"m4")

def test_bot_only_private_and_real_messages(client):
    msg={"update_type":"message_created","message":{
        "sender":{"user_id":123,"is_bot":False},
        "recipient":{"chat_type":"chat"},
        "body":{"mid":"m2","text":"/help"}}}
    assert process_update(msg) is None
    msg["message"]["recipient"]["chat_type"]="dialog"
    assert process_update(msg)==("123",interpret("123","/help","m2"))
    msg["message"]["sender"]["is_bot"]=True
    assert process_update(msg) is None
