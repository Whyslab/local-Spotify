# 🎵 Personal Music Server

**Персональный офлайн-музыкальный сервер на Arch Linux с iOS-клиентом.**

Полностью самостоятельная домашняя музыкальная система без зависимости от Spotify: музыка хранится локально, добавляется через веб-интерфейс из YouTube, автоматически индексируется Navidrome и доступна с iPhone через Subsonic API.

> **Проект предназначен для личного использования в домашней сети.**

---

## ✨ Возможности

* 🎧 Локальная музыкальная библиотека
* 📱 Прослушивание с iPhone через **Amperfy**
* 🖥️ Сервер на **Arch Linux**
* 🎵 **Navidrome** как музыкальный сервер
* ▶️ Добавление музыки через YouTube
* 🌐 Удобный веб-интерфейс для загрузки треков
* ⚡ Автоматическое обнаружение новых файлов через `inotify`
* 🏷️ Автоматическое заполнение метаданных
* 🖼️ Автоматический поиск и встраивание обложек
* 📥 Офлайн-кэш музыки на iPhone
* 🔄 Импорт больших плейлистов из Spotify
* 🚀 Автозапуск сервисов через systemd
* 🔒 Доступ к сервисам ограничен локальной сетью

---

## 🏗️ Архитектура

```text
                         ┌──────────────────────┐
                         │        iPhone        │
                         │                      │
                         │       Amperfy        │
                         │          │           │
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
│        YouTube                                             │
│            │                                                │
│            ▼                                                │
│      yt-dlp → M4A                                         │
└─────────────────────────────────────────────────────────────┘
```

### Компоненты

| Компонент      | Назначение                          |
| -------------- | ----------------------------------- |
| **Navidrome**  | Музыкальный сервер и Subsonic API   |
| **adder**      | Веб-интерфейс для добавления музыки |
| **yt-dlp**     | Загрузка аудио с YouTube            |
| **watcher**    | Обнаружение новых файлов            |
| **Amperfy**    | iOS-клиент с офлайн-кэшем           |
| **iTunes API** | Поиск обложек                       |
| **Deezer API** | Fallback для обложек                |

---

# 🚀 Быстрый старт

## 1. Установка Navidrome

Установите Navidrome:

```bash
yay -S navidrome
```

Создайте конфигурацию:

```bash
sudo tee /etc/navidrome/navidrome.toml <<'EOF'
MusicFolder = "/home/$USER/Music/Normalized Library"
DataFolder = "/var/lib/navidrome"
Address = "0.0.0.0"
Port = 4533
LogLevel = "info"
EOF
```

Настройте права:

```bash
sudo chown -R navidrome:navidrome /var/lib/navidrome
```

При необходимости добавьте ограничение доступа к домашнему каталогу:

```bash
sudo systemctl edit navidrome
```

Добавьте:

```ini
[Service]
ProtectHome=read-only
```

Запустите Navidrome:

```bash
sudo systemctl enable --now navidrome
```

Откройте:

```text
http://localhost:4533
```

Создайте администратора.

---

# 🌐 2. Запуск adder

Перейдите в каталог проекта:

```bash
cd adder
```

Создайте виртуальное окружение:

```bash
python -m venv .venv
source .venv/bin/activate
```

Установите зависимости:

```bash
pip install -r requirements.txt
```

Запустите сервер:

```bash
python app.py
```

Веб-интерфейс будет доступен по адресу:

```text
http://localhost:8787
```

С телефона в той же LAN можно открыть:

```text
http://<IP_сервера>:8787
```

Вставьте ссылку на YouTube → нажмите **«Добавить»**.

После обработки трек автоматически появится в музыкальной библиотеке Navidrome.

---

# 📱 3. Подключение iPhone

Установите **Amperfy** из App Store.

Создайте новое подключение:

```text
Server:
http://<IP_сервера>:4533
```

Введите логин и пароль администратора Navidrome.

После подключения библиотека будет доступна прямо в приложении.

Amperfy также поддерживает локальный кэш, поэтому музыку можно слушать офлайн.

---

# 🎵 Добавление нового трека

Полный workflow выглядит так:

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

### Пошагово

1. Найдите трек на YouTube.
2. Откройте веб-интерфейс `adder`.
3. Вставьте ссылку.
4. Нажмите **«Добавить»**.
5. Дождитесь статуса `done`.
6. Navidrome автоматически обнаружит новый файл.
7. Трек появится в Amperfy.

Обычно после завершения загрузки трек становится доступен в Navidrome в течение нескольких секунд.

---

# 🔄 Миграция плейлиста из Spotify

Проект также поддерживает перенос большой музыкальной библиотеки из Spotify.

## 1. Экспорт Spotify-плейлиста

Можно воспользоваться Spotify Playlist Exporter:

[Spotify Playlist Exporter — Chosic](https://www.chosic.com/spotify-playlist-exporter/svg?utm_source=chatgpt.com)

Экспортируйте плейлист в `.txt`.

---

## 2. Поиск YouTube-ссылок

Перейдите в `scripts`:

```bash
cd scripts
```

Запустите:

```bash
python youtube_links.py
```

Скрипт:

* читает список треков из Spotify;
* формирует запрос `Artist - Title`;
* выполняет поиск через `yt-dlp`;
* выбирает первый результат;
* сохраняет найденные URL.

Результат:

```text
spotify_tracks_youtube.csv
```

---

# 📥 3. Скачивание и нормализация

Запустите:

```bash
source ../adder/.venv/bin/activate
python migrate_playlist.py
```

Скрипт автоматически:

1. скачивает аудио через `yt-dlp`;
2. извлекает название и исполнителя;
3. ищет обложку;
4. встраивает метаданные;
5. встраивает обложку в `.m4a`;
6. перемещает файл в нормализованную библиотеку.

Структура библиотеки:

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
└── ...
```

### Источники обложек

Основной источник:

```text
iTunes API
```

Fallback:

```text
YouTube thumbnail
```

---

# 🖼️ 4. Восстановление отсутствующих обложек

Иногда iTunes API может временно ограничивать количество запросов.

Для повторной обработки файлов без обложки:

```bash
python fix_covers.py
```

Скрипт:

* ищет `.m4a` без `covr`;
* обращается к Deezer API;
* повторяет неудачные запросы;
* делает паузы между запросами;
* встраивает найденную обложку непосредственно в файл.

---

# ⚙️ Автозапуск adder

Создайте user-level systemd service:

```bash
mkdir -p ~/.config/systemd/user
```

Создайте файл:

```bash
nano ~/.config/systemd/user/music-adder.service
```

Содержимое:

```ini
[Unit]
Description=YouTube to Navidrome adder

[Service]
ExecStart="%h/localSpotify/adder/.venv/bin/python" "%h/localSpotify/adder/app.py"
Restart=on-failure

[Install]
WantedBy=default.target
```

Включите сервис:

```bash
systemctl --user enable --now music-adder
```

Для работы user services после выхода из системы:

```bash
loginctl enable-linger $USER
```

Проверить состояние:

```bash
systemctl --user status music-adder
```

Посмотреть логи:

```bash
journalctl --user -u music-adder -f
```

---

# 🔥 UFW

Если используется UFW и сервер должен быть доступен только из домашней сети:

```bash
sudo ufw allow from 192.168.1.0/24 \
    to any port 4533 proto tcp \
    comment "Navidrome"

sudo ufw allow from 192.168.1.0/24 \
    to any port 8787 proto tcp \
    comment "Adder"

sudo ufw reload
```

> Замените `192.168.1.0/24` на свою локальную подсеть.

---

# 📁 Структура проекта

```text
localSpotify/
├── README.md
├── .gitignore
├── .env.example
│
├── adder/
│   ├── app.py
│   ├── fix_covers.py
│   ├── requirements.txt
│   └── tmp/
│
└── scripts/
    ├── youtube_links.py
    └── normalize_library.py
```

### Основные файлы

| Файл                           | Назначение                                     |
| ------------------------------ | ---------------------------------------------- |
| `adder/app.py`                 | FastAPI-приложение и фоновые воркеры           |
| `adder/fix_covers.py`          | Восстановление отсутствующих обложек           |
| `scripts/youtube_links.py`     | Поиск YouTube-ссылок для Spotify-треков        |
| `scripts/normalize_library.py` | Загрузка, тегирование и организация библиотеки |
| `adder/tmp/`                   | Временные файлы загрузок                       |

---

# 🧩 Технологический стек

```text
Server
├── Arch Linux
├── Python
├── FastAPI
├── Navidrome
└── systemd

Media
├── yt-dlp
├── M4A / AAC
└── Embedded metadata + artwork

APIs
├── Subsonic API
├── iTunes API
└── Deezer API

Client
└── iOS / Amperfy

Networking
└── Local LAN + UFW
```

---

# 🎯 Цель проекта

Идея проекта максимально простая:

> **Своя музыка → свой сервер → свой клиент → никакой зависимости от Spotify.**

Вместо того чтобы хранить музыкальную библиотеку исключительно в стриминговом сервисе, вся коллекция находится локально и контролируется владельцем сервера.

При этом пользовательский сценарий остаётся максимально похожим на Spotify:

```text
Найти музыку
     ↓
Вставить YouTube URL
     ↓
Добавить через телефон
     ↓
Автоматическая обработка
     ↓
Navidrome
     ↓
Amperfy
     ↓
Слушать
```

---

# 🔐 Приватность

Музыкальная библиотека хранится локально на сервере.

Основной доступ:

```text
iPhone
   ↓
LAN
   ↓
ThinkPad
   ↓
Navidrome
```

Сервер не требует постоянной зависимости от облачного музыкального сервиса для воспроизведения уже загруженной библиотеки.

---

# 📌 Статус

**Status:** 🟢 Personal / Self-hosted

Проект находится в активной разработке и предназначен прежде всего для личного домашнего использования.

---

# 📄 Лицензия

Приватный проект для личного использования.

Не предназначен для коммерческого распространения или предоставления публичного музыкального сервиса.
