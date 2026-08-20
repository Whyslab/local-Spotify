# 🎵 localSpotify

> **Personal self-hosted music server for Arch Linux with an iOS client.**

Полноценная домашняя замена Spotify: музыка хранится локально на собственном сервере, добавляется через веб-интерфейс из YouTube, автоматически обрабатывается и появляется в **Navidrome**, после чего доступна с iPhone через **Amperfy**.

Проект рассчитан на **личное использование в домашней сети**.

---

## ✨ Features

* 🎧 Полностью локальная музыкальная библиотека
* 📱 iOS-клиент с офлайн-кэшем
* 🖥️ Сервер на Arch Linux
* 🎵 Navidrome + Subsonic API
* ▶️ Добавление треков через YouTube
* 🌐 Веб-интерфейс `adder`
* ⚡ Автоматическое обнаружение новых файлов
* 🏷️ Автоматическое тегирование M4A
* 🖼️ Автоматический поиск и встраивание обложек
* 📥 Импорт больших Spotify-плейлистов
* 🔄 Автоматическая нормализация библиотеки
* 🚀 Автозапуск через systemd
* 🔒 Доступ только из локальной сети
* 💾 Простое резервное копирование
* 🛠️ Автоматическое развёртывание на новой машине

---

# 🏗️ Architecture

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
┌─────────────────────────────────────────────────────────────┐
│                         ThinkPad                            │
│                       Arch Linux                            │
│                                                             │
│   ┌─────────────────┐                                      │
│   │    Navidrome    │◄──────────────┐                       │
│   │   Subsonic API  │               │                       │
│   │      :4533      │               │ watcher / inotify     │
│   └────────┬────────┘               │                       │
│            │                        │                       │
│            ▼                        │                       │
│   ┌─────────────────────────────┐  │                       │
│   │   Normalized Library        │──┘                       │
│   │                             │                          │
│   │ Artist / Singles / Track    │                          │
│   └─────────────────────────────┘                          │
│                                                             │
│   ┌─────────────────┐                                      │
│   │      adder      │                                      │
│   │   Web UI :8787  │                                      │
│   └────────┬────────┘                                      │
│            │                                                │
│            ▼                                                │
│         YouTube                                             │
│            │                                                │
│            ▼                                                │
│       yt-dlp → M4A                                         │
└─────────────────────────────────────────────────────────────┘
```

### Components

| Component      | Purpose                             |
| -------------- | ----------------------------------- |
| **Navidrome**  | Музыкальный сервер и Subsonic API   |
| **adder**      | Веб-интерфейс для добавления треков |
| **yt-dlp**     | Загрузка аудио с YouTube            |
| **FFmpeg**     | Обработка и конвертация аудио       |
| **watcher**    | Обнаружение новых файлов            |
| **Amperfy**    | iOS-клиент с офлайн-кэшем           |
| **iTunes API** | Основной источник обложек           |
| **Deezer API** | Fallback для обложек                |
| **systemd**    | Автозапуск сервисов                 |
| **UFW**        | Ограничение доступа к LAN           |

---

# 📋 Prerequisites

Перед установкой убедись, что на машине установлены необходимые компоненты.

| Requirement         | Arch Linux                         |
| ------------------- | ---------------------------------- |
| Python 3.10+        | `sudo pacman -S python python-pip` |
| FFmpeg              | `sudo pacman -S ffmpeg`            |
| Git                 | `sudo pacman -S git`               |
| AUR helper          | `yay` или `paru`                   |
| UFW *(опционально)* | `sudo pacman -S ufw`               |

Для других Linux-дистрибутивов принцип установки тот же, но менеджер пакетов будет отличаться (`apt`, `dnf`, `pacman` и т.д.).

---

# 🚀 Deployment

## 1. Clone repository

```bash
git clone https://github.com/Whyslab/local-Spotify.git
cd local-Spotify
```

---

## 2. Install Navidrome

```bash
yay -S navidrome
```

---

## 3. Create Python environment

```bash
cd adder

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

deactivate
cd ..
```

---

## 4. Run automatic installer

Сделай installer executable:

```bash
chmod +x deploy/install.sh
```

Запусти:

```bash
./deploy/install.sh
```

### `install.sh` автоматически:

* устанавливает `music-adder.service`;
* устанавливает конфигурацию Navidrome;
* заменяет placeholder пользователя на текущего пользователя;
* применяет systemd hardening;
* настраивает доступ Navidrome к музыкальной библиотеке;
* открывает порты `4533` и `8787` в UFW;
* ограничивает доступ к сервисам локальной подсетью.

После установки:

```text
Navidrome:
http://localhost:4533

Adder:
http://localhost:8787
```

Сначала зайди в Navidrome и создай администратора.

После этого открой `adder` и попробуй добавить тестовый YouTube-трек.

---

# ⚙️ Configuration

Все основные настройки можно менять через `.env` или systemd environment variables.

## Environment variables

| Variable               | Used by                            |                      Default | Description                      |
| ---------------------- | ---------------------------------- | ---------------------------: | -------------------------------- |
| `LIBRARY_PATH`         | `adder/app.py`, playlist migration | `~/Music/Normalized Library` | Путь к музыкальной библиотеке    |
| `PORT`                 | `adder/app.py`                     |                       `8787` | Порт веб-интерфейса              |
| `MAX_WORKERS`          | `adder/app.py`                     |                          `2` | Количество параллельных загрузок |
| `DELAY_BETWEEN_TRACKS` | playlist migration                 |                        `1.1` | Пауза между запросами            |

### `LIBRARY_PATH`

Путь, который одновременно используется:

* `adder`;
* скриптами миграции;
* Navidrome.

Если библиотека находится в другом месте, необходимо изменить соответствующие настройки.

---

# 🔧 Environment setup

## Option A — `.env`

Создай `.env` внутри `adder`:

```bash
cp .env.example adder/.env
nano adder/.env
```

Пример:

```env
LIBRARY_PATH=/home/your-user/Music/Normalized Library
PORT=8787
MAX_WORKERS=3
DELAY_BETWEEN_TRACKS=1.1
```

---

## Option B — systemd override

Если используется `deploy/music-adder.service`, можно задать переменные через systemd:

```bash
systemctl --user edit music-adder
```

Добавь:

```ini
[Service]
Environment="LIBRARY_PATH=/home/your-user/Music/Normalized Library"
Environment="PORT=8787"
Environment="MAX_WORKERS=3"
Environment="DELAY_BETWEEN_TRACKS=1.1"
```

После изменения:

```bash
systemctl --user restart music-adder
```

---

# 📝 Where to enter your own values

При развёртывании на новой машине необходимо проверить несколько значений.

## 1. Music library

В:

```text
/etc/navidrome/navidrome.toml
```

должно быть:

```toml
MusicFolder = "/home/YOUR_USER/Music/Normalized Library"
```

Замените `YOUR_USER` на своего пользователя Linux.

---

## 2. Local network

Узнать свою локальную подсеть:

```bash
hostname -I
```

или:

```bash
ip route
```

Например:

```text
192.168.1.0/24
```

UFW должен разрешать доступ к:

```text
4533 → Navidrome
8787 → adder
```

---

## 3. Navidrome credentials

При первом открытии:

```text
http://localhost:4533
```

создайте администратора.

Эти данные понадобятся для подключения iPhone-клиента.

---

## 4. Server IP

Узнать локальный IP сервера:

```bash
ip route get 1.1.1.1 | awk '{print $7; exit}'
```

Например:

```text
192.168.1.42
```

Тогда iPhone будет подключаться к:

```text
http://192.168.1.42:4533
```

---

# 📱 iOS Client

Для iPhone используется **Amperfy**.

После установки добавь новый сервер:

```text
http://IP_OF_SERVER:4533
```

Например:

```text
http://192.168.1.42:4533
```

Используй логин и пароль администратора Navidrome.

### Требования

Телефон и сервер должны находиться в одной локальной сети:

```text
iPhone
   │
   │ Wi-Fi
   ▼
Router
   │
   │ LAN
   ▼
ThinkPad
```

---

# 🎵 Adding music

Добавить новый трек можно напрямую с телефона.

## Workflow

```text
YouTube URL
     │
     ▼
   adder
     │
     ▼
   queued
     │
     ▼
 downloading
     │
     ▼
  tagging
     │
     ▼
    done
     │
     ▼
Normalized Library
     │
     ▼
   Navidrome
     │
     ▼
   Amperfy
```

### Шаги

1. Найди трек на YouTube.
2. Открой `http://IP_OF_SERVER:8787`.
3. Вставь ссылку.
4. Нажми **Add**.
5. Дождись статуса `done`.
6. Navidrome автоматически обнаружит файл.
7. Трек появится в Amperfy.

---

# 🔄 Spotify Playlist Migration

Проект поддерживает перенос больших плейлистов Spotify в локальную библиотеку.

## 1. Export Spotify playlist

Используй Spotify Playlist Exporter:

```text
https://www.chosic.com/spotify-playlist-exporter/svg
```

Экспортируй плейлист в `.txt`.

---

## 2. Find YouTube URLs

Перейди в `scripts`:

```bash
cd scripts
```

Запусти:

```bash
python youtube_links.py
```

Скрипт:

* читает список Spotify-треков;
* создаёт запрос `Artist - Title`;
* выполняет поиск через `yt-dlp`;
* выбирает первый результат;
* сохраняет найденный YouTube URL.

Результат:

```text
spotify_tracks_youtube.csv
```

---

## 3. Download and normalize

Запусти:

```bash
python normalize_library.py
```

Скрипт автоматически:

1. скачивает `.m4a` через `yt-dlp`;
2. извлекает исполнителя;
3. извлекает название;
4. ищет обложку;
5. встраивает metadata;
6. встраивает artwork;
7. перемещает файл в нормализованную библиотеку.

---

# 🗂️ Library structure

Итоговая структура:

```text
~/Music/Normalized Library/
├── Artist A/
│   └── Singles/
│       ├── Track 1.m4a
│       └── Track 2.m4a
│
├── Artist B/
│   └── Singles/
│       └── Track 3.m4a
│
└── Artist C/
    └── Singles/
        └── Track 4.m4a
```

---

# 🖼️ Artwork

Основной источник обложек:

```text
iTunes API
```

Fallback:

```text
YouTube thumbnail
```

Если часть обложек не загрузилась из-за rate-limit:

```bash
python fix_covers.py
```

Скрипт:

* находит `.m4a` без `covr`;
* ищет обложку через Deezer API;
* повторяет неудачные запросы;
* делает паузы;
* встраивает artwork непосредственно в файл.

Операция идемпотентна: уже обработанные файлы повторно менять не требуется.

---

# ⚙️ Systemd

## music-adder

Основной сервис:

```text
music-adder.service
```

Проверить состояние:

```bash
systemctl --user status music-adder
```

Запустить:

```bash
systemctl --user start music-adder
```

Остановить:

```bash
systemctl --user stop music-adder
```

Перезапустить:

```bash
systemctl --user restart music-adder
```

Логи:

```bash
journalctl --user -u music-adder -f
```

Для запуска user service после выхода пользователя:

```bash
loginctl enable-linger $USER
```

---

# 🔥 UFW

Если используется UFW, рекомендуется разрешить доступ только из локальной сети.

Пример для `192.168.1.0/24`:

```bash
sudo ufw allow from 192.168.1.0/24 \
    to any port 4533 proto tcp \
    comment "Navidrome"

sudo ufw allow from 192.168.1.0/24 \
    to any port 8787 proto tcp \
    comment "Adder"

sudo ufw reload
```

Проверить:

```bash
sudo ufw status
```

> `192.168.1.0/24` необходимо заменить на свою локальную подсеть.

---

# 📁 Project structure

```text
local-Spotify/
├── README.md
├── .gitignore
├── .env.example
│
├── adder/
│   ├── app.py
│   ├── fix_covers.py
│   ├── requirements.txt
│   ├── .env
│   └── tmp/
│
├── scripts/
│   ├── youtube_links.py
│   ├── normalize_library.py
│   └── migrate_playlist.py
│
└── deploy/
    ├── install.sh
    └── music-adder.service
```

### Main files

| File                           | Purpose                                       |
| ------------------------------ | --------------------------------------------- |
| `adder/app.py`                 | FastAPI application and workers               |
| `adder/fix_covers.py`          | Artwork recovery                              |
| `scripts/youtube_links.py`     | Spotify → YouTube URL matching                |
| `scripts/normalize_library.py` | Downloading, tagging and library organization |
| `scripts/migrate_playlist.py`  | Playlist migration workflow                   |
| `deploy/install.sh`            | Automated deployment                          |
| `deploy/music-adder.service`   | systemd service definition                    |
| `adder/tmp/`                   | Temporary download files                      |

---

# 🛠️ Troubleshooting

## `adder` returns `000` / does not respond

Check service status:

```bash
systemctl --user status music-adder --no-pager
```

Check recent logs:

```bash
journalctl --user -u music-adder -n 30 --no-pager
```

### `Start-limit-hit`

The service has crashed too many times.

Reset the failure state:

```bash
systemctl --user reset-failed music-adder
```

Then:

```bash
systemctl --user restart music-adder
```

### `No such file or directory` in `ExecStart`

Check that the paths inside the systemd service match the actual repository location:

```bash
pwd
```

and:

```bash
systemctl --user cat music-adder
```

---

# ❌ Navidrome: `permission denied`

There are two common causes.

## 1. `ProtectHome`

Check the service:

```bash
systemctl cat navidrome
```

If `ProtectHome` prevents access to the music directory, create an override:

```bash
sudo systemctl edit navidrome
```

Example:

```ini
[Service]
ProtectHome=read-only
PrivateUsers=no
```

Then restart:

```bash
sudo systemctl restart navidrome
```

---

## 2. Directory traversal permissions

The Navidrome user needs permission to traverse the parent directories.

For example:

```bash
chmod o+x ~
chmod o+x ~/Music
```

Then allow group read/traverse access to the library:

```bash
chmod -R g+rX ~/Music/Normalized\ Library
```

After changing permissions:

```bash
sudo systemctl restart navidrome
```

---

# 🖼️ Covers are missing

If many covers are missing, the most common reason is API rate limiting.

Run:

```bash
python fix_covers.py
```

The script intentionally adds delays between requests.

If necessary, run it again:

```bash
python fix_covers.py
```

Check the log:

```bash
tail fix_covers.log
```

---

# 📱 iPhone cannot connect

Check the following:

### 1. Use HTTP

Correct:

```text
http://192.168.1.42:4533
```

Not:

```text
https://192.168.1.42:4533
```

### 2. Same Wi-Fi

The iPhone and ThinkPad must be on the same local network.

### 3. Check server IP

```bash
ip route get 1.1.1.1 | awk '{print $7; exit}'
```

### 4. Check UFW

```bash
sudo ufw status
```

### 5. Check Navidrome

```bash
sudo systemctl status navidrome
```

---

# 💾 Backup

Минимальный backup должен включать:

* музыкальную библиотеку;
* Navidrome configuration;
* systemd service;
* project source code.

Создать архив:

```bash
tar -czf local-spotify-backup-$(date +%Y%m%d).tar.gz \
    ~/Music/Normalized\ Library \
    /etc/navidrome/navidrome.toml \
    ~/.config/systemd/user/music-adder.service \
    ~/localSpotify
```

> `*.db` и `*.session` отдельно сохранять не требуется: они могут быть пересозданы.

Для полноценного backup также рекомендуется хранить копию `.env`, если он содержит необходимые настройки.

---

# 🔄 Update

Получить последнюю версию проекта:

```bash
cd ~/localSpotify
git pull
```

Перезапустить сервис:

```bash
systemctl --user restart music-adder
```

Если изменились Python dependencies:

```bash
cd adder

source .venv/bin/activate
pip install -r requirements.txt
deactivate

systemctl --user restart music-adder
```

Проверить:

```bash
systemctl --user status music-adder
```

---

# 🗑️ Uninstall

Если необходимо полностью удалить систему:

## 1. Stop services

```bash
systemctl --user stop music-adder
systemctl --user disable music-adder

sudo systemctl stop navidrome
sudo systemctl disable navidrome
```

## 2. Remove packages

```bash
sudo pacman -R navidrome
```

## 3. Remove project and library

```bash
rm -rf ~/localSpotify
rm -rf ~/Music/Normalized\ Library
```

## 4. Remove Navidrome data and configuration

```bash
sudo rm -rf /var/lib/navidrome
sudo rm -f /etc/navidrome/navidrome.toml
```

Remove the user service:

```bash
rm -f ~/.config/systemd/user/music-adder.service
```

Reload user systemd:

```bash
systemctl --user daemon-reload
```

## 5. Remove UFW rules

```bash
sudo ufw delete allow 4533
sudo ufw delete allow 8787
```

---

# 🧩 Technology Stack

```text
Operating System
└── Arch Linux

Backend
├── Python 3.10+
├── FastAPI
└── systemd

Music Server
└── Navidrome

Media
├── yt-dlp
├── FFmpeg
└── M4A / AAC

Metadata
├── iTunes API
└── Deezer API

Protocol
└── Subsonic API

Client
└── iOS / Amperfy

Security
└── UFW / Local LAN
```

---

# 🎯 Project workflow

Весь проект построен вокруг максимально простого сценария:

```text
                    ┌───────────────┐
                    │    YouTube    │
                    └───────┬───────┘
                            │
                            ▼
                    ┌───────────────┐
                    │     adder     │
                    │    :8787      │
                    └───────┬───────┘
                            │
                            ▼
                    ┌───────────────┐
                    │    yt-dlp     │
                    │      ↓        │
                    │      M4A      │
                    └───────┬───────┘
                            │
                            ▼
                 ┌─────────────────────┐
                 │      Metadata       │
                 │                     │
                 │ Artist / Title      │
                 │ Artwork / Tags      │
                 └──────────┬──────────┘
                            │
                            ▼
                 ┌─────────────────────┐
                 │ Normalized Library  │
                 └──────────┬──────────┘
                            │
                            ▼
                 ┌─────────────────────┐
                 │      Navidrome      │
                 │       :4533         │
                 └──────────┬──────────┘
                            │
                       Subsonic API
                            │
                            ▼
                 ┌─────────────────────┐
                 │       Amperfy       │
                 │        iOS          │
                 └─────────────────────┘
```

---

# 🔐 Privacy

Музыкальная библиотека хранится локально.

После загрузки и обработки трека воспроизведение происходит непосредственно с собственного сервера:

```text
iPhone
   │
   │ LAN
   ▼
ThinkPad
   │
   ▼
Navidrome
   │
   ▼
Local Music Library
```

Внешние API используются только для вспомогательных задач:

* поиск YouTube-контента;
* поиск метаданных;
* получение обложек.

---

# 📌 Status

**Status:** 🟢 Personal / Self-hosted

Проект предназначен для личного домашнего использования и находится в активной разработке.

---

# 📄 License

Private project for personal use.

Проект не предназначен для коммерческого предоставления публичного музыкального сервиса.
