# 🎵 localSpotify

> **Self-hosted музыкальная система для локального хранения, обработки и воспроизведения музыки.**

`localSpotify` — домашняя музыкальная система, которая позволяет хранить собственную музыкальную библиотеку на сервере, добавлять треки через YouTube, автоматически загружать и обрабатывать их, нормализовать метаданные и воспроизводить музыку через **Navidrome** с iPhone или других устройств.

Проект рассчитан прежде всего на **личное self-hosted использование в домашней сети**.

---

## ✨ Возможности

* 🎵 Локальная музыкальная библиотека
* ▶️ Добавление музыки через YouTube URL
* ⚡ Фоновая очередь обработки
* 👷 Несколько worker-потоков для обработки задач
* 📥 Загрузка через `yt-dlp`
* 🎚️ Конвертация и нормализация через FFmpeg
* 🏷️ Автоматическая обработка метаданных
* 🖼️ Работа с обложками
* 🧹 Очистка названий треков и файлов
* 🔄 Автоматические retry при временных ошибках
* 💾 SQLite для хранения состояния задач
* 🔐 Защита API через `API_TOKEN`
* ❤️ Health endpoint
* 🛑 Graceful shutdown
* ♻️ Восстановление незавершённых задач после перезапуска
* 🧹 Очистка старых временных файлов
* 🎧 Navidrome как музыкальный сервер
* 📱 Поддержка Subsonic API-клиентов
* 🚀 Запуск через systemd
* 🐧 Оптимизировано под Arch Linux
* 🔒 Предназначено для использования в локальной сети

---

# 🏗️ Архитектура

Система состоит из нескольких независимых компонентов.

```text
                         ┌──────────────────────┐
                         │        iPhone        │
                         │                      │
                         │       Amperfy        │
                         │          │           │
                         └──────────┼───────────┘
                                    │
                              Subsonic API
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │      Navidrome       │
                         │                      │
                         │   Music Server       │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │   Music Library      │
                         │                      │
                         │ Artist / Singles     │
                         │ Albums / Tracks      │
                         └──────────────────────┘


                         Music ingestion
                               │
                               ▼
                    ┌──────────────────────┐
                    │      adder API       │
                    │      FastAPI         │
                    └──────────┬───────────┘
                               │
                         Task Queue
                               │
                               ▼
                    ┌──────────────────────┐
                    │       Workers        │
                    │                      │
                    │       yt-dlp         │
                    │        FFmpeg        │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │  Normalized Library  │
                    └──────────────────────┘
```

---

# 📁 Структура проекта

Основная структура репозитория:

```text
local-Spotify/
├── adder/
│   ├── app.py
│   ├── server.py
│   ├── .env
│   └── tmp/
│
├── tests/
│   ├── test_app.py
│   ├── test_config.py
│   └── test_title_cleaning.py
│
├── scripts/
│   └── ...
│
├── systemd/
│   └── ...
│
├── requirements.txt
├── README.md
└── ...
```

---

# 📂 Основные файлы

## `adder/app.py`

Главный файл приложения.

В нём находятся:

* FastAPI application;
* API endpoints;
* авторизация;
* SQLite database;
* task queue;
* worker logic;
* YouTube URL validation;
* YouTube URL canonicalization;
* загрузка через `yt-dlp`;
* обработка через FFmpeg;
* metadata processing;
* retry logic;
* cleanup;
* graceful shutdown;
* recovery незавершённых задач.

Это **основная логика сервиса**.

---

## `adder/server.py`

Production launcher приложения.

Файл специально оставлен максимально простым:

```text
adder/server.py
        │
        ▼
uvicorn
        │
        ▼
adder.app:app
        │
        ▼
FastAPI lifespan
        │
        ▼
worker startup
```

Worker'ы запускаются через lifecycle FastAPI, а не отдельно в launcher.

Это предотвращает ситуацию, когда worker'ы запускаются дважды.

Запуск:

```bash
python -m adder.server
```

---

## `adder/.env`

Локальная конфигурация приложения.

Пример:

```text
API_TOKEN=change-this-token
HOST=127.0.0.1
PORT=8787
```

Файл содержит секреты и **не должен попадать в Git**.

Добавь его в `.gitignore`:

```text
adder/.env
```

---

## `adder/tmp/`

Временная директория.

Используется во время:

* загрузки;
* конвертации;
* обработки;
* временного хранения файлов.

После завершения обработки временные файлы удаляются.

При запуске приложения также выполняется очистка устаревших временных файлов.

---

# 🧪 Тесты

Проект содержит автоматические тесты.

Запуск:

```bash
source .venv/bin/activate
PYTHONPATH="$PWD" pytest -q
```

Текущий проверенный результат:

```text
47 passed
```

Тесты покрывают:

* `/health`;
* API authentication;
* API `/api/add`;
* URL validation;
* YouTube URL canonicalization;
* duplicate detection;
* SQLite;
* task queue;
* worker execution;
* worker shutdown;
* task recovery;
* retry behavior;
* temporary file cleanup;
* title cleaning;
* metadata behavior.

---

# 🐍 Требования

Минимально необходимы:

| Компонент    | Назначение         |
| ------------ | ------------------ |
| Python 3.10+ | Runtime            |
| FastAPI      | Web API            |
| Uvicorn      | ASGI server        |
| SQLite       | Database           |
| yt-dlp       | YouTube downloader |
| FFmpeg       | Audio processing   |
| systemd      | Автозапуск         |
| Navidrome    | Music server       |

Для разработки дополнительно используется:

```text
pytest
```

---

# 🐧 Установка на Arch Linux

## 1. Установка системных пакетов

```bash
sudo pacman -Syu
sudo pacman -S git python python-pip ffmpeg
```

Проверь версии:

```bash
python --version
ffmpeg -version
```

---

# 📥 Клонирование проекта

```bash
cd ~
git clone https://github.com/Whyslab/local-Spotify.git
cd local-Spotify
```

Проверить текущую ветку:

```bash
git branch --show-current
```

Для production рекомендуется использовать:

```text
main
```

---

# 🐍 Создание виртуального окружения

```bash
python -m venv .venv
```

Активировать:

```bash
source .venv/bin/activate
```

Обновить pip:

```bash
python -m pip install --upgrade pip
```

Установить зависимости:

```bash
pip install -r requirements.txt
```

Проверить:

```bash
pip list
```

---

# 🔐 Настройка API_TOKEN

API защищён Bearer Token.

Создай:

```text
adder/.env
```

Пример:

```text
API_TOKEN=your-long-random-secret-token
```

Лучше использовать длинный случайный токен.

Например:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Полученное значение вставь в:

```text
adder/.env
```

Например:

```text
API_TOKEN=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

**Не публикуй настоящий токен в GitHub, README, Issues или скриншотах.**

---

# ▶️ Запуск приложения вручную

Активируй окружение:

```bash
cd ~/local-Spotify
source .venv/bin/activate
```

Запусти:

```bash
python -m adder.server
```

После запуска API будет доступен по адресу:

```text
http://127.0.0.1:8787
```

---

# ❤️ Проверка `/health`

Endpoint health не требует авторизации.

```bash
curl http://127.0.0.1:8787/health
```

Ожидаемый результат:

```json
{
  "status": "ok"
}
```

---

# 🔐 Проверка авторизации

Получить token:

```bash
TOKEN="$(grep '^API_TOKEN=' adder/.env | cut -d= -f2-)"
```

Запрос без token:

```bash
curl http://127.0.0.1:8787/api/tasks
```

Должен быть отклонён.

Запрос с token:

```bash
curl \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8787/api/tasks
```

---

# ▶️ Добавление YouTube трека

Пример:

```bash
TOKEN="$(grep '^API_TOKEN=' adder/.env | cut -d= -f2-)"

curl -sS \
  -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "links": [
      "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    ]
  }' \
  http://127.0.0.1:8787/api/add
```

Пример ответа:

```json
{
  "added": [
    123
  ]
}
```

ID означает номер задачи в SQLite.

---

# 📚 Добавление нескольких треков

Можно передать несколько ссылок:

```bash
curl -sS \
  -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "links": [
      "https://www.youtube.com/watch?v=VIDEO_ID_1",
      "https://www.youtube.com/watch?v=VIDEO_ID_2",
      "https://www.youtube.com/watch?v=VIDEO_ID_3"
    ]
  }' \
  http://127.0.0.1:8787/api/add
```

Каждая ссылка становится отдельной задачей.

---

# 📊 Проверка очереди

Получить все задачи:

```bash
curl \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8787/api/tasks
```

Задача может находиться в состояниях:

```text
queued
downloading
tagging
done
error
```

---

# 🔄 Как работает обработка

После добавления URL:

```text
YouTube URL
     │
     ▼
Validation
     │
     ▼
Canonicalization
     │
     ▼
Duplicate check
     │
     ▼
SQLite
     │
     ▼
Task Queue
     │
     ▼
Worker
     │
     ▼
yt-dlp
     │
     ▼
Downloaded audio
     │
     ▼
FFmpeg
     │
     ▼
Metadata
     │
     ▼
Normalized Library
     │
     ▼
Navidrome
     │
     ▼
iPhone / Amperfy
```

---

# 👷 Worker System

Приложение использует фоновые worker-потоки.

Worker:

1. получает задачу из очереди;
2. запускает обработку;
3. выполняет download;
4. выполняет conversion;
5. записывает metadata;
6. перемещает готовый файл;
7. отмечает задачу как `done`;
8. при ошибке запускает retry;
9. освобождает task lock.

Количество worker'ов определяется настройкой:

```text
MAX_WORKERS
```

Worker'ы запускаются во время FastAPI lifespan.

Это важно: приложение не должно запускать одну группу worker'ов из `server.py`, а вторую из `app.py`.

---

# ♻️ Recovery после перезапуска

Если сервер был остановлен во время обработки:

```text
downloading
tagging
```

такие задачи могут быть восстановлены после следующего запуска.

Механизм recovery возвращает незавершённые задачи в очередь.

Это позволяет переживать:

* reboot;
* restart systemd;
* crash процесса;
* остановку сервера.

---

# 🔁 Retry

Временные ошибки обработки могут приводить к повторной попытке.

Состояние retry хранится в задаче:

```text
retry_count
```

Если ошибка является окончательной, задача получает:

```text
status = error
```

и дополнительную информацию:

```text
error
error_type
```

---

# 🧹 Temporary Files

Временные файлы хранятся в:

```text
adder/tmp/
```

Сервис автоматически удаляет устаревшие временные файлы.

После graceful shutdown выполняется дополнительная очистка.

---

# 🎵 Music Library

Готовая библиотека хранится отдельно от исходников проекта.

Типичная структура:

```text
~/Music/
└── Normalized Library/
    ├── Artist A/
    │   └── Singles/
    │       └── Track.m4a
    │
    ├── Artist B/
    │   └── Singles/
    │       └── Track.m4a
    │
    └── Artist C/
        └── Albums/
            └── Album/
                └── Track.m4a
```

Navidrome должен быть настроен на каталог:

```text
~/Music/Normalized Library
```

или соответствующий абсолютный путь на сервере.

---

# 🎧 Navidrome

Navidrome отвечает за:

* индексацию музыки;
* музыкальную библиотеку;
* поиск;
* playlists;
* воспроизведение;
* Subsonic API.

`localSpotify` не является музыкальным streaming-сервером сам по себе.

Архитектура разделена:

```text
localSpotify adder
        │
        ▼
Music Library
        │
        ▼
Navidrome
        │
        ▼
Subsonic API
        │
        ▼
Amperfy
```

Это позволяет заменить Navidrome другим совместимым сервером в будущем.

---

# 📱 iPhone

Для iPhone можно использовать Subsonic-совместимый клиент.

Например:

```text
Amperfy
```

Подключение выполняется к Navidrome.

В приложении указываются:

```text
Server URL
Username
Password
```

Конкретный URL зависит от конфигурации домашней сети.

---

# 🖥️ systemd

Для production использования рекомендуется запускать приложение через systemd.

Пример service-файла:

```ini
[Unit]
Description=localSpotify Music Adder
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/local-Spotify
Environment="PATH=/home/YOUR_USER/local-Spotify/.venv/bin"
ExecStart=/home/YOUR_USER/local-Spotify/.venv/bin/python -m adder.server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Замени:

```text
YOUR_USER
```

на имя пользователя Linux.

---

# ⚙️ Установка systemd service

Создай:

```bash
sudo nano /etc/systemd/system/localspotify.service
```

Вставь конфигурацию выше.

После этого:

```bash
sudo systemctl daemon-reload
```

Запусти:

```bash
sudo systemctl enable --now localspotify
```

Проверь:

```bash
systemctl status localspotify
```

---

# 📋 Логи systemd

Последние логи:

```bash
journalctl -u localspotify -n 100
```

Следить в реальном времени:

```bash
journalctl -u localspotify -f
```

Перезапуск:

```bash
sudo systemctl restart localspotify
```

Остановка:

```bash
sudo systemctl stop localspotify
```

---

# 🔄 Обновление проекта

Перед обновлением желательно проверить состояние Git:

```bash
cd ~/local-Spotify
git status
```

Если рабочая директория чистая:

```bash
git switch main
git pull --ff-only origin main
```

После обновления:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

Проверить тесты:

```bash
PYTHONPATH="$PWD" pytest -q
```

Если всё успешно:

```bash
sudo systemctl restart localspotify
```

Проверить:

```bash
systemctl status localspotify
```

---

# 🧪 Production Smoke Test

После запуска рекомендуется проверить основные компоненты.

## 1. Health

```bash
curl -sS http://127.0.0.1:8787/health
```

## 2. Authentication

```bash
TOKEN="$(grep '^API_TOKEN=' adder/.env | cut -d= -f2-)"
```

## 3. Tasks API

```bash
curl -sS \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8787/api/tasks
```

## 4. Add test track

```bash
curl -sS \
  -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"links":["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]}' \
  http://127.0.0.1:8787/api/add
```

## 5. Check processing

```bash
curl -sS \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8787/api/tasks
```

## 6. Check output library

```bash
find "$HOME/Music/Normalized Library" \
  -type f \
  -name '*.m4a' \
  -printf '%T@ %p\n' |
sort -nr |
head
```

---

# 🔍 Проверка FFmpeg

```bash
ffmpeg -version
```

Проверка конкретного файла:

```bash
ffprobe \
  -v error \
  -show_entries format=duration,size \
  -of default=noprint_wrappers=1 \
  "PATH_TO_FILE.m4a"
```

---

# 🔍 Проверка yt-dlp

```bash
yt-dlp --version
```

Проверка:

```bash
yt-dlp \
  --simulate \
  "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

---

# 🗄️ SQLite

Основная база данных используется для хранения задач.

Типичная база:

```text
adder.db
```

В ней хранится состояние очереди.

Пример информации о задаче:

```text
id
url
status
artist
title
error
error_type
retry_count
updated_at
```

База данных является частью runtime-состояния и **не должна коммититься в Git**, если это локальная production-база.

---

# 🔒 Безопасность

Проект рассчитан на использование в домашней сети.

API защищён:

```text
Authorization: Bearer API_TOKEN
```

Не публикуй API напрямую в Интернет без дополнительной защиты.

Особенно важно:

* не коммитить `.env`;
* не публиковать API token;
* не публиковать production database;
* не открывать порт `8787` наружу без необходимости;
* использовать firewall;
* использовать reverse proxy/VPN при удалённом доступе.

---

# 🌐 Доступ из Интернета

Проект не предполагает прямого публичного доступа к API.

Для удалённого доступа рекомендуется использовать VPN, например:

```text
WireGuard
```

или другой защищённый VPN.

Не рекомендуется просто пробрасывать:

```text
8787
```

на Internet.

---

# 💾 Backup

Рекомендуется регулярно резервировать:

```text
~/Music/Normalized Library/
```

а также:

```text
adder.db
```

Конфигурацию:

```text
adder/.env
```

следует сохранять отдельно и безопасно.

Пример:

```bash
tar \
  -czf localspotify-backup.tar.gz \
  "$HOME/Music/Normalized Library" \
  adder.db
```

Не добавляй `.env` в публичный backup без шифрования.

---

# 🚨 Troubleshooting

## API не запускается

Проверь:

```bash
systemctl status localspotify
```

и:

```bash
journalctl -u localspotify -n 100
```

При ручном запуске:

```bash
source .venv/bin/activate
python -m adder.server
```

---

## `/health` не отвечает

Проверь процесс:

```bash
pgrep -af uvicorn
```

Проверь порт:

```bash
ss -ltnp | grep 8787
```

---

## Задачи зависли

Проверь:

```bash
curl \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8787/api/tasks
```

Затем:

```bash
journalctl -u localspotify -n 200
```

---

## yt-dlp не загружает видео

Проверь:

```bash
yt-dlp --version
```

и попробуй:

```bash
yt-dlp --simulate "YOUTUBE_URL"
```

Некоторые YouTube видео могут быть:

* удалены;
* приватными;
* недоступными в регионе;
* ограниченными автором;
* недоступными без авторизации.

Такие ошибки не обязательно означают проблему самого приложения.

---

## FFmpeg не найден

Проверь:

```bash
which ffmpeg
```

Если отсутствует:

```bash
sudo pacman -S ffmpeg
```

---

## Файл появился, но Navidrome его не видит

Проверь путь музыкальной библиотеки в Navidrome.

Затем выполни rescan библиотеки.

Также проверь права:

```bash
ls -lah "$HOME/Music/Normalized Library"
```

Пользователь, под которым работает Navidrome, должен иметь доступ к музыкальной библиотеке.

---

# 🧹 Очистка development artifacts

Перед commit рекомендуется:

```bash
git status
```

Не должны попадать в Git:

```text
.venv/
__pycache__/
*.pyc
adder.db
adder/.env
adder/tmp/
```

Проверь:

```bash
git status --short
```

---

# 🌿 Git Workflow

Основная production ветка:

```text
main
```

Для изменений рекомендуется создавать отдельную ветку:

```bash
git switch main
git pull --ff-only origin main
git switch -c fix/my-change
```

После изменений:

```bash
git diff --check
```

Запусти тесты:

```bash
source .venv/bin/activate
PYTHONPATH="$PWD" pytest -q
```

Commit:

```bash
git add .
git commit -m "fix: description"
```

Push:

```bash
git push -u origin fix/my-change
```

После проверки изменения можно объединить с `main`.

---

# 🧪 CI / Quality Gate

Перед слиянием изменений рекомендуется пройти:

```text
Git diff
     │
     ▼
git diff --check
     │
     ▼
pytest
     │
     ▼
manual smoke test
     │
     ▼
production verification
```

Минимальная команда:

```bash
PYTHONPATH="$PWD" pytest -q
```

---

# 📊 Текущий статус

**Project:** localSpotify

**Type:** Self-hosted personal music system

**Platform:** Linux / Arch Linux

**API:** FastAPI

**Database:** SQLite

**Downloader:** yt-dlp

**Audio processing:** FFmpeg

**Music server:** Navidrome

**Mobile client:** Subsonic-compatible clients / Amperfy

**Process manager:** systemd

**Status:** 🟢 Production-ready for personal self-hosted use

Проект прошёл функциональную проверку основных компонентов:

* FastAPI startup;
* `/health`;
* API authentication;
* YouTube URL validation;
* YouTube URL canonicalization;
* task creation;
* SQLite persistence;
* background workers;
* queue processing;
* `yt-dlp`;
* FFmpeg;
* metadata processing;
* retry handling;
* task recovery;
* temporary file cleanup;
* graceful shutdown;
* duplicate URL handling;
* title cleaning;
* output library creation.

Автоматический тестовый набор:

```text
47 passed
```

---

# ⚠️ Ограничения

Проект является домашней self-hosted системой.

Он не предназначен для:

* публичного SaaS;
* массового использования;
* высоконагруженного production;
* публичного музыкального streaming-сервиса;
* обхода ограничений сторонних сервисов.

Доступность конкретных YouTube видео зависит от самого YouTube и параметров конкретного контента.

---

# 🗺️ Roadmap

Возможные дальнейшие улучшения:

* [ ] Web UI для управления очередью
* [ ] Просмотр прогресса загрузки
* [ ] Удаление треков через API
* [ ] Управление библиотекой
* [ ] Album import
* [ ] Playlist management
* [ ] Улучшенный поиск metadata
* [ ] Автоматический поиск лучшего audio source
* [ ] Более подробная система retry
* [ ] Structured logging
* [ ] Metrics
* [ ] Prometheus integration
* [ ] Docker deployment
* [ ] Backup automation
* [ ] CI/CD
* [ ] Автоматический production smoke test

---

# 📜 Лицензия

Проект предназначен для личного self-hosted использования.

Перед использованием стороннего контента убедитесь, что вы имеете соответствующие права или разрешение на его загрузку и хранение.

---

# 👤 Автор

**Whyslab**

GitHub:

https://github.com/Whyslab

Repository:

https://github.com/Whyslab/local-Spotify

---

# ❤️ localSpotify

```text
YouTube
   │
   ▼
localSpotify Adder
   │
   ├── yt-dlp
   ├── FFmpeg
   ├── Metadata
   └── Workers
          │
          ▼
   Normalized Library
          │
          ▼
      Navidrome
          │
          ▼
      Subsonic API
          │
          ▼
       iPhone
          │
          ▼
       Amperfy
```

**Own your music. Own your server. Own your library.**
