"""«Продолжить на другом устройстве» (adder/handoff.py, PUT/GET /api/now)."""

import pytest
from fastapi.testclient import TestClient

from adder import config

LAPTOP = "laptop-0001"
PHONE = "phone-00002"
TRACK = {"path": "A/Singles/a.m4a", "artist": "A", "title": "a"}
QUEUE = {"v": 1, "queue": [TRACK, {"path": "B/Singles/b.m4a"}], "index": 0, "shuffle": False}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    from adder import app as app_module

    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        yield test_client


def report(client, device, label="ноутбуке", **kw):
    body = {"device": device, "label": label, "track": TRACK, "at": 30, "stamp": 7, "playing": True}
    body.update(kw)
    r = client.put("/api/now", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_other_device_sees_what_played_and_its_queue(client):
    assert client.get("/api/now").json() == {"now": None}
    assert report(client, LAPTOP, claim=True, queue=QUEUE)["yield"] is False
    report(client, LAPTOP, at=42)  # место без очереди — та же метка
    now = client.get("/api/now").json()["now"]
    assert now["device"] == LAPTOP and now["label"] == "ноутбуке"
    assert now["at"] == 42 and now["playing"] is True and now["track"]["path"] == TRACK["path"]
    saved = client.get("/api/now/queue", params={"stamp": 7}).json()
    assert [t["path"] for t in saved["queue"]] == ["A/Singles/a.m4a", "B/Singles/b.m4a"]
    # Метка другая — эта очередь уже не та.
    assert client.get("/api/now/queue", params={"stamp": 8}).status_code == 404


def test_a_new_queue_without_its_contents_is_asked_for(client):
    report(client, LAPTOP, claim=True, queue=QUEUE)
    answer = report(client, LAPTOP, stamp=8)  # очередь сменилась, а прислали только место
    assert answer.get("need_queue") is True
    assert client.get("/api/now").json()["now"]["stamp"] == 7


def test_continue_on_the_phone_pauses_the_laptop(client):
    report(client, LAPTOP, claim=True, queue=QUEUE)
    assert (
        client.post("/api/now/take", json={"device": PHONE, "label": "айфоне"}).status_code == 200
    )
    report(client, PHONE, label="айфоне", claim=True, queue=QUEUE, stamp=9)
    # Ноутбук ещё играет — следующий его отчёт: встань на паузу.
    answer = report(client, LAPTOP)
    assert answer == {"yield": True, "to": "айфоне"}
    # Пока ноутбук на паузе, запись — про телефон.
    report(client, LAPTOP, playing=False)
    assert client.get("/api/now").json()["now"]["device"] == PHONE
    # Нажали «играть» на ноутбуке — теперь пауза телефону.
    assert report(client, LAPTOP, claim=True, queue=QUEUE)["yield"] is False
    assert report(client, PHONE, label="айфоне", stamp=9)["yield"] is True


def test_a_huge_or_empty_queue_is_refused(client):
    body = {"device": LAPTOP, "track": TRACK, "stamp": 1, "queue": {"queue": []}}
    assert client.put("/api/now", json=body).status_code == 422
    body["queue"] = {"queue": [TRACK] * 20001}
    assert client.put("/api/now", json=body).status_code == 422


def test_now_needs_the_token(client):
    client.headers.pop("Authorization")
    assert client.get("/api/now").status_code == 401
    assert client.put("/api/now", json={"device": LAPTOP}).status_code == 401


def test_a_claim_without_a_queue_is_not_lost(client):
    """Рецензия 24.11: телефон перезаписал очередь, ноутбук нажал «играть», не
    прислав свою (та же метка), — сервер ответил need_queue и забыл заявку, и
    следующий отчёт ноутбука получил «пауза»."""
    report(client, LAPTOP, claim=True, queue=QUEUE)
    client.post("/api/now/take", json={"device": PHONE, "label": "айфоне"})
    report(client, PHONE, label="айфоне", claim=True, queue=QUEUE, stamp=9)
    answer = report(client, LAPTOP, claim=True)  # без очереди
    assert answer.get("need_queue") is True and answer["yield"] is False
    assert report(client, LAPTOP)["yield"] is False
    assert report(client, PHONE, label="айфоне", stamp=9)["yield"] is True


def test_the_place_of_a_silent_device_is_not_counted_on(client, monkeypatch):
    from adder import handoff

    clock = [1000.0]
    monkeypatch.setattr(handoff.time, "time", lambda: clock[0])
    track = dict(TRACK, duration=100)
    report(client, LAPTOP, claim=True, queue=QUEUE, track=track, at=30)
    clock[0] += 12
    now = client.get("/api/now").json()["now"]
    assert now["playing"] is True and now["at"] == 42 and now["age"] == 12
    clock[0] += 200  # трек кончился бы — дальше конца не считаем
    now = client.get("/api/now").json()["now"]
    # Молчит дольше 30 с: закрыли вкладку — «играло», место — последнее.
    assert now["playing"] is False and now["at"] == 30
    clock[0] = 1000.0 + 25
    assert client.get("/api/now").json()["now"]["at"] == 55
    report(client, LAPTOP, track=track, at=95)
    clock[0] += 20
    assert client.get("/api/now").json()["now"]["at"] == 99  # конец трека − 1 с
