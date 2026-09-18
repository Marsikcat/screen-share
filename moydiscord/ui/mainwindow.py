"""Main window: three columns, settings overlay, tray icon, notifications."""

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMenu, QPlainTextEdit, QStackedWidget, QSystemTrayIcon, QTextEdit,
                               QVBoxLayout, QWidget)

from .. import updater
from ..hotkeys import HotkeyManager
from ..config import APP_NAME, ROOT
from . import dialogs, icons, theme
from .chat import ChatView
from .members import MemberList
from .settings import SettingsView
from .settings_pages import run_async
from .sidebar import Sidebar
from .theme import T
from .voiceview import SourcePicker, VoiceView
from .widgets import Toast, button


def app_icon(accent=None, badge=False):
    pm = QPixmap(256, 256)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(accent or T.c["accent"]))
    p.drawRoundedRect(QRectF(8, 8, 240, 240), 64, 64)
    p.drawPixmap(48, 52, icons.pixmap("message", "#ffffff", 160, 2.2).scaled(
        160, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    if badge:
        p.setBrush(QColor(T.c["red"]))
        p.drawEllipse(QRectF(160, 150, 96, 96))
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, core, app, start_hidden=False):
        super().__init__()
        self.core, self.app, self.s = core, app, core.s
        self.quitting = False
        self.current = None
        self.last_notified = None
        self.settings_view = None
        self._tray_hint_shown = False
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(980, 620)
        self.setWindowIcon(app_icon())
        geo = self.s.get("window")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        else:
            self.resize(1320, 820)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.toaster = Toast(self)
        self.build()
        self.tray = self._make_tray()

        core.toast.connect(self.toast)
        core.notify.connect(self.on_notify)
        core.channels_changed.connect(self._check_current)
        # global hotkeys (mute, deafen, stream…) — also active while the window is focused
        self.hotkeys = HotkeyManager(self.s, lambda: QApplication.activeWindow() is not None,
                                     self._is_typing)
        self.hotkeys.triggered.connect(self.on_hotkey)
        self.hotkeys.start()
        # in-window shortcuts; the Cyrillic twins make them work on the Russian layout too
        for keys, fn in (
            (("Ctrl+K", "Ctrl+Л"), self.quick_switch),
            (("Alt+Up",), lambda: self.step_channel(-1)),
            (("Alt+Down",), lambda: self.step_channel(1)),
            (("Alt+Shift+Up",), lambda: self.step_channel(-1, unread=True)),
            (("Alt+Shift+Down",), lambda: self.step_channel(1, unread=True)),
            (("Ctrl+E", "Ctrl+У"), self._emoji),
            (("Ctrl+Shift+U", "Ctrl+Shift+Г"), self._attach),
            (("Ctrl+,", "Ctrl+Б"), lambda: self.open_settings("profile")),
            (("Ctrl+/", "Ctrl+."), self.show_shortcuts),
        ):
            sc = QShortcut(self)
            sc.setKeys([QKeySequence(k) for k in keys])
            sc.activated.connect(fn)

        if self.s["check_updates"]:
            QTimer.singleShot(4000, self._check_updates)
        if not start_hidden:
            self.show()
            theme.style_window(self)

    # ── layout ──────────────────────────────────────────────────────
    def build(self):
        root = QWidget()
        root.setObjectName("Root")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.sidebar = Sidebar(self.core, self)
        self.sidebar.channel_selected.connect(self.open_channel)
        self.sidebar.open_settings.connect(self.open_settings)
        self.sidebar.stream_requested.connect(self.pick_stream)
        center = QWidget()
        v = QVBoxLayout(center)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.banner = QFrame()
        self.banner.setObjectName("Banner")
        self.banner.setFixedHeight(36)
        b = QHBoxLayout(self.banner)
        b.setContentsMargins(16, 0, 8, 0)
        self.banner_text = QLabel()
        self.banner_text.setStyleSheet("font-weight: 600;")
        b.addStretch(1)
        b.addWidget(self.banner_text)
        go = button("Подробнее", "secondary", lambda: self.open_settings("updates"))
        go.setStyleSheet("padding: 3px 12px;")
        b.addWidget(go)
        b.addStretch(1)
        self.banner.hide()
        v.addWidget(self.banner)
        self.views = QStackedWidget()
        self.chat = ChatView(self.core)
        self.chat.members_toggled.connect(self._toggle_members)
        self.voiceview = VoiceView(self.core)
        self.voiceview.stream_requested.connect(self.pick_stream)
        self.views.addWidget(self.chat)
        self.views.addWidget(self.voiceview)
        v.addWidget(self.views, 1)
        self.members = MemberList(self.core)
        self.members.setVisible(self.s.get("show_members", True))
        h.addWidget(self.sidebar)
        h.addWidget(center, 1)
        h.addWidget(self.members)
        self.root = root
        self.stack.addWidget(root)
        self.stack.setCurrentWidget(root)
        target = self.current or self.s["last_channel"]
        self.current = None
        if not self.core.store.channel(target or ""):
            texts = self.core.store.channel_list("text")
            target = texts[0]["id"] if texts else None
        if target:
            self.open_channel(target)

    def rebuild(self):
        """Re-create every view (after a theme change — styles are baked in at creation)."""
        old = self.root
        self.stack.removeWidget(old)
        old.deleteLater()
        self.build()

    def open_channel(self, cid):
        ch = self.core.store.channel(cid)
        if not ch:
            return
        self.current = cid
        if ch["kind"] == "text":
            self.chat.set_channel(cid)
            self.views.setCurrentWidget(self.chat)
            self.s["last_channel"] = cid
            self.s.save()
        else:
            self.voiceview.set_channel(cid)
            self.views.setCurrentWidget(self.voiceview)
        self.sidebar.select(cid)
        self.close_settings()

    def _check_current(self):
        if self.current and not self.core.store.channel(self.current):
            texts = self.core.store.channel_list("text")
            if texts:
                self.open_channel(texts[0]["id"])

    def _toggle_members(self):
        vis = not self.members.isVisible()
        self.members.setVisible(vis)
        self.s["show_members"] = vis
        self.s.save()

    # ── hotkeys ─────────────────────────────────────────────────────
    @staticmethod
    def _is_typing():
        return isinstance(QApplication.focusWidget(), (QLineEdit, QPlainTextEdit, QTextEdit))

    def on_hotkey(self, action):
        core = self.core
        if action == "toggle_mute":
            core.toggle_mute()
        elif action == "toggle_deafen":
            core.toggle_deafen()
        elif action == "leave_voice":
            core.leave_voice()
        elif action == "join_voice":
            cid = self.s["last_voice"] if core.store.channel(self.s["last_voice"] or "") else None
            cid = cid or next((c["id"] for c in core.store.channel_list("voice")), None)
            if cid:
                core.join_voice(cid)
        elif action == "toggle_stream":
            if core.sender.running:
                core.stop_stream()
            else:
                self.show_window()
                self.pick_stream()
        elif action == "show_window":
            if self.isVisible() and self.isActiveWindow():
                self.hide() if (self.tray and self.s["close_to_tray"]) else self.showMinimized()
            else:
                self.show_window()

    def _channel_order(self):
        return self.core.store.channel_list("text") + self.core.store.channel_list("voice")

    def step_channel(self, delta, unread=False):
        chans = self._channel_order()
        if unread:
            chans = [c for c in chans if c["kind"] == "text" and
                     (c["id"] == self.current or self.core.unread(c["id"])[0])]
        ids = [c["id"] for c in chans]
        if not ids:
            return
        i = ids.index(self.current) if self.current in ids else -1 if delta > 0 else len(ids)
        target = ids[(i + delta) % len(ids)]
        if target != self.current:
            self.open_channel(target)

    def quick_switch(self):
        d = dialogs.QuickSwitcher(self, self.core)
        if d.exec() and d.cid:
            self.open_channel(d.cid)

    def show_shortcuts(self):
        dialogs.ShortcutsHelp(self, self.s).exec()

    def _chat_active(self):
        return self.stack.currentWidget() is self.root and self.views.currentWidget() is self.chat

    def _emoji(self):
        if self._chat_active():
            self.chat.composer.open_emoji()

    def _attach(self):
        if self._chat_active():
            self.chat.composer.pick_files()

    # ── settings ────────────────────────────────────────────────────
    def open_settings(self, page="profile"):
        self.close_settings()
        self.settings_view = SettingsView(self, page)
        self.settings_view.closed.connect(self.close_settings)
        self.settings_view.appearance_changed.connect(self.apply_appearance)
        self.stack.addWidget(self.settings_view)
        self.stack.setCurrentWidget(self.settings_view)
        self.settings_view.setFocus()

    def close_settings(self):
        if self.settings_view:
            self.core.voice.monitor = False
            self.stack.setCurrentWidget(self.root)
            self.stack.removeWidget(self.settings_view)
            self.settings_view.deleteLater()
            self.settings_view = None
            self.sidebar.update_user()

    def apply_appearance(self):
        page = self.settings_view.page_key if self.settings_view else "appearance"
        theme.apply(self.app, self.s)
        icons.clear_cache()
        theme.style_window(self)
        self.setWindowIcon(app_icon())
        if self.tray:
            self.tray.setIcon(app_icon())
        self.close_settings()
        self.rebuild()
        self.open_settings(page)

    # ── channel dialogs ─────────────────────────────────────────────
    def create_channel_dialog(self, kind):
        title = "Создать текстовый канал" if kind == "text" else "Создать голосовой канал"
        name = dialogs.ask_text(self, title, "Название канала",
                                placeholder="новый-канал" if kind == "text" else "Новый канал",
                                ok="Создать канал")
        if name:
            ev = self.core.create_channel(kind, name)
            if ev and kind == "text":
                self.open_channel(ev["id"])

    def rename_channel_dialog(self, cid):
        ch = self.core.store.channel(cid)
        if ch:
            name = dialogs.ask_text(self, "Переименовать канал", "Название канала", ch["name"])
            if name:
                self.core.rename_channel(cid, name)

    def delete_channel_dialog(self, cid):
        ch = self.core.store.channel(cid)
        if ch and dialogs.confirm(self, "Удалить канал",
                                  f"Канал «{ch['name']}» пропадёт у всех участников комнаты. "
                                  f"Отменить это нельзя."):
            self.core.delete_channel(cid)

    def rename_room_dialog(self):
        name = dialogs.ask_text(self, "Переименовать комнату", "Название",
                                self.core.room_name(), text="Название увидят все участники комнаты.")
        if name:
            self.core.set_room_name(name)

    # ── voice & stream ──────────────────────────────────────────────
    def pick_stream(self):
        if not self.core.my_voice:
            self.toast("Сначала зайдите в голосовой канал", "error")
            return
        picker = SourcePicker(self.core, self)
        if picker.exec() and picker.source:
            self.core.start_stream(picker.source)

    def launch_classic(self):
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        subprocess.Popen([str(pyw if pyw.exists() else exe), str(ROOT / "screen_share.py")], cwd=str(ROOT))

    # ── notifications & tray ────────────────────────────────────────
    def toast(self, text, kind="info"):
        self.toaster.show_text(text, kind)

    def on_notify(self, title, body, cid):
        active = self.isVisible() and self.isActiveWindow()
        if active and self.current == cid and self.stack.currentWidget() is self.root:
            return
        self.core.voice.play("message")
        if not active:
            QApplication.alert(self)
            if self.s["notify"] and self.tray:
                self.last_notified = cid
                self.tray.showMessage(title, body, app_icon(), 5000)
        self.sidebar.update_unread()

    def update_badge(self):
        tray = getattr(self, "tray", None)
        if not tray:
            return
        total = sum(self.core.unread(c["id"])[0] for c in self.core.store.channel_list("text")
                    if c["id"] != self.current or not self.isActiveWindow())
        tray.setToolTip(f"{APP_NAME} — непрочитанных: {total}" if total else APP_NAME)
        tray.setIcon(app_icon(badge=total > 0))

    def _make_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu()
        menu.addAction(f"Открыть {APP_NAME}", self.show_window)
        menu.addSeparator()
        self.tray_mute = menu.addAction("Выключить микрофон", self.core.toggle_mute)
        self.tray_deaf = menu.addAction("Выключить звук", self.core.toggle_deafen)
        self.tray_leave = menu.addAction("Отключиться от голосового канала", self.core.leave_voice)
        menu.addSeparator()
        menu.addAction("Настройки", lambda: (self.show_window(), self.open_settings("profile")))
        menu.addAction("Выход", self.quit)
        menu.aboutToShow.connect(self._update_tray_menu)
        tray.setContextMenu(menu)
        self._tray_menu = menu
        tray.activated.connect(self._tray_activated)
        tray.messageClicked.connect(self._tray_message_clicked)
        tray.setToolTip(APP_NAME)
        tray.show()
        return tray

    def _update_tray_menu(self):
        s = self.s
        self.tray_mute.setText("Включить микрофон" if s["muted"] else "Выключить микрофон")
        self.tray_deaf.setText("Включить звук" if s["deafened"] else "Выключить звук")
        self.tray_leave.setVisible(bool(self.core.my_voice))

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible() and self.isActiveWindow():
                self.hide()
            else:
                self.show_window()

    def _tray_message_clicked(self):
        self.show_window()
        if self.last_notified:
            self.open_channel(self.last_notified)

    def show_window(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        theme.style_window(self)
        self.raise_()
        self.activateWindow()

    def changeEvent(self, e):
        if e.type() == e.Type.ActivationChange and self.isActiveWindow() and self.current:
            ch = self.core.store.channel(self.current)
            if ch and ch["kind"] == "text":
                self.core.mark_read(self.current)
                self.sidebar.update_unread()
        super().changeEvent(e)

    def closeEvent(self, e):
        if not self.quitting and self.s["close_to_tray"] and self.tray:
            e.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(APP_NAME, "Приложение продолжает работать в трее. "
                                      "Выйти можно через меню значка.", app_icon(), 4000)
            return
        self.s["window"] = bytes(self.saveGeometry().toBase64()).decode()
        self.s.save()
        self.core.shutdown()
        if self.tray:
            self.tray.hide()
        e.accept()
        self.app.quit()

    def quit(self):
        self.quitting = True
        self.close()

    def restart(self):
        self.quitting = True
        self.close()
        updater.restart()

    def _check_updates(self):
        def done(res):
            if isinstance(res, dict) and res.get("available"):
                self.banner_text.setText(f"Доступна новая версия {res['latest']}")
                self.banner.show()
                if self.tray and not self.isVisible():
                    self.tray.showMessage(APP_NAME, f"Доступна новая версия {res['latest']}",
                                          app_icon(), 5000)
        self._update_bridge = run_async(updater.check, done)
