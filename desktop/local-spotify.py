#!/usr/bin/env python3
"""A window around the local-Spotify page, plus the two things a browser tab cannot do.

The player is one web page; this is not a second implementation of it. What the
shell adds is what a tab could not: an application window with its own icon, and
media keys, which need an MPRIS service on the session bus.

On the WebKit binding. This uses WebKit2 4.1, which is the GTK3 build, because
that is what is installed -- the engine is 2.52.6, the same version the GTK4
package ships. The planning note that "webkit2gtk-4.1 is already there, so a
GTK4 shell costs nothing" confused two packages: webkitgtk-6.0 is the GTK4 one
and is not installed. For a window whose entire content is a WebView the
toolkit is invisible, and a second web engine on a machine with 2.4 GB free is
a real cost. Moving to GTK4 later is the import block below plus Gtk.Window
construction; nothing else in this file knows the difference.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import gi

gi.require_version("Gtk", "3.0")
# Gdk has to be pinned too. Without it gi resolves Gdk to the newest it can
# find -- 4.0 on this machine -- and then refuses to load it alongside Gtk 3.0.
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")

import dbus  # noqa: E402
import dbus.mainloop.glib  # noqa: E402
import dbus.service  # noqa: E402
from gi.repository import GLib  # noqa: E402

# Имя программы, по которому окно узнают панель задач и док: класс окна в X11
# и app_id в Wayland. Совпадает с именем ярлыка local-spotify.desktop, его
# значком и DesktopEntry в MPRIS ниже — одно имя на всё.
#
# Ставится до импорта Gtk: GDK читает имя, когда подключается к дисплею, а
# это происходит при импорте. Раньше вызов стоял после show_all() и не делал
# ничего — окно называлось по файлу скрипта, «local-spotify.py».
APP_ID = "local-spotify"
GLib.set_prgname(APP_ID)

from gi.repository import Gdk, Gio, Gtk, WebKit2  # noqa: E402

SERVICE_URL = os.environ.get("LOCAL_SPOTIFY_URL", "http://127.0.0.1:8787")

# Режим замера (scripts/measure_window.py): страница сообщает окну время своей
# готовности и разрывы кадров при прокрутке, окно печатает их строкой «PERF {…}»
# и, если попросили, закрывается. В обычном окне этого обработчика нет вовсе.
PERF = os.environ.get("LOCAL_SPOTIFY_PERF") == "1"
PERF_SCROLL = os.environ.get("LOCAL_SPOTIFY_PERF_SCROLL") == "1"
PERF_EXIT = os.environ.get("LOCAL_SPOTIFY_PERF_EXIT") == "1"

# Ставится в страницу только в режиме замера. Метку «app-ready» ставит app.js.
PERF_SCRIPT = """
(() => {
  if (location.origin !== ORIGIN) return;
  const post = (m) => window.webkit.messageHandlers.perf.postMessage(JSON.stringify(m));
  addEventListener("error", (e) => post({ event: "error", message: String(e.message) }));
  addEventListener("unhandledrejection", (e) =>
    post({ event: "error", message: String(e.reason) }));
  async function scrollWholeLibrary() {
    switchView("viewLibrary");
    while (!document.querySelector("#library .track")) await new Promise((r) => setTimeout(r, 50));
    for (;;) {
      const more = document.querySelector("#library .library-more");
      if (!more) break;
      if (!more.disabled) more.click();
      await new Promise((r) => setTimeout(r, 100));
    }
    let el = document.querySelector("#library");
    while (el && el !== document.body) {
      const s = getComputedStyle(el);
      if (/(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight) break;
      el = el.parentElement;
    }
    if (!el || el === document.body) el = document.scrollingElement;
    const gaps = [];
    let prev = performance.now(), running = true;
    const frame = (t) => {
      gaps.push(t - prev);
      prev = t;
      if (running) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
    el.scrollTop = 0;
    while (el.scrollTop + el.clientHeight < el.scrollHeight - 2) {
      el.scrollTop += 600;
      await new Promise((r) => setTimeout(r, 16));
    }
    await new Promise((r) => setTimeout(r, 300));
    running = false;
    const long = gaps.filter((g) => g > 50);
    post({ event: "scroll", frame_gaps_over_50ms: long.length, longest_gap_ms: Math.max(0, ...long),
           rows: document.querySelectorAll("#library .track").length });
  }
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) {
      if (e.name !== "app-ready") continue;
      post({ event: "ready", ready_ms: e.startTime });
      if (SCROLL) setTimeout(scrollWholeLibrary, 200);
    }
  }).observe({ type: "mark", buffered: true });
})();
"""
REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / "adder" / ".env"


def origin_of(url: str) -> str:
    """scheme://host:port, the unit a browser trusts as one site."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


SERVICE_ORIGIN = origin_of(SERVICE_URL)

# Данные страницы (localStorage: громкость, ширина панели, последний раздел)
# WebKit по умолчанию кладёт в папку по имени программы. Пока имя было
# «local-spotify.py», папка звалась так же; с новым именем WebKit завёл бы
# пустую, и настройки молча обнулились бы. Держим прежнюю папку явно.
WEB_DATA_NAME = "local-spotify.py"


def read_token() -> str:
    """Take API_TOKEN from adder/.env.

    The shell runs on the same machine, as the same user, as the service whose
    file this is: if it can read the token at all it could read the library
    directly. Reading it here means the window opens on the library rather than
    on a login box, without inventing a second place to keep a secret.
    """
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            # The same forms the service's dotenv accepts: leading spaces,
            # "export ", quotes. A quoted token read with its quotes gave 401.
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            key, sep, value = line.partition("=")
            if sep and key.strip() == "API_TOKEN":
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------
# MPRIS
# ---------------------------------------------------------------------------

MPRIS_PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"


class MprisService(dbus.service.Object):
    """Just enough MPRIS for the media keys and a "now playing" readout.

    The page is the source of truth for what is playing; this only mirrors it
    onto the bus and forwards key presses back. Everything it reports comes
    from a message the page sent.
    """

    def __init__(self, bus_name, window):
        super().__init__(bus_name, MPRIS_PATH)
        self.window = window
        self.status = "Stopped"
        self.metadata = {}
        self.position = 0

    # -- root interface --

    @dbus.service.method(ROOT_IFACE)
    def Raise(self):
        self.window.present()

    @dbus.service.method(ROOT_IFACE)
    def Quit(self):
        Gtk.main_quit()

    # -- player interface --

    @dbus.service.method(PLAYER_IFACE)
    def PlayPause(self):
        self.window.call_js("togglePlay()")

    @dbus.service.method(PLAYER_IFACE)
    def Play(self):
        self.window.call_js("if (player.audio.paused) togglePlay()")

    @dbus.service.method(PLAYER_IFACE)
    def Pause(self):
        self.window.call_js("if (!player.audio.paused) togglePlay()")

    @dbus.service.method(PLAYER_IFACE)
    def Stop(self):
        self.window.call_js("player.audio.pause(); player.audio.currentTime = 0")

    @dbus.service.method(PLAYER_IFACE)
    def Next(self):
        self.window.call_js("nextTrack()")

    @dbus.service.method(PLAYER_IFACE)
    def Previous(self):
        self.window.call_js("prevTrack()")

    # -- properties --

    def _properties(self, interface):
        if interface == ROOT_IFACE:
            return {
                "CanQuit": True,
                "CanRaise": True,
                "HasTrackList": False,
                "Identity": "local-Spotify",
                "DesktopEntry": "local-spotify",
                "SupportedUriSchemes": dbus.Array([], signature="s"),
                "SupportedMimeTypes": dbus.Array([], signature="s"),
            }
        if interface == PLAYER_IFACE:
            return {
                "PlaybackStatus": self.status,
                "Metadata": dbus.Dictionary(self.metadata, signature="sv"),
                "Position": dbus.Int64(self.position),
                "Rate": 1.0,
                "MinimumRate": 1.0,
                "MaximumRate": 1.0,
                "Volume": 1.0,
                "CanGoNext": True,
                "CanGoPrevious": True,
                "CanPlay": True,
                "CanPause": True,
                "CanSeek": False,
                "CanControl": True,
            }
        return {}

    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        return self._properties(interface).get(prop, "")

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return dbus.Dictionary(self._properties(interface), signature="sv")

    @dbus.service.method(PROPS_IFACE, in_signature="ssv")
    def Set(self, interface, prop, value):
        pass

    @dbus.service.signal(PROPS_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    def update(self, state: dict) -> None:
        """Take a state message from the page and publish it."""
        self.status = {
            "playing": "Playing",
            "paused": "Paused",
        }.get(str(state.get("status")), "Stopped")
        self.position = int(float(state.get("position") or 0) * 1_000_000)

        title = state.get("title") or ""
        artist = state.get("artist") or ""
        self.metadata = {
            # A stable object path per track, which is what clients key on.
            "mpris:trackid": dbus.ObjectPath(
                # sha1, not hash(): Python salts str hashes per process, so the
                # "stable" path changed with every launch.
                "/org/whyslab/localSpotify/track/"
                + hashlib.sha1(state.get("path", "").encode()).hexdigest()[:16]
            ),
            "mpris:length": dbus.Int64(int(float(state.get("duration") or 0) * 1_000_000)),
            "xesam:title": title,
            "xesam:artist": dbus.Array([artist] if artist else [], signature="s"),
        }
        self.PropertiesChanged(
            PLAYER_IFACE,
            dbus.Dictionary(
                {
                    "PlaybackStatus": self.status,
                    "Metadata": dbus.Dictionary(self.metadata, signature="sv"),
                },
                signature="sv",
            ),
            [],
        )


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


class PlayerWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="local-Spotify")
        self.set_default_size(1180, 820)
        self.set_icon_name("local-spotify")

        manager = WebKit2.UserContentManager()

        # WebKit keeps its own localStorage, so without this the window would
        # open on the token prompt every time. Injected before the document
        # runs, so the page finds the token already in place.
        #
        # Только на страницы самого сервиса. Без списка скрипт выполнялся на
        # любой странице, куда окно перейдёт, — и ключ оказался бы в
        # localStorage чужого сайта. Уйти окно никуда и не должно (см.
        # on_decide_policy), но ключ стережём и здесь.
        #
        # Порт шаблоны WebKit не различают (проверено: «http://127.0.0.1:8799/*»
        # не совпадает ни с чем), поэтому в списке — хост, а точное
        # совпадение с портом скрипт проверяет сам.
        # В режиме замера окно запускается из любой копии проекта, и своего
        # adder/.env у неё может не быть: ключ передаёт scripts/measure_window.py.
        token = (os.environ.get("LOCAL_SPOTIFY_TOKEN", "") if PERF else "") or read_token()
        if token:
            parts = urlsplit(SERVICE_URL)
            manager.add_script(
                WebKit2.UserScript.new(
                    f"if (location.origin === {json.dumps(SERVICE_ORIGIN)}) "
                    f"localStorage.setItem('token', {json.dumps(token)});",
                    WebKit2.UserContentInjectedFrames.TOP_FRAME,
                    WebKit2.UserScriptInjectionTime.START,
                    [f"{parts.scheme}://{parts.hostname}/*"],
                    None,
                )
            )

        manager.register_script_message_handler("mpris")
        manager.connect("script-message-received::mpris", self.on_mpris_message)

        if PERF:
            script = PERF_SCRIPT.replace("ORIGIN", json.dumps(SERVICE_ORIGIN)).replace(
                "SCROLL", "true" if PERF_SCROLL else "false"
            )
            manager.add_script(
                WebKit2.UserScript.new(
                    script,
                    WebKit2.UserContentInjectedFrames.TOP_FRAME,
                    WebKit2.UserScriptInjectionTime.START,
                    None,
                    None,
                )
            )
            manager.register_script_message_handler("perf")
            manager.connect("script-message-received::perf", self.on_perf_message)

        data = WebKit2.WebsiteDataManager(
            base_data_directory=os.path.join(GLib.get_user_data_dir(), WEB_DATA_NAME),
            base_cache_directory=os.path.join(GLib.get_user_cache_dir(), WEB_DATA_NAME),
        )
        self.webview = WebKit2.WebView(
            web_context=WebKit2.WebContext.new_with_website_data_manager(data),
            user_content_manager=manager,
            # Without it WebKit refuses play() on a second element while the
            # first one plays: the DJ transition (web/mix.js) starts the next
            # track that way.
            website_policies=WebKit2.WebsitePolicies(autoplay=WebKit2.AutoplayPolicy.ALLOW),
        )
        settings = self.webview.get_settings()
        settings.set_enable_developer_extras(True)
        # The page only ever plays audio the user asked for, and a shell that
        # needs a click before every track is not a music player.
        settings.set_media_playback_requires_user_gesture(False)

        self.webview.connect("decide-policy", self.on_decide_policy)

        self.add(self.webview)
        self.webview.load_uri(SERVICE_URL)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self.on_key)

        self.mpris = None

    def call_js(self, script: str) -> None:
        self.webview.run_javascript(script, None, None, None)

    def on_mpris_message(self, _manager, message):
        try:
            payload = json.loads(message.get_js_value().to_string())
        except Exception:
            return
        if self.mpris is not None:
            self.mpris.update(payload)

    def on_perf_message(self, _manager, message):
        """Режим замера: строка «PERF {…}» с настенным временем получения.

        Время — time.time(): scripts/measure_window.py засекает его до запуска
        процесса, так что в цифру входит и старт Python, GTK и окна.
        """
        try:
            payload = json.loads(message.get_js_value().to_string())
        except Exception:
            return
        payload["wall_ms"] = time.time() * 1000
        print("PERF " + json.dumps(payload), flush=True)
        done = payload.get("event") == ("scroll" if PERF_SCROLL else "ready")
        if PERF_EXIT and done:
            GLib.timeout_add(300, Gtk.main_quit)

    def on_decide_policy(self, _webview, decision, decision_type):
        """Окно показывает только сервис.

        Переход на чужой адрес — ссылка, перенаправление или файл, брошенный
        мимо поля загрузки (WebKit открывает его вместо страницы), — внутри
        окна не выполняется. Ссылку, по которой нажал человек, открываем в
        браузере по умолчанию: там ей и место.
        """
        if decision_type not in (
            WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
            WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION,
        ):
            return False
        action = decision.get_navigation_action()
        uri = action.get_request().get_uri() or ""
        if origin_of(uri) == SERVICE_ORIGIN:
            return False
        # about:blank и подобное — пустые кадры самого движка, не переходы.
        if uri.startswith("about:"):
            return False
        decision.ignore()
        clicked = action.get_navigation_type() == WebKit2.NavigationType.LINK_CLICKED
        if clicked and uri.startswith(("http://", "https://")):
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except GLib.Error as exc:
                print(f"Could not open {uri}: {exc}", file=sys.stderr)
        return True

    def on_key(self, _widget, event):
        """Keys that belong to the window rather than to the page."""
        if event.keyval == Gdk.KEY_F5:
            self.webview.reload()
            return True
        return False

    # Пробел здесь больше не обрабатывается.
    #
    # Было: `if event.keyval == Gdk.KEY_space and not self._typing()`, где
    # _typing() всегда возвращал False с комментарием «страница сама съедает
    # клавиши в своих полях». Это неверно: key-press-event на окне GTK
    # срабатывает РАНЬШЕ, чем WebKit отдаёт событие странице, так что пробел
    # уходил в паузу даже посреди набора в поиске фонотеки.
    #
    # Теперь пробел ловит сама страница (web/player.js), где синхронно виден
    # document.activeElement. Мультимедийные клавиши через MPRIS по-прежнему
    # зовут togglePlay() отсюда — они не конфликтуют, у них свои коды.


def main() -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    window = PlayerWindow()
    if PERF:
        # Замер запускает окна одно за другим — медиаклавиши у настоящего окна
        # пользователя они отбирать не должны.
        window.show_all()
        Gtk.main()
        return 0
    try:
        bus_name = dbus.service.BusName(
            "org.mpris.MediaPlayer2.local-Spotify", bus=dbus.SessionBus()
        )
        window.mpris = MprisService(bus_name, window)
    except Exception as exc:  # noqa: BLE001
        # Media keys are a convenience; the window is the point.
        print(f"MPRIS unavailable, media keys will not work: {exc}", file=sys.stderr)

    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
