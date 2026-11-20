"""Колода обычного перемешивания (adder/deck.py, /api/deck/*, разбор 19.11.2026)."""

import random
import time

import pytest
from fastapi.testclient import TestClient

from adder import config, db, deck, library, navidrome


def _rows(artists: int = 12, per: int = 10, collabs: int = 6) -> list[dict]:
    rows = []
    for a in range(artists):
        for t in range(per):
            rows.append(
                {
                    "path": f"A{a:02}/t{t:02}.m4a",
                    "artist": f"Артист {a}",
                    "album": f"Альбом {a}-{t % 3}",
                    "title": f"{a}-{t}",
                    "duration": 180.0,
                }
            )
    # Совместные: второй артист должен сторожиться так же, как первый.
    for c in range(collabs):
        rows.append(
            {
                "path": f"F/feat{c}.m4a",
                "artist": f"Артист {c} • Артист {c + 1}",
                "album": "",
                "title": f"feat {c}",
                "duration": 180.0,
            }
        )
    return rows


ROWS = _rows()
META = {r["path"]: r for r in ROWS}


@pytest.fixture(autouse=True)
def _fresh_db():
    db.db_init()
    deck.forget_leases()
    yield
    deck.forget_leases()


def _distances(order: list[str]) -> dict[str, int]:
    last: dict[str, int] = {}
    closest: dict[str, int] = {}
    for i, path in enumerate(order):
        for name in deck.artists_of(META[path]):
            if name in last:
                closest[name] = min(closest.get(name, 10**9), i - last[name])
            last[name] = i
    return closest


# ---------------------------------------------------------------------------
# Сборка


def test_one_artist_never_lands_closer_than_its_gap():
    """У каждого артиста ~10 треков из 126: равномерный шаг ~12, держим ≥ 6
    (половина шага), совместные треки — для обоих артистов."""
    paths = [r["path"] for r in ROWS]
    gaps = deck._gaps(paths, META)
    for seed in range(40):
        order = deck.build(paths, META, random.Random(seed))
        assert sorted(order) == sorted(paths)
        for name, distance in _distances(order).items():
            assert distance >= gaps[name], (seed, name, distance)


def test_plain_random_order_would_fail_the_same_check():
    """Проверка выше что-то значит: обычная тасовка её не проходит."""
    paths = [r["path"] for r in ROWS]
    gaps = deck._gaps(paths, META)
    failures = 0
    for seed in range(40):
        order = list(paths)
        random.Random(seed).shuffle(order)
        if any(d < gaps[n] for n, d in _distances(order).items()):
            failures += 1
    assert failures >= 35


def test_the_tail_of_the_old_deck_stays_out_of_the_head_of_the_new():
    paths = [f"x/{i:03}.m4a" for i in range(300)]
    meta = {p: {"artist": f"a{i % 30}", "album": ""} for i, p in enumerate(paths)}
    old = deck.build(paths, meta, random.Random(1))
    tail = old[-100:]
    for seed in range(10):
        new = deck.build(paths, meta, random.Random(seed), avoid=tail)
        assert not set(new[:100]) & set(tail)
        assert sorted(new) == sorted(paths)


def test_a_small_set_keeps_a_third_apart():
    paths = [f"s/{i}.m4a" for i in range(30)]
    meta = {p: {"artist": f"a{i}", "album": ""} for i, p in enumerate(paths)}
    old = deck.build(paths, meta, random.Random(3))
    new = deck.build(paths, meta, random.Random(4), avoid=old[-10:])
    assert not set(new[:10]) & set(old[-10:])


# ---------------------------------------------------------------------------
# Через API


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(navidrome, "configured", lambda: False)
    config.LIBRARY.mkdir(parents=True, exist_ok=True)
    library.invalidate_library_index()
    rows = [dict(r) for r in ROWS]
    monkeypatch.setattr(library, "library_index", lambda: [dict(r) for r in rows])

    from adder import app as app_module

    # Здесь целые круги колоды за секунду — предел «60 в минуту» не про это.
    monkeypatch.setattr(app_module, "PLAYS_PER_MINUTE", 10**6)
    app_module._PLAYS_WINDOW.clear()
    with TestClient(app_module.app) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-secret"})
        test_client.rows = rows  # type: ignore[attr-defined]
        yield test_client


def take(client, device="laptop", playlist="", paths=None):
    body = {"device": device, "playlist": playlist}
    if paths is not None:
        body["paths"] = paths
    r = client.post("/api/deck/take", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def play(client, path, *, deck_tag=None, skipped=False, seconds=170.0, heard_at=None):
    body = {
        "path": path,
        "played_seconds": 5.0 if skipped else seconds,
        "duration": 180.0,
        "skipped": skipped,
        "mode": "plain",
        "shuffled": True,
        "heard_at": heard_at
        if heard_at is not None
        else time.time() - (5.0 if skipped else seconds),
    }
    if deck_tag:
        body["deck"] = deck_tag
    r = client.post("/api/plays", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_cursor_moves_with_plays_not_with_taking(client):
    first = take(client)
    assert first["round"] == 1
    assert sorted(first["order"]) == sorted(r["path"] for r in ROWS)
    # Взять ещё раз, ничего не сыграв, — та же колода с того же места.
    again = take(client)
    assert again["order"] == first["order"]
    tag = {"key": first["key"], "round": first["round"]}
    for path in first["order"][:5]:
        play(client, path, deck_tag=tag)
    after = take(client)
    assert after["order"] == first["order"][5:]
    assert after["left"] == len(ROWS) - 5


def test_a_new_session_after_a_restart_continues_from_the_cursor(client):
    first = take(client)
    tag = {"key": first["key"], "round": 1}
    for path in first["order"][:7]:
        play(client, path, deck_tag=tag)
    # Перезапуск службы: память (бронь) пропадает, база остаётся.
    deck.forget_leases()
    from adder import app as app_module

    with TestClient(app_module.app) as second:
        second.headers.update({"Authorization": "Bearer test-secret"})
        resumed = take(second, device="phone")
    assert resumed["order"] == first["order"][7:]
    assert resumed["round"] == 1


def test_two_devices_take_from_one_deck_without_playing_the_same(client):
    laptop = take(client, device="laptop")
    phone = take(client, device="phone")
    held = set(laptop["order"][: deck.LEASE_SIZE])
    assert not held & set(phone["order"][: deck.LEASE_SIZE])
    assert phone["order"] == [p for p in laptop["order"] if p not in held]
    # Телефон сыграл своё — ноутбук при сверке это видит.
    tag = {"key": phone["key"], "round": 1}
    for path in phone["order"][:3]:
        play(client, path, deck_tag=tag)
    again = take(client, device="laptop")
    assert not set(phone["order"][:3]) & set(again["order"])
    # Бронь истекла — занятое снова выдаётся другим.
    deck.forget_leases()
    assert (
        take(client, device="phone")["order"][: deck.LEASE_SIZE]
        == again["order"][: deck.LEASE_SIZE]
    )


def test_plays_heard_without_network_count_when_they_arrive(client):
    first = take(client)
    tag = {"key": first["key"], "round": 1}
    played_long_ago = time.time() - 3 * 3600
    # Ушли из очереди без сети через три часа — засчитываются по heard_at.
    for i, path in enumerate(first["order"][:4]):
        play(client, path, deck_tag=tag, heard_at=played_long_ago + i * 200)
    assert take(client)["order"] == first["order"][4:]


def test_a_late_play_from_the_previous_round_does_not_touch_the_new_one(client):
    first = take(client)
    key = first["key"]
    for path in first["order"]:
        play(client, path, deck_tag={"key": key, "round": 1})
    second = take(client)
    assert second["round"] == 2
    assert len(second["order"]) == len(ROWS)
    # Прослушивание первого круга, пришедшее без сети после смены круга.
    stale = second["order"][0]
    play(client, stale, deck_tag={"key": key, "round": 1}, heard_at=time.time() - 50 * 60)
    # И без метки, но услышанное до начала круга.
    play(client, second["order"][1], heard_at=time.time() - 3 * 3600)
    assert take(client)["order"] == second["order"]


def test_the_next_round_is_handed_out_early_and_starts_with_its_first_play(client):
    first = take(client)
    key = first["key"]
    for path in first["order"][: len(ROWS) - 10]:
        play(client, path, deck_tag={"key": key, "round": 1})
    near = take(client)
    assert near["left"] == 10
    assert len(near["next"]) == min(deck.NEXT_SHOWN, len(ROWS))
    # Хвост круга не открывает следующий.
    assert not set(near["order"]) & set(near["next"][: deck.junction_size(len(ROWS))])
    # Плеер дошёл до конца круга и играет первый трек следующего.
    play(client, near["next"][0], deck_tag={"key": key, "round": 2})
    after = take(client)
    assert after["round"] == 2
    assert near["next"][0] not in after["order"]
    assert after["order"][: len(near["next"]) - 1] == near["next"][1:]


def test_an_early_skip_moves_the_track_to_the_second_half_once(client):
    first = take(client)
    tag = {"key": first["key"], "round": 1}
    skipped = first["order"][0]
    play(client, skipped, deck_tag=tag, skipped=True)
    after = take(client)
    assert skipped in after["order"]
    assert after["order"].index(skipped) >= (len(after["order"]) - 1) // 2
    # Второй ранний пропуск за круг — уже «сыграно».
    play(client, skipped, deck_tag=tag, skipped=True, heard_at=time.time() - 1)
    assert skipped not in take(client)["order"]


def test_a_play_anywhere_counts_in_every_deck_that_has_the_track(client):
    (config.LIBRARY / "Мой.m3u").write_text(
        "\n".join(r["path"] for r in ROWS[:20]) + "\n", encoding="utf-8"
    )
    playlist = take(client, playlist="Мой")
    library_deck = take(client)
    # Включён руками, без колоды.
    path = playlist["order"][0]
    play(client, path)
    assert path not in take(client, playlist="Мой")["order"]
    assert path not in take(client)["order"]
    assert library_deck["key"] == deck.LIBRARY_KEY


def test_repeated_rows_are_one_track_and_new_ones_land_in_the_rest(client):
    paths = [r["path"] for r in ROWS[:30]]
    (config.LIBRARY / "Повторы.m3u").write_text(
        "\n".join(paths + paths[:10]) + "\n", encoding="utf-8"
    )
    first = take(client, playlist="Повторы")
    assert sorted(first["order"]) == sorted(paths)
    tag = {"key": first["key"], "round": 1}
    for path in first["order"][:5]:
        play(client, path, deck_tag=tag)
    added = ROWS[40]["path"]
    removed = first["order"][10]
    kept = [p for p in paths if p != removed] + [added]
    (config.LIBRARY / "Повторы.m3u").write_text("\n".join(kept) + "\n", encoding="utf-8")
    after = take(client, playlist="Повторы")
    assert added in after["order"]
    assert removed not in after["order"]
    assert set(after["order"]) == set(kept) - set(first["order"][:5])


def test_reshuffle_keeps_what_was_heard_out(client):
    first = take(client)
    tag = {"key": first["key"], "round": 1}
    for path in first["order"][:10]:
        play(client, path, deck_tag=tag)
    r = client.post("/api/deck/reshuffle", json={"device": "laptop"})
    assert r.status_code == 200
    again = r.json()
    assert set(again["order"]) == set(first["order"][10:])
    assert again["order"] != first["order"][10:]


def test_only_limits_the_answer_to_the_queue(client):
    only = [r["path"] for r in ROWS[:12]]
    answer = take(client, paths=only)
    assert sorted(answer["order"]) == sorted(only)


def test_an_unknown_playlist_is_404(client):
    assert client.post("/api/deck/take", json={"playlist": "нет такой"}).status_code == 404


def test_stats_count_repeats_over_three_days(client):
    first = take(client)
    tag = {"key": first["key"], "round": 1}
    a, b = first["order"][:2]
    play(client, a, deck_tag=tag, heard_at=time.time() - 2 * 3600)
    play(client, b, deck_tag=tag, heard_at=time.time() - 3600)
    play(client, a, deck_tag=tag, heard_at=time.time() - 600)
    # Неделю назад — за окном.
    db.db_exec(
        "INSERT INTO plays(path, played_at, played_seconds, duration, skipped, source, mode, shuffled) "
        "VALUES(?, datetime('now', 'localtime', '-7 days'), 170, 180, 0, 'player', 'plain', 1)",
        (b,),
    )
    # Игра по порядку — не перемешивание.
    db.db_exec(
        "INSERT INTO plays(path, played_at, played_seconds, duration, skipped, source, mode, shuffled) "
        "VALUES(?, datetime('now', 'localtime'), 170, 180, 0, 'player', 'manual', 0)",
        (b,),
    )
    # Умное — не колода; ранний пропуск колода нарочно возвращает позже.
    for mode, skipped, seconds in (("smart", 0, 170), ("plain", 1, 4)):
        db.db_exec(
            "INSERT INTO plays(path, played_at, played_seconds, duration, skipped, source, mode,"
            " shuffled) VALUES(?, datetime('now', 'localtime'), ?, 180, ?, 'player', ?, 1)",
            (a, seconds, skipped, mode),
        )
    repeats = client.get("/api/stats?period=month").json()["repeats"]
    assert repeats == {"days": 3, "plays": 3, "repeats": 1}


# ---------------------------------------------------------------------------
# Замечания рецензии 19.11.2026


def test_a_device_finishing_the_old_round_after_the_roll_still_counts(client):
    """Телефон начал новый круг, ноутбук ещё доигрывает свои забронированные
    треки прошлого: их прослушивания не теряются, и в новом круге эти треки
    не звучат снова сразу."""
    laptop = take(client, device="laptop")
    key = laptop["key"]
    held = laptop["order"][: deck.LEASE_SIZE]
    phone = take(client, device="phone")
    for path in phone["order"]:
        play(client, path, deck_tag={"key": key, "round": 1})
    near = take(client, device="phone")
    assert set(near["order"]) == set(held)  # остались только чужие брони — отдаются, лучше повтор
    nxt = near["next"]
    play(client, nxt[0], deck_tag={"key": key, "round": 2})
    assert take(client, device="phone")["round"] == 2
    # Ноутбук доигрывает свой трек прошлого круга уже после смены круга.
    late = held[0]
    play(client, late, deck_tag={"key": key, "round": 1}, heard_at=time.time() - 1)
    assert late not in take(client, device="phone")["order"]


def test_shuffling_a_queue_of_heard_tracks_keeps_the_whole_queue(client):
    """Кнопка в плеере над двадцатью найденными, уже слышанными в круге: играет
    вся очередь, а не пустой маршрут."""
    first = take(client)
    tag = {"key": first["key"], "round": 1}
    only = first["order"][:20]
    for path in only[:12]:
        play(client, path, deck_tag=tag)
    answer = take(client, paths=only)
    assert sorted(answer["order"]) == sorted(only)
    # Несыгранное — первым.
    assert set(answer["order"][:8]) == set(only[12:])


def test_reshuffle_keeps_the_next_round_another_device_may_be_playing(client):
    first = take(client)
    key = first["key"]
    for path in first["order"][: len(ROWS) - 10]:
        play(client, path, deck_tag={"key": key, "round": 1})
    nxt = take(client)["next"]
    r = client.post("/api/deck/reshuffle", json={"device": "laptop"})
    assert r.json()["next"] == nxt


def test_a_play_writes_only_the_decks_it_changes(client):
    (config.LIBRARY / "Мой.m3u").write_text(ROWS[0]["path"] + "\n", encoding="utf-8")
    first = take(client)
    take(client, playlist="Мой")
    # Трек не из «Моего»: подборку не трогаем.
    path = next(p for p in first["order"] if p != ROWS[0]["path"])
    assert deck.heard(
        path,
        started=time.time() - 170,
        played_seconds=170,
        skipped=False,
        source_key=first["key"],
        source_round=1,
    ) == [deck.LIBRARY_KEY]
    # Повтор уже сыгранного — ничего не пишет.
    assert (
        deck.heard(
            path,
            started=time.time() - 170,
            played_seconds=170,
            skipped=False,
            source_key=first["key"],
            source_round=1,
        )
        == []
    )


# ---------------------------------------------------------------------------
# Замечания критика 19.11.2026


def test_a_library_read_mid_rescan_does_not_wipe_the_round(client):
    first = take(client)
    tag = {"key": first["key"], "round": 1}
    for path in first["order"][:30]:
        play(client, path, deck_tag=tag)
    full = list(client.rows)
    client.rows[:] = full[:10]  # индекс посреди пересканирования
    take(client)
    client.rows[:] = full
    assert take(client)["order"] == first["order"][30:]


def test_a_replaced_track_keeps_its_place_and_heard_mark(client):
    first = take(client)
    tag = {"key": first["key"], "round": 1}
    old = first["order"][0]
    play(client, old, deck_tag=tag)
    new = "Новый/путь.m4a"
    deck.rename_path(old, new)
    for row in client.rows:
        if row["path"] == old:
            row["path"] = new
    assert new not in take(client)["order"]


def test_an_early_skip_moves_a_track_only_in_its_own_deck(client):
    (config.LIBRARY / "Мой.m3u").write_text(
        "\n".join(r["path"] for r in ROWS[:20]) + "\n", encoding="utf-8"
    )
    playlist = take(client, playlist="Мой")
    library_order = take(client)["order"]
    path = playlist["order"][0]
    play(client, path, deck_tag={"key": playlist["key"], "round": 1}, skipped=True)
    assert take(client)["order"] == library_order
    # Пропуск без колоды (альбом по порядку) — тоже ничего не двигает.
    play(client, library_order[0], skipped=True)
    assert take(client)["order"] == library_order
    assert take(client, playlist="Мой")["order"][0] != path


def test_a_new_track_does_not_open_the_next_round(client):
    first = take(client)
    key = first["key"]
    for p in first["order"][: len(ROWS) - 10]:
        play(client, p, deck_tag={"key": key, "round": 1})
    near = take(client)
    added = {"path": "Новый/трек.m4a", "artist": "Новый", "album": "", "title": "н"}
    client.rows.append(added)
    after = take(client)
    assert added["path"] in after["order"]
    assert added["path"] not in after["next"][: deck.junction_size(len(ROWS) + 1)]
    assert len(near["next"]) <= len(after["next"])


def test_forgotten_decks_are_pruned(client):
    take(client)
    db.db_exec("UPDATE decks SET updated_at = ?", (time.time() - 200 * 86400,))
    deck.prune(90)
    assert db.db_query("SELECT COUNT(*) AS n FROM decks")[0]["n"] == 0


def test_an_unreadable_library_leaves_playlist_decks_alone(client):
    (config.LIBRARY / "Мой.m3u").write_text(
        "\n".join(r["path"] for r in ROWS[:20]) + "\n", encoding="utf-8"
    )
    first = take(client, playlist="Мой")
    tag = {"key": first["key"], "round": 1}
    for path in first["order"][:5]:
        play(client, path, deck_tag=tag)
    full = list(client.rows)
    client.rows[:] = []  # диск не смонтирован
    r = client.post("/api/deck/take", json={"playlist": "Мой", "device": "laptop"})
    assert r.status_code == 503
    client.rows[:] = full
    assert take(client, playlist="Мой")["order"] == first["order"][5:]


def test_renaming_a_playlist_keeps_its_deck(client):
    """Хвост этапа 2: переименование подборки начинало колоду заново, старая
    лежала до prune. Теперь колода идёт за подборкой; колода под новым именем,
    если уже есть, не затирается."""
    (config.LIBRARY / "Мой.m3u").write_text(
        "\n".join(r["path"] for r in ROWS[:20]) + "\n", encoding="utf-8"
    )
    first = take(client, playlist="Мой")
    tag = {"key": first["key"], "round": 1}
    for path in first["order"][:5]:
        play(client, path, deck_tag=tag)

    r = client.patch("/api/playlists/Мой", json={"name": "Новый"})
    assert r.status_code == 200, r.text

    after = take(client, playlist="Новый")
    assert after["key"] == deck.playlist_key("Новый")
    assert after["order"] == first["order"][5:]
    assert after["round"] == 1
    assert not db.db_query("SELECT 1 FROM decks WHERE key = ?", (deck.playlist_key("Мой"),))


def test_a_rename_onto_the_name_of_a_deleted_playlist_drops_its_old_deck(client):
    """Колода под новым именем — от удалённой подборки: она не продолжается."""
    (config.LIBRARY / "А.m3u").write_text(ROWS[0]["path"] + "\n", encoding="utf-8")
    take(client, playlist="А")
    db.db_exec(
        "INSERT INTO decks(key, state, updated_at) VALUES(?, ?, ?)",
        (deck.playlist_key("Б"), '{"order": ["x"], "round": 7}', time.time()),
    )
    assert deck.rename_playlist("А", "Б") is True
    state = db.db_query("SELECT state FROM decks WHERE key = ?", (deck.playlist_key("Б"),))
    assert '"round": 7' not in state[0]["state"]
    assert not db.db_query("SELECT 1 FROM decks WHERE key = ?", (deck.playlist_key("А"),))
