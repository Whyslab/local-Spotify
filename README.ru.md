# 🎵 local-Spotify

*[English version](README.md)*

[![CI](https://github.com/Whyslab/local-Spotify/actions/workflows/ci.yml/badge.svg)](https://github.com/Whyslab/local-Spotify/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](adder/requirements.txt)

**Self-hosted музыкальная библиотека: закидываешь ссылку на YouTube — на выходе трек с обложкой и метаданными в личной медиатеке, доступной с телефона через Navidrome.**

`local-Spotify` — это небольшой сервис для дома/домашней сети, который забирает аудио с YouTube, приводит его к единому виду (нормализованные имена, ID3/MP4-теги, обложка в HD) и складывает в библиотеку, которую раздаёт [Navidrome](https://www.navidrome.org/) по Subsonic API. С телефона это выглядит как собственный Spotify: [Amperfy](https://github.com/BLL-Games/Amperfy), play\:Sub, DSub и любой другой Subsonic-клиент подключаются к нему как к обычному стриминговому сервису.

Проект не предназначен для публичного SaaS или обхода ограничений YouTube — это инструмент для личного использования одним человеком/семьёй в доверенной сети.

---

## Содержание

* [Возможности](#-возможности)
* [Архитектура](#️-архитектура)
* [Быстрый старт](#-быстрый-старт)
* [Конфигурация](#️-конфигурация)
* [API](#-api)
* [Production: systemd](#-production-systemd)
* [Тесты](#-тесты)
* [Решение проблем](#-решение-проблем)
* [Безопасность](#-безопасность)
* [Структура проекта](#-структура-проекта)
* [Ограничения](#️-ограничения)
* [Roadmap](#️-roadmap)
* [Лицензия](#-лицензия)

---

## ✨ Возможности

* **Добавление музыки по ссылке** — POST-запрос со списком YouTube URL, остальное сервис делает сам.
* **Фоновая очередь с несколькими воркерами** — загрузки не блокируют API и выполняются параллельно (`MAX_WORKERS`).
* **Автоматическая очистка метаданных** — `Song (Official Video) [4K]` превращается в чистые `Artist / Song`, при этом версии вида `(Live)`/`(Remix)` сохраняются в тегах.
* **HD-обложки** — iTunes Search API с фолбэком на превью YouTube; отдельный скрипт (`fix_covers.py`) добивает обложки постфактум через iTunes → Deezer.
* **Дедупликация по содержимому** — перед сохранением трек хешируется (SHA-256) и сверяется с уже имеющимися файлами в библиотеке, а не только по имени.
* **Retry с экспоненциальным backoff** — временные сетевые ошибки и сбои загрузки повторяются автоматически, постоянные — нет.
* **Graceful shutdown и recovery** — `SIGTERM` останавливает воркеры корректно (включая дочерние процессы `yt-dlp`/`ffmpeg`); незавершённые задачи переживают перезапуск сервиса.
* **Проверка целостности файлов** — каждый M4A валидируется до и после записи метаданных, битые файлы не попадают в библиотеку.
* **Контроль ресурсов** — лимиты на размер очереди, число ссылок в запросе и свободное место на диске.
* **API защищён Bearer-токеном**; без токена health-check отвечает только «жив ли сервис».
* **Подборки — это файлы.** Каждая подборка лежит в корне фонотеки как `.m3u` и правится перезаписью файла: Navidrome перечитывает её примерно за шесть секунд.
* **Правка с телефона не теряется.** Navidrome принимает перестановку, присланную клиентом, но в `.m3u` её не пишет — и собственный сторож стирает её при следующем изменении файла. Раз в тридцать секунд сервис ищет подборку, чью базу правили мимо файла, и переносит порядок в файл. Замерено: правка доходит до `.m3u` за десять секунд.
* **Перестановка не путается в дубликатах** — строка опознаётся по номеру, а не по пути. В `Monday.m3u` девятнадцать треков стоят дважды.
* **Одновременные правки отвергаются, а не сливаются** — при чтении выдаётся хеш файла, при записи его надо вернуть; правка на устаревшем состоянии получает `409` вместо тихой перезаписи чужой.
* **Обложки подборок** — хранятся локально и реплицируются в Navidrome, поэтому переименование становится повторной загрузкой, а не потерей. Тип определяется по содержимому файла, а не по имени.
* **Плеер** — одна страница в двух ширинах; звук отдаётся по короткоживущим подписанным ссылкам, чтобы `<audio>` умел перемотку, а токен не попадал в адресную строку.
* **Журнал прослушиваний** — Navidrome хранит счётчик и дату последнего раза, но не журнал. Здесь пишется трек, время, докуда доиграл и был ли пропущен.
* **Перемешивание с формой** — ни жанров, ни BPM в фонотеке нет, поэтому `scripts/analyze_audio.py` измеряет темп, энергию, яркость и тональность по самому звуку, а очередь не набирается случайно, а обходится: соседи близки по темпу, артист не идёт сам за собой, недавно слышанное возвращается редко. Подробности — в `scripts/README.md`.
* **Веб-интерфейс** — минимальная SPA-страница для добавления ссылок и просмотра очереди (`web/`).
* **Инструменты для аудита библиотеки** — офлайн-скрипты для поиска дублей, проверки метаданных и массовой миграции плейлиста из CSV.

---

## 🏗️ Архитектура

```text
                    ┌──────────────┐
                    │   iPhone /    │
                    │   Android     │
                    │   (Amperfy)   │
                    └──────┬───────┘
                           │ Subsonic API
                           ▼
                    ┌──────────────┐        читает файлы
                    │  Navidrome   │───────────────────────┐
                    └──────────────┘                       │
                                                             ▼
┌──────────┐   POST /api/add   ┌──────────────┐   ┌──────────────────┐
│  клиент  │ ─────────────────▶│  adder API   │   │ Normalized Library│
│ (curl/UI)│                   │  (FastAPI)   │   │  Artist/Singles/  │
└──────────┘                   └──────┬───────┘   └────────▲──────────┘
                                       │ task queue                 │
                                       ▼                             │
                              ┌──────────────────┐                   │
                              │  N worker-потоков │───────────────────┘
                              │  yt-dlp → ffmpeg  │   валидация + запись
                              │  → mutagen (теги) │   после успешной проверки
                              └──────────────────┘
```

Ключевой принцип: файл никогда не попадает в библиотеку напрямую. Загрузка и обработка идут во временной директории (`adder/tmp/`), и только после успешной проверки метаданных, целостности и отсутствия дубликата происходит атомарное перемещение в `Normalized Library`.

---

## 🚀 Быстрый старт

Проверено на Debian/Ubuntu. Каждый шаг заканчивается командой, которая показывает, что он удался — не переходи дальше, пока она не сработает.

### 1. Системные пакеты

| Что | Зачем | Проверка |
| --- | --- | --- |
| Linux с systemd | продакшен-юнит (шаг 7) | `systemctl --user status` |
| Python **3.12+** с `venv` | сам сервис | `python3 --version` |
| **FFmpeg** (с `ffprobe`) | yt-dlp извлекает им аудио; без него падает каждое скачивание | `ffmpeg -version && ffprobe -version` |
| **Deno** | yt-dlp нужен JavaScript-рантайм для JS-проверок плеера YouTube; по умолчанию он использует Deno | `deno --version` |
| git | клонирование | `git --version` |

```bash
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg git curl unzip

# Deno в /usr/local/bin, чтобы systemd-сервис нашёл его в своём PATH по умолчанию.
# (Официальный установщик кладёт его в ~/.deno/bin — он есть в PATH шелла, но не сервиса.)
curl -fsSLo /tmp/deno.zip \
  "https://github.com/denoland/deno/releases/latest/download/deno-$(uname -m)-unknown-linux-gnu.zip"
sudo unzip -o /tmp/deno.zip -d /usr/local/bin
deno --version
```

В Ubuntu 22.04 и старше `python3` — это 3.10/3.11: поставь 3.12 (например, из PPA deadsnakes как `python3.12` + `python3.12-venv`) и используй дальше `python3.12` вместо `python3`.

### 2. Клонирование

```bash
git clone https://github.com/Whyslab/local-Spotify.git
cd local-Spotify
```

Все команды ниже выполняются из этого каталога.

### 3. Виртуальное окружение и зависимости

```bash
python3 -m venv .venv
.venv/bin/pip install -r adder/requirements.txt
.venv/bin/python -m yt_dlp --version     # печатает версию, например 2026.08.19
```

Необязательно: умное перемешивание измеряет темп, тональность и энергию каждого трека через librosa. Она вынесена из основных зависимостей, потому что тянет компиляторы. Без неё всё остальное работает:

```bash
.venv/bin/pip install -r scripts/requirements-analysis.txt
```

venv должен лежать именно в `.venv` в корне репозитория: там его ищут `deploy/install.sh` и systemd-юнит.

### 4. Настройка

```bash
cp .env.example adder/.env
TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
sed -i "s|^API_TOKEN=.*|API_TOKEN=$TOKEN|" adder/.env
grep '^API_TOKEN=' adder/.env             # НЕ должно быть CHANGE_ME_...
```

Это единственная обязательная настройка. С пустым токеном или заглушкой из `.env.example` сервис не запустится.

Необязательно, в `adder/.env`:

* `LIBRARY_PATH` — куда складывать музыку. По умолчанию `~/Music/Normalized Library`. Чтобы изменить, раскомментируй строку и укажи абсолютный путь **без кавычек**. Navidrome должен читать эту же папку (шаг 6).
* `COOKIES_FROM_BROWSER` — только если YouTube начнёт отвечать «Sign in to confirm you're not a bot» (см. [Решение проблем](#-решение-проблем)).

Полный список — в разделе [Конфигурация](#️-конфигурация).

### 5. Запуск и проверка

```bash
.venv/bin/python -m adder.server
```

Сервис слушает `http://0.0.0.0:8787`. Во втором терминале:

```bash
TOKEN=$(grep '^API_TOKEN=' adder/.env | cut -d= -f2-)
curl -s http://127.0.0.1:8787/health -H "Authorization: Bearer $TOKEN"
```

Без токена `/health` отвечает только `{"status": ...}`. Ожидается `"status":"healthy"`, `"ffmpeg":"ok"` и `"js_runtime":"ok"`. Ответ `503` с `"ffmpeg":"missing"` значит, что шаг 1 не завершён; то же самое написано в логе запуска.

Добавить трек:

```bash
curl -X POST http://127.0.0.1:8787/api/add \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"links": ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]}'
# {"added":[1]}

curl -s http://127.0.0.1:8787/api/tasks -H "Authorization: Bearer $TOKEN"
# status: queued -> downloading -> tagging -> done
```

Файл появится как `<LIBRARY_PATH>/<Исполнитель>/Singles/<Название>.m4a`. Строка лога `Task finished: stored` отмечает его; при ошибке в логе будет строка `ERROR` с причиной, а в `/api/tasks` — поля `error` / `error_type`.

Веб-интерфейс — `http://<хост>:8787/`: один раз вставь токен, дальше добавляй ссылки и смотри очередь с телефона.

Перед шагом 7 останови сервис (`Ctrl+C`).

### 6. Navidrome

`local-Spotify` только наполняет папку, раздаёт её [Navidrome](https://www.navidrome.org/). Установи его по [официальной инструкции для Linux](https://www.navidrome.org/docs/installation/linux/) (пакет `.deb`/`.rpm` или архив релиза; сервис должен называться `navidrome`).

Затем `MusicFolder` в Navidrome должен указывать на твой `LIBRARY_PATH`. Шаг 7 сделает это сам, если у Navidrome ещё нет конфига: запишет `/etc/navidrome/navidrome.toml` из `deploy/navidrome.toml.example` и добавит `deploy/navidrome-override.conf`, который разрешает сервису Navidrome читать папку внутри `/home`. Существующий конфиг никогда не перезаписывается — вместо этого скрипт напечатает, какую строку `MusicFolder` проверить.

Для обложек подборок и удаления подборки из Navidrome, когда её `.m3u` удалён, задай ещё `NAVIDROME_USER` и `NAVIDROME_PASSWORD` (пользователь Navidrome) в `adder/.env`. Без них сервис работает, а эта часть ждёт в очереди, пока они не появятся.

Проверка: открой `http://<хост>:4533`, создай администратора — трек из шага 5 появится после сканирования (Navidrome следит за папкой; полное пересканирование — *Settings → Scan*).

### 7. Постоянная работа (systemd)

```bash
./deploy/install.sh
systemctl --user status music-adder       # active (running)
curl -s http://127.0.0.1:8787/health     # {"status":"healthy"}
```

Что делает скрипт — в разделе [Production: systemd](#-production-systemd). Окно приложения с медиаклавишами вокруг той же страницы описано в [`desktop/README.md`](desktop/README.md).

---

## ⚙️ Конфигурация

Все настройки читаются из `adder/.env` (см. `.env.example`); настоящие переменные окружения имеют приоритет. Без валидного `API_TOKEN` сервис не запустится — это осознанное поведение, т.к. API доступен по всей локальной сети.

| Переменная              | По умолчанию                    | Назначение                                             |
| ------------------------ | -------------------------------- | ------------------------------------------------------- |
| `API_TOKEN`               | *(обязательно)*                  | Bearer-токен для доступа к API. Пустой или заглушка из `.env.example` отвергаются |
| `LIBRARY_PATH`             | `~/Music/Normalized Library`     | Путь к библиотеке; должен совпадать с `MusicFolder` в Navidrome. Абсолютный, без кавычек |
| `PORT` / `HOST`            | `8787` / `0.0.0.0`               | Адрес, на котором слушает сервис                        |
| `MAX_WORKERS`              | `2`                               | Число параллельных воркеров загрузки                    |
| `MAX_LINKS_PER_REQUEST`    | `100`                             | Лимит ссылок в одном запросе `/api/add`                 |
| `MAX_QUEUE_SIZE`           | `5000`                            | Максимальный размер очереди задач                       |
| `PRESERVE_FEAT_ARTISTS`    | `true`                            | Сохранять `feat./ft.` в имени папки исполнителя         |
| `MAX_RETRIES`              | `3`                               | Число попыток на задачу при временных ошибках (сеть, HTTP 429) |
| `RETRY_BACKOFF_BASE`       | `2.0`                             | База экспоненциального backoff (сек.)                   |
| `RATE_LIMIT_BACKOFF` | `60` | После ограничения от YouTube (HTTP 429) — секунд ожидания на попытку; всё это время yt-dlp не запускается |
| `MAX_DURATION_MINUTES` | `30` | Самое длинное видео, которое принимается, в минутах; `0` — без ограничения. Прямые эфиры не принимаются никогда |
| `SHUTDOWN_TIMEOUT`         | `30`                              | Таймаут graceful shutdown (сек.)                        |
| `MIN_FREE_SPACE_MB`        | `2048`                            | Минимум свободного места на диске перед загрузкой        |
| `TMP_TTL_HOURS`            | `24`                              | Через сколько часов удаляются зависшие временные файлы   |
| `COOKIES_FROM_BROWSER` | *(пусто)* | Значение `--cookies-from-browser` для yt-dlp: `firefox`, `chrome` или `firefox:/путь/к/профилю` |
| `NAVIDROME_URL` | `http://127.0.0.1:4533` | Navidrome — для обложек подборок и удаления подборок в нём |
| `NAVIDROME_USER` / `NAVIDROME_PASSWORD` | *(пусто)* | Пользователь Navidrome; необязательно, эта работа ждёт в очереди, пока их нет |
| `MAX_COVER_BYTES` | `8388608` | Максимальный размер загружаемой обложки подборки |
| `PLAY_HISTORY_DAYS` | `400` | Сколько дней хранится журнал прослушиваний |
| `DELAY_BETWEEN_TRACKS` | `1.1` | Пауза между треками в офлайн-скриптах, не в сервисе |

Изменения применяются после перезапуска (`systemctl --user restart music-adder`).

---

## 🔌 API

Авторизация — заголовок `Authorization: Bearer <API_TOKEN>`. `/health` без него отвечает только `{"status": ...}`; полный ответ ниже — с токеном.

| Метод  | Путь           | Описание                                    |
| ------ | -------------- | -------------------------------------------- |
| `GET`  | `/health`      | Статус сервиса, БД, библиотеки, ffmpeg, JS-рантайма и очереди |
| `POST` | `/api/add`     | Добавить одну или несколько YouTube-ссылок   |
| `GET`  | `/api/tasks`   | Последние 50 задач и их статус               |
| `GET`  | `/`            | Веб-интерфейс                                |
| `GET`  | `/api/playlists` | Список подборок |
| `POST` | `/api/playlists` | Создать подборку |
| `PATCH` | `/api/playlists/{name}` | Переименовать |
| `DELETE` | `/api/playlists/{name}` | Удалить: файл в `trash/`, подборка убирается и из Navidrome |
| `GET`  | `/api/playlists/{name}/tracks` | Треки подборки и ревизия, нужная для правки |
| `PUT`  | `/api/playlists/{name}/tracks` | Заменить состав и порядок; `409`, если ревизия устарела |
| `POST` `GET` `DELETE` | `/api/playlists/{name}/cover` | Загрузить, отдать или убрать обложку |
| `GET`  | `/api/stream-url` | Выдать короткоживущую подписанную ссылку на трек |
| `GET`  | `/api/stream`  | Отдать трек в `<audio>`; пускает подпись, а не токен |
| `POST` | `/api/plays`   | Записать доигранный или брошенный трек |
| `GET`  | `/api/plays/stats` | Доля пропусков — общая и по режимам очереди |
| `POST` | `/api/import`  | Принять файлы с диска через тот же конвейер |
| `GET`  | `/api/search`  | Найти трек на YouTube, ничего не скачивая |
| `POST` | `/api/import-playlist` | Поставить в очередь плейлист по одной ссылке |
| `GET`  | `/api/shuffle` | Собрать очередь (`mode=smart` или `plain`) |
| `POST` | `/api/shuffle/blind` | Две очереди, по одной каждого вида, без подписей |
| `POST` | `/api/shuffle/blind/{id}` | Записать, какая понравилась |
| `GET`  | `/api/shuffle/blind/results` | Как стоит сравнение |
| `GET`  | `/api/sync`    | Что нашёл последний проход синхронизации |
| `POST` | `/api/sync`    | Пройти сейчас; `?apply=false` — только отчёт |
| `GET`  | `/api/track`   | Теги трека плюс темп, тональность и энергия |
| `GET`  | `/api/cover`   | Обложка из самого файла; `?size=96\|300\|600` — уменьшенная, из кэша |
| `GET` `DELETE` | `/api/library` | Фонотека (поиск, сортировка, порции) / трек в `trash/` |
| `POST` | `/api/replace` `/api/replace-file` | Заменить трек лучшей версией, сохранив его место во всех подборках |
| `GET`  | `/api/home` | Полки главной |
| `GET`  | `/api/discover-external` | Треки похожих артистов, которых нет в фонотеке |
| `POST` | `/api/shuffle/smart` | Умное перемешивание любого набора (альбом, текущая очередь); примерно треть — новое |
| `GET` `POST` | `/api/outside/status` `/api/outside/prefetch` | Состояние новых (не из фонотеки) треков; скачать заранее |
| `POST` | `/api/outside/{key}/keep` | Положить новый трек в фонотеку обычным импортом |
| `GET`  | `/api/lyrics` | Текст песни, где есть — с таймингами |
| `GET` `POST` | `/api/lyrics/candidates` `/api/lyrics/choose` `/api/lyrics/custom` | Выбрать текст руками или вставить свой |

<details>
<summary><code>POST /api/add</code> — пример</summary>

```bash
curl -X POST http://127.0.0.1:8787/api/add \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "links": [
      "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      "https://youtu.be/anotherVideoId"
    ]
  }'
```

```json
{ "added": [12, 13] }
```

Принимаются только ссылки на `youtube.com`, `m.youtube.com`, `music.youtube.com` и `youtu.be`; разные формы одной и той же ссылки на один и тот же ролик приводятся к единому каноническому виду и не создают дубликат задачи. Повторная отправка ссылки с уже завершившейся или выполняющейся задачей игнорируется; ссылку с задачей в статусе `error` можно переотправить, чтобы поставить её в очередь заново.

</details>

<details>
<summary><code>GET /api/tasks</code> — статусы задач и типы ошибок</summary>

`status` — одно из `queued`, `downloading`, `tagging`, `done`, `error`. Для `error` в поле `error` лежит строка ошибки самого yt-dlp, а `error_type` подсказывает, что делать:

| `error_type` | Повтор автоматически | Значение |
| --- | --- | --- |
| `network_error`, `download_error`, `artwork_error` | да | Временный сбой |
| `rate_limited` | да | YouTube ограничивает запросы (HTTP 429 / «try again later») |
| `youtube_not_found` | нет | Видео удалено, приватное или заблокировано в регионе |
| `unsupported_video` | нет | Прямой эфир или видео длиннее `MAX_DURATION_MINUTES` |
| `youtube_auth_required` | нет | Проверка на бота, возрастное ограничение, только для спонсоров: задай `COOKIES_FROM_BROWSER` |
| `dependency_error` | нет | На машине нет ffmpeg/ffprobe |
| `invalid_url`, `filesystem_error`, `database_error`, `metadata_error`, `internal_error`, `unknown_error` | нет | Смотри лог сервиса по id задачи |

Упавшую ссылку можно отправить заново, когда причина устранена.

</details>

<details>
<summary><code>GET /health</code> — пример ответа</summary>

```json
{
  "status": "healthy",
  "database": "ok",
  "library": "ok",
  "library_path": "/home/user/Music/Normalized Library",
  "ffmpeg": "ok",
  "js_runtime": "ok",
  "workers": 2,
  "queue_size": 0,
  "max_queue_size": 5000
}
```

Если БД недоступна, папки библиотеки нет или не установлены ffmpeg/ffprobe, статус становится `unhealthy`, а код ответа — `503`. Отсутствие Deno видно как `"js_runtime": "missing"`, но не делает сервис нездоровым.

</details>

---

## 🖥 Production: systemd

Для постоянной работы в фоне используется user-unit systemd. Установочный скрипт сам проверит наличие `.venv` и корректно заполненного `API_TOKEN`, сгенерирует юнит и (опционально) настроит Navidrome и правила `ufw` для LAN:

```bash
./deploy/install.sh
```

Скрипт не продолжит без `.venv` или с пустым/заглушечным `API_TOKEN`; запишет юнит для `LIBRARY_PATH` и включит lingering; если установлен `navidrome` и у него ещё нет конфига — запишет `/etc/navidrome/navidrome.toml` с тем же `MusicFolder` (существующий конфиг не трогается); если активен `ufw` — откроет Navidrome и сервис для подсети LAN (`LAN_SUBNET=192.168.1.0/24`, чтобы выбрать её самому).

```bash
systemctl --user status music-adder
journalctl --user -u music-adder -f      # на задачу: Processing / Task finished / ERROR
systemctl --user restart music-adder     # после правки adder/.env
```

Обновление:

```bash
git pull
.venv/bin/pip install -r adder/requirements.txt
./deploy/install.sh                      # юнит меняется между версиями
```

Юнит запускает сервис с `WorkingDirectory` в корне репозитория и разрешает запись только в `adder/` (БД и временные файлы), `trash/` (удалённые треки) и путь библиотеки. Одного `ProtectSystem=strict` для этого мало: он монтирует `/` только для чтения, но `/home` — отдельная точка монтирования и остаётся доступной на запись. Закрывает её `ProtectHome=read-only`, а нужные каталоги возвращаются через `ReadWritePaths`.

Резервное копирование состояния (SQLite + `.env`):

```bash
./deploy/backup.sh      # в ~/local-spotify-backups, хранит последние 10
```

---

## 🧪 Тесты

```bash
.venv/bin/pip install ruff               # только для линтера; pytest уже в requirements.txt
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check adder scripts tests
```

Тесты полностью офлайн и не требуют настоящего `.env`; тестам импорта и потоков нужен `ffmpeg` (CI его ставит). `tests/conftest.py` роняет любой тест, который открывает сетевое соединение, поэтому YouTube, Deezer и iTunes всегда замоканы. Покрыто:

* **скачивание** — команды yt-dlp, разбор его JSON и строки `ERROR:`, запуск подпроцесса (таймауты, остановка, большой вывод), какие ошибки повторяются;
* **метаданные** — разбор заголовков YouTube на исполнителя и название, обогащение из Deezer на подготовленных ответах API;
* **запись в библиотеку** — `process()` целиком на настоящем AAC-файле длиной 1 с (`tests/fixtures/tone.m4a`): теги читаются обратно через mutagen, раскладка `Artist/Singles/Title.m4a`, запасная обложка, битые файлы, дедупликация по содержимому;
* API: авторизация, валидация и канонизация ссылок, удаление и path traversal, `/health`, восстановление задач после рестарта, graceful shutdown и XSS-регрессия во фронтенде;
* подборки, синхронизация с Navidrome, подписи потоков, импорт файлов, тексты песен, перемешивания и скачивания умного перемешивания.

CI (`.github/workflows/ci.yml`) на каждый push и pull request запускает `ruff check`, `ruff format --check`, `compileall` и весь набор тестов в чистом окружении.

---

## 🛠 Решение проблем

Начни с `curl -s http://127.0.0.1:8787/health -H "Authorization: Bearer $TOKEN"` и `journalctl --user -u music-adder -n 50`.

| Симптом | Причина и решение |
| --- | --- |
| `RuntimeError: API_TOKEN is required` при запуске | Токен пустой или всё ещё заглушка. Повтори [шаг 4](#4-настройка). |
| `PermissionError` при создании библиотеки на старте | `LIBRARY_PATH` указывает туда, куда нельзя писать. Исправь в `adder/.env` или закомментируй для значения по умолчанию. |
| `/health` → `"ffmpeg": "missing"`, задачи падают с `dependency_error` | `sudo apt install ffmpeg`, затем перезапусти сервис. |
| `/health` → `"js_runtime": "missing"`, в логе предупреждение про Deno | Поставь Deno в `/usr/local/bin`, как в [шаге 1](#1-системные-пакеты). Deno из `~/.deno/bin` работает в шелле, но не в systemd-сервисе. |
| `youtube_auth_required`: «Sign in to confirm you're not a bot» | YouTube не доверяет этому IP. Войди в YouTube в браузере на этой же машине, задай `COOKIES_FROM_BROWSER=firefox` (или `chrome`, или `firefox:/путь/к/профилю`) в `adder/.env`, перезапусти, отправь ссылку заново. |
| Много ошибок `rate_limited` | YouTube ограничивает запросы; сервис уже ставит все скачивания на паузу на `RATE_LIMIT_BACKOFF` секунд за попытку. Если не проходит — уменьши `MAX_WORKERS` до `1`, подожди час, отправь упавшие ссылки заново. |
| Скачивания, которые работали, начали падать | YouTube что-то поменял; обнови yt-dlp: `.venv/bin/pip install -U yt-dlp yt-dlp-ejs` и перезапусти. |
| Задача `done`, но в Navidrome трека нет | Navidrome читает другую папку: его `MusicFolder` (`/etc/navidrome/navidrome.toml`) должен совпадать с `library_path` из `/health`. Проверь, что пользователь Navidrome может её читать: `sudo -u navidrome ls "<library_path>"`. Затем *Settings → Scan* в Navidrome. |
| Удаление трека из веб-интерфейса не работает | Удалённые файлы попадают в `trash/` в корне репозитория. После `git pull` запусти `./deploy/install.sh` заново, чтобы у юнита был `ReadWritePaths` для неё. |
| Веб-интерфейс говорит, что токен неверный | Вставь значение `API_TOKEN` из `adder/.env` без `API_TOKEN=`. |

---

## 🔒 Безопасность

* API закрыт Bearer-токеном, сравнение — через `secrets.compare_digest` (защита от timing-атак); без токена сервис не запускается.
* Заголовки/данные из YouTube (заголовок видео, автор) — недоверенные данные: во фронтенде они рендерятся только через `textContent`/`replaceChildren`, никогда через `innerHTML`.
* Принимаются только YouTube-URL с точным совпадением хоста (защита от обхода вида `youtube.com.evil.example`).
* systemd-юнит: `ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges`, `PrivateTmp`, запись разрешена только в `adder/`, `trash/` и путь библиотеки. Ограничивает процесс именно `ProtectHome`: `ProtectSystem=strict` не покрывает `/home`, и без него сервис мог бы писать в собственные исходники.
* Токен и `.env` никогда не коммитятся (`.gitignore`); в README и issue не публикуйте реальный `API_TOKEN`.

Нашли уязвимость — заведите приватный security advisory в репозитории, а не публичный issue.

---

## 📁 Структура проекта

```text
local-Spotify/
├── adder/                  # Сервис приёма и обработки треков
│   ├── app.py               # FastAPI-приложение, воркеры, вся бизнес-логика
│   ├── config.py             # Загрузка и валидация конфигурации из .env
│   ├── server.py              # Точка входа (uvicorn)
│   ├── fix_covers.py           # Офлайн-добивка отсутствующих обложек
│   └── requirements.txt
├── web/                     # Статический веб-интерфейс (vanilla JS)
├── scripts/                 # Офлайн-инструменты: аудит библиотеки, поиск дублей, миграция плейлиста
├── tests/                   # pytest
├── deploy/                   # systemd unit, install/backup-скрипты, конфиг Navidrome
└── .env.example
```

---

## ⚠️ Ограничения

Это домашний self-hosted проект, не рассчитанный на:

* публичный SaaS или высоконагруженный production;
* массовое/коммерческое использование;
* обход региональных или иных ограничений YouTube.

Отдельно про телефон. Amperfy 2.1 **не показывает свою обложку подборки**: его разборщик ответа не читает поле `coverArt` вообще и рисует коллаж из обложек первых треков. Проверено по исходникам (`SsPlaylistParserDelegate`, `Playlist.updateArtworkItems`). Обложка, поставленная здесь, видна в веб-интерфейсе Navidrome и в этом плеере — в Amperfy её не будет. Порядок треков он, наоборот, перечитывает при каждом открытии подборки.

Перед загрузкой стороннего контента убедитесь, что у вас есть на это право.

---

## 🗺️ Roadmap

* [ ] Удаление и переорганизация треков через API
* [ ] Импорт целых альбомов и плейлистов, а не только отдельных ссылок
* [ ] Docker-образ для развёртывания без systemd
* [ ] Метрики (Prometheus) поверх текущего `/health`

---

## 📜 Лицензия

[MIT](LICENSE). Убедитесь, что у вас есть право на загрузку и хранение стороннего контента, который вы добавляете в библиотеку.

---

<p align="center">
  <a href="https://github.com/Whyslab">Whyslab</a> ·
  <a href="https://github.com/Whyslab/local-Spotify">local-Spotify</a>
</p>
