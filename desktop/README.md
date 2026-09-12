# Оболочка приложения

Окно вокруг той же страницы, что открывается в браузере. Второй реализации
плеера здесь нет — оболочка добавляет ровно то, чего не может вкладка:
собственное окно с иконкой и медиаклавиши.

## Поставить

```bash
cp desktop/local-spotify.desktop ~/.local/share/applications/
cp desktop/icons/local-spotify.svg ~/.local/share/icons/hicolor/scalable/apps/
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor
update-desktop-database ~/.local/share/applications
```

Путь к запускаемому файлу в `.desktop` прописан абсолютным — если репозиторий
лежит не в `~/Projects/local-Spotify`, поправь строку `Exec=`.

Запуск руками:

```bash
./desktop/local-spotify.py
```

Адрес сервиса можно переопределить: `LOCAL_SPOTIFY_URL=http://192.168.1.5:8787`.

## Токен

Оболочка читает `API_TOKEN` из `adder/.env` и кладёт его в `localStorage`
своего WebView до загрузки страницы — иначе окно каждый раз открывалось бы на
форме ввода ключа, потому что у WebKit собственное хранилище, отдельное от
браузера.

Это не ослабление доступа: оболочка работает на той же машине и под тем же
пользователем, что и сервис. Кто может прочитать этот файл, тот и так имеет
доступ к фонотеке напрямую.

## Медиаклавиши

Оболочка поднимает MPRIS-сервис `org.mpris.MediaPlayer2.local-Spotify`.
Страница шлёт ему своё состояние, он выкладывает его на шину и передаёт
обратно команды — это и есть механизм, которым рабочий стол обрабатывает
кнопки «играть», «дальше» и «назад».

Проверить, что он живой:

```bash
busctl --user list | grep local-Spotify
busctl --user call org.mpris.MediaPlayer2.local-Spotify \
  /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player PlayPause
```

## Почему GTK3, а не GTK4

Движок один и тот же — WebKitGTK 2.52.6. Разница только в том, к какому
тулкиту он привязан: `webkit2gtk-4.1` собран под GTK3 и **уже стоит** в
системе, а GTK4-версия живёт в отдельном пакете `webkitgtk-6.0`, которого нет.

Для окна, всё содержимое которого — один WebView, тулкит невидим, а второй
веб-движок на машине с 2,4 ГБ свободной памяти стоит вполне ощутимо. Если
однажды понадобится GTK4:

```bash
sudo pacman -S webkitgtk-6.0
```

и в `local-spotify.py` заменить блок `gi.require_version` на `Gtk 4.0` /
`WebKit 6.0`, а `window.add(...)` на `window.set_child(...)`. Больше в файле
про тулкит ничего не знает.

## Правило окна для Hyprland (по желанию)

Приложение нормально ложится в тайлинг и без правил. Если хочется, чтобы оно
всегда открывалось плавающим окном определённого размера, — в `hyprland.conf`,
в синтаксисе 0.56:

```
windowrule = match:class ^(local-spotify\.py)$, float true, size 1180 820, center true
```

Класс окна — `local-spotify.py`: так его видит Hyprland, и то же значение стоит
в `StartupWMClass` в `.desktop`, чтобы окно связывалось со своей иконкой.
