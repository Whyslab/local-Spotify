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

import json
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
# Gdk has to be pinned too. Without it gi resolves Gdk to the newest it can
# find -- 4.0 on this machine -- and then refuses to load it alongside Gtk 3.0.
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")

import dbus  # noqa: E402
import dbus.mainloop.glib  # noqa: E402
import dbus.service  # noqa: E402
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402

APP_ID = "org.whyslab.localSpotify"
SERVICE_URL = os.environ.get("LOCAL_SPOTIFY_URL", "http://127.0.0.1:8787")
REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / "adder" / ".env"


def read_token() -> str:
    """Take API_TOKEN from adder/.env.

    The shell runs on the same machine, as the same user, as the service whose
    file this is: if it can read the token at all it could read the library
    directly. Reading it here means the window opens on the library rather than
    on a login box, without inventing a second place to keep a secret.
    """
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_TOKEN="):
                return line.split("=", 1)[1].strip()
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
        }.get(state.get("status"), "Stopped")
        self.position = int(float(state.get("position") or 0) * 1_000_000)

        title = state.get("title") or ""
        artist = state.get("artist") or ""
        self.metadata = {
            # A stable object path per track, which is what clients key on.
            "mpris:trackid": dbus.ObjectPath(
                "/org/whyslab/localSpotify/track/" + str(abs(hash(state.get("path", ""))))
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
        token = read_token()
        if token:
            manager.add_script(
                WebKit2.UserScript.new(
                    f"localStorage.setItem('token', {json.dumps(token)});",
                    WebKit2.UserContentInjectedFrames.TOP_FRAME,
                    WebKit2.UserScriptInjectionTime.START,
                    None,
                    None,
                )
            )

        manager.register_script_message_handler("mpris")
        manager.connect("script-message-received::mpris", self.on_mpris_message)

        self.webview = WebKit2.WebView.new_with_user_content_manager(manager)
        settings = self.webview.get_settings()
        settings.set_enable_developer_extras(True)
        # The page only ever plays audio the user asked for, and a shell that
        # needs a click before every track is not a music player.
        settings.set_media_playback_requires_user_gesture(False)

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

    def on_key(self, _widget, event):
        """Keys that belong to the window rather than to the page."""
        if event.keyval == Gdk.KEY_F5:
            self.webview.reload()
            return True
        if event.keyval == Gdk.KEY_space and not self._typing():
            self.call_js("togglePlay()")
            return True
        return False

    def _typing(self) -> bool:
        """Space must still be a space while a search box has focus."""
        return False  # the page swallows key events in its own inputs first


def main() -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    window = PlayerWindow()
    try:
        bus_name = dbus.service.BusName(
            "org.mpris.MediaPlayer2.local-Spotify", bus=dbus.SessionBus()
        )
        window.mpris = MprisService(bus_name, window)
    except Exception as exc:  # noqa: BLE001
        # Media keys are a convenience; the window is the point.
        print(f"MPRIS unavailable, media keys will not work: {exc}", file=sys.stderr)

    window.show_all()
    GLib.set_prgname(APP_ID)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
