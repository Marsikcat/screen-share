"""Settings pages that talk to hardware or the network."""

import re
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QRadioButton,
                               QSlider, QVBoxLayout, QWidget)

from .. import hotkeys, stream, system, updater, voice
from ..config import DISCOVERY_PORT, PEER_PORT, VERSION
from . import icons
from .settings import section, switch_row
from .theme import T
from .widgets import IconButton, LevelMeter, button, label


class Bridge(QObject):
    """Deliver results from worker threads to the GUI thread."""
    done = Signal(object)


def run_async(fn, on_done):
    bridge = Bridge()
    bridge.done.connect(on_done)

    def work():
        try:
            result = fn()
        except Exception as e:  # shown to the user as-is
            result = e
        bridge.done.emit(result)
    threading.Thread(target=work, daemon=True).start()
    return bridge


def slider_row(title, value, lo, hi, on_change, suffix="%"):
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    cap = label(f"{title.upper()} · {value}{suffix}", "caption")
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(value)
    s.valueChanged.connect(lambda x: (cap.setText(f"{title.upper()} · {x}{suffix}"), on_change(x)))
    v.addWidget(cap)
    v.addWidget(s)
    return w


def keycaps(keys):
    """'Ctrl+Shift+M' → keys drawn as little caps."""
    c = T.c
    cap = (f"<span style='background-color:{c['rail'] if not T.light else c['active']}; "
           f"color:{c['header']}; font-weight:600'>&nbsp;{{}}&nbsp;</span>")
    groups = []
    for group in keys.split(" / "):
        # split on "+" between keys, but keep a trailing "+" that is itself the key ("Num +")
        groups.append(" ".join(cap.format(k.strip()) for k in re.split(r"\s*\+\s*(?=.)", group)))
    return f"<span style='color:{c['muted']}'> / </span>".join(groups)


class BindButton(QWidget):
    """Shows a hotkey and records a new one on click (Esc cancels, × clears)."""

    def __init__(self, view, action):
        super().__init__()
        self.view, self.action, self.s = view, action, view.s
        self.recorder = hotkeys.Recorder(self)
        self.recorder.captured.connect(self._captured)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self.btn = button("", "secondary", self._record)
        self.btn.setMinimumWidth(230)
        self.clear = IconButton("x", "Убрать сочетание", 16, 30)
        self.clear.clicked.connect(lambda: self._set(None))
        keep = self.clear.sizePolicy()
        keep.setRetainSizeWhenHidden(True)      # buttons line up whether or not × is shown
        self.clear.setSizePolicy(keep)
        h.addWidget(self.btn)
        h.addWidget(self.clear)
        mgr = view.win.hotkeys
        self.destroyed.connect(lambda: setattr(mgr, "paused", False))
        self._refresh()

    def _refresh(self):
        b = self.s["hotkeys"].get(self.action)
        self.btn.setText(hotkeys.describe(b))
        self.btn.setStyleSheet("")
        self.clear.setVisible(bool(b))

    def _record(self):
        self.view.win.hotkeys.paused = True
        self.btn.setText("Нажмите сочетание…  (Esc — отмена)")
        self.btn.setStyleSheet(f"background: {T.c['accent_soft']}; color: {T.c['header']};"
                               f"border: 1px solid {T.c['accent']};")
        self.recorder.start()

    def _captured(self, binding):
        self.view.win.hotkeys.paused = False
        if binding:
            self._set(binding)
        else:
            self._refresh()

    def _set(self, binding):
        hk = self.s["hotkeys"]
        if binding:
            for other, b in hk.items():
                if other != self.action and b == binding:
                    hk[other] = None
                    self.view.win.toast(f"Сочетание снято с «{hotkeys.ACTIONS[other][0]}»")
        hk[self.action] = binding
        self.s.save()
        self._refresh()


# ── hotkeys ─────────────────────────────────────────────────────────
def page_hotkeys(view, v):
    s = view.s
    v.addWidget(label("Горячие клавиши", "h2"))
    v.addWidget(label("Сочетания ниже работают, даже когда окно свёрнуто или вы в игре. Клавиши не "
                      "перехватываются — игра их тоже получит. Можно назначить кнопки мыши 4/5 и "
                      "одиночный модификатор (например, левый Ctrl для рации).", "muted", wrap=True))
    v.addWidget(switch_row("Работать вне окна приложения", "Выключите, если сочетания мешают в других "
                           "программах, — тогда они будут действовать только в окне МойДискорд.",
                           s["hotkeys_global"], lambda on: (s.__setitem__("hotkeys_global", on), s.save())))
    v.addWidget(section("Голосовая связь и демонстрация"))
    for action, (title, hint) in hotkeys.ACTIONS.items():
        row = QFrame()
        row.setStyleSheet(f"QFrame {{ border-bottom: 1px solid {T.c['divider']}; }}"
                          f"QLabel, QPushButton, QToolButton {{ border-bottom: none; }}")
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 10, 0, 10)
        col = QVBoxLayout()
        col.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet(f"color: {T.c['header']}; font-weight: 600;")
        col.addWidget(t)
        if hint:
            col.addWidget(label(hint, "hint", wrap=True))
        h.addLayout(col, 1)
        h.addWidget(BindButton(view, action), 0, Qt.AlignVCenter)
        v.addWidget(row)
    v.addWidget(section("Внутри приложения", "Работают, когда окно МойДискорд активно."))
    grid = QGridLayout()
    grid.setVerticalSpacing(8)
    grid.setHorizontalSpacing(24)
    for i, (keys, what) in enumerate(hotkeys.IN_APP):
        k = QLabel(keycaps(keys))
        k.setTextFormat(Qt.RichText)
        grid.addWidget(k, i, 0)
        grid.addWidget(label(what, wrap=True), i, 1)
    grid.setColumnStretch(1, 1)
    v.addLayout(grid)


# ── voice ───────────────────────────────────────────────────────────
def page_voice(view, v):
    s, core = view.s, view.core
    v.addWidget(label("Голос и звук", "h2"))
    voice.refresh_devices() if not core.voice.in_stream else None
    ins, outs = voice.list_devices()
    grid = QGridLayout()
    grid.setHorizontalSpacing(16)
    grid.setVerticalSpacing(6)
    grid.setContentsMargins(0, 18, 0, 0)

    def combo(items, current, key, restart):
        c = QComboBox()
        c.addItem("По умолчанию (как в Windows)", "")
        for name in items:
            c.addItem(name, name)
        c.setCurrentIndex(max(0, c.findData(current)))

        def changed():
            s[key] = c.currentData()
            s.save()
            restart()
        c.currentIndexChanged.connect(changed)
        return c

    grid.addWidget(label("УСТРОЙСТВО ВВОДА", "caption"), 0, 0)
    grid.addWidget(label("УСТРОЙСТВО ВЫВОДА", "caption"), 0, 1)
    grid.addWidget(combo(ins, s["input_device"], "input_device", core.voice.restart_input), 1, 0)
    grid.addWidget(combo(outs, s["output_device"], "output_device", core.voice.restart_output), 1, 1)
    grid.addWidget(slider_row("Громкость микрофона", s["input_volume"], 0, 200,
                              lambda x: s.__setitem__("input_volume", x)), 2, 0)
    grid.addWidget(slider_row("Громкость звука", s["output_volume"], 0, 200,
                              lambda x: s.__setitem__("output_volume", x)), 2, 1)
    grid.setColumnStretch(0, 1)
    grid.setColumnStretch(1, 1)
    v.addLayout(grid)

    # mic test
    v.addWidget(section("Проверка микрофона", "Скажите что-нибудь — вы услышите себя. "
                        "Полоса показывает уровень, белая метка — порог срабатывания."))
    meter = LevelMeter()
    meter.editable = not s["vad_auto"] and s["input_mode"] == "vad"
    test = button("Проверить", None)
    row = QHBoxLayout()
    row.addWidget(test)
    row.addWidget(meter, 1)
    v.addLayout(row)
    started_here = {"input": False}

    def toggle_test():
        on = not core.voice.monitor
        if on and core.voice.in_stream is None:
            started_here["input"] = core.voice.start_input()
        core.voice.monitor = on
        if not on and started_here["input"] and not core.my_voice:
            core.voice.stop_input()
            started_here["input"] = False
        test.setText("Остановить" if on else "Проверить")
    test.clicked.connect(toggle_test)

    def on_threshold(db):
        s["vad_threshold"] = int(db)
    meter.threshold_changed.connect(on_threshold)
    timer = QTimer(meter, interval=50)
    timer.timeout.connect(lambda: meter.set_values(core.voice.level, core.voice.threshold
                                                   if s["vad_auto"] else s["vad_threshold"]))
    timer.start()
    meter.destroyed.connect(lambda: (setattr(core.voice, "monitor", False),
                                     started_here["input"] and not core.my_voice and core.voice.stop_input()))

    # input mode
    v.addWidget(section("Режим ввода"))
    group = QButtonGroup(v.parentWidget())
    vad = QRadioButton("Голосовая активность — микрофон включается, когда вы говорите")
    ptt = QRadioButton("Режим рации — говорите, пока держите клавишу (работает в любом окне и игре)")
    vad.setChecked(s["input_mode"] == "vad")
    ptt.setChecked(s["input_mode"] == "ptt")
    group.addButton(vad)
    group.addButton(ptt)
    v.addWidget(vad)
    v.addWidget(ptt)

    ptt_box = QWidget()
    pb = QHBoxLayout(ptt_box)
    pb.setContentsMargins(28, 6, 0, 0)
    pb.addWidget(label("Клавиша:", "muted"))
    pb.addWidget(BindButton(view, "ptt"))
    pb.addWidget(slider_row("Задержка отпускания", s["ptt_release_ms"], 0, 1000,
                            lambda x: s.__setitem__("ptt_release_ms", x), " мс"), 1)
    v.addWidget(ptt_box)

    vad_box = QWidget()
    vb = QVBoxLayout(vad_box)
    vb.setContentsMargins(0, 0, 0, 0)
    vb.addWidget(switch_row("Определять чувствительность автоматически",
                            "Порог подстраивается под фоновый шум. Выключите, чтобы задать его вручную "
                            "перетаскиванием белой метки на полосе выше.", s["vad_auto"],
                            lambda on: (s.__setitem__("vad_auto", on), s.save(),
                                        setattr(meter, "editable", not on))))
    v.addWidget(vad_box)

    def mode_changed():
        s["input_mode"] = "ptt" if ptt.isChecked() else "vad"
        s.save()
        ptt_box.setVisible(ptt.isChecked())
        vad_box.setVisible(vad.isChecked())
        meter.editable = vad.isChecked() and not s["vad_auto"]
    vad.toggled.connect(mode_changed)
    mode_changed()

    # WebRTC audio processing — the same echo canceller / denoiser as Chrome and Discord
    v.addWidget(section("Обработка звука", "Работает на вашей стороне, до отправки: собеседники слышат "
                        "уже очищенный голос."))

    def proc(key):
        def apply(on):
            s[key] = on
            s.save()
            core.voice.rebuild_processing()
        return apply
    if not core.voice.processing_available():
        v.addWidget(label("Модуль pywebrtc-audio не установлен — запустите setup.bat.", "hint", wrap=True))
    v.addWidget(switch_row("Эхоподавление", "Убирает из микрофона звук ваших колонок — можно говорить "
                           "без наушников.", s["aec"], proc("aec")))
    v.addWidget(switch_row("Шумоподавление", "Гасит шум вентилятора, клавиатуры и улицы.", s["ns"], proc("ns")))
    level_row = QHBoxLayout()
    level_row.addWidget(label("Сила шумоподавления", "muted"))
    level = QComboBox()
    for i, name in enumerate(("Слабое", "Среднее", "Сильное", "Очень сильное")):
        level.addItem(name, i)
    level.setCurrentIndex(int(s["ns_level"]))

    def level_changed():
        s["ns_level"] = level.currentData()
        s.save()
        core.voice.rebuild_processing()
    level.currentIndexChanged.connect(level_changed)
    level_row.addWidget(level)
    level_row.addStretch(1)
    v.addLayout(level_row)
    v.addWidget(switch_row("Автоматическая громкость", "Выравнивает громкость: тихий голос подтягивает, "
                           "крик приглушает.", s["agc"], proc("agc")))


# ── stream ──────────────────────────────────────────────────────────
def page_stream(view, v):
    s = view.s
    v.addWidget(label("Демонстрация экрана", "h2"))
    v.addWidget(label("Запускается кнопкой «Демонстрация экрана» в голосовом канале. Зрители открывают "
                      "трансляцию в отдельном окне — двойной клик или клавиша F разворачивает его на весь "
                      "экран.", "muted", wrap=True))
    grid = QGridLayout()
    grid.setContentsMargins(0, 18, 0, 0)
    grid.setHorizontalSpacing(16)
    grid.setVerticalSpacing(6)
    quality = QComboBox()
    for key, q in stream.QUALITY.items():
        quality.addItem(f"{q['label']}  ·  до {q['kbps'] / 1000:g} Мбит/с", key)
    quality.setCurrentIndex(max(0, quality.findData(s["stream_quality"])))
    quality.currentIndexChanged.connect(lambda: (s.__setitem__("stream_quality", quality.currentData()),
                                                 s.save()))
    enc = QComboBox()
    nv = stream.has_nvenc()
    enc.addItem(f"Автоматически ({'NVENC найден' if nv else 'NVENC нет — процессор'})", "auto")
    enc.addItem("NVIDIA NVENC (видеокарта)", "nvenc")
    enc.addItem("Процессор (x264)", "cpu")
    enc.setCurrentIndex(max(0, enc.findData(s["stream_encoder"])))
    enc.currentIndexChanged.connect(lambda: (s.__setitem__("stream_encoder", enc.currentData()), s.save()))
    audio = QComboBox()
    audio.addItem("Без звука", "")
    for name in stream.list_audio_devices():
        audio.addItem(name, name)
    audio.setCurrentIndex(max(0, audio.findData(s["stream_audio"])))
    audio.currentIndexChanged.connect(lambda: (s.__setitem__("stream_audio", audio.currentData()), s.save()))
    grid.addWidget(label("КАЧЕСТВО ПО УМОЛЧАНИЮ", "caption"), 0, 0)
    grid.addWidget(label("КОДИРОВЩИК", "caption"), 0, 1)
    grid.addWidget(quality, 1, 0)
    grid.addWidget(enc, 1, 1)
    grid.addWidget(label("ЗВУК ТРАНСЛЯЦИИ", "caption"), 2, 0)
    grid.addWidget(audio, 3, 0, 1, 2)
    grid.setColumnStretch(0, 1)
    grid.setColumnStretch(1, 1)
    v.addLayout(grid)
    v.addSpacing(8)
    v.addWidget(label("Чтобы друзья слышали звук игры или видео, выберите «Стерео микшер» "
                      "(включается в Панели управления → Звук → Запись) или виртуальный кабель VB-Cable.",
                      "hint", wrap=True))


# ── network ─────────────────────────────────────────────────────────
def page_network(view, v):
    from .dialogs import InviteDialog, JoinDialog
    s, core, win = view.s, view.core, view.win
    c = T.c
    v.addWidget(label("Сеть", "h2"))
    v.addWidget(label("Серверов нет: друзья соединяются напрямую — в локальной сети, через Radmin VPN или "
                      "через интернет. Всё зашифровано и подписано. Если напрямую пробиться не удаётся, "
                      "трафик идёт через ретранслятор n0 — он видит только зашифрованные данные.",
                      "muted", wrap=True))

    v.addWidget(section("Комната"))
    closed = s.room_closed()
    v.addWidget(label(f"«{core.room_name()}» — " + ("закрытая: войти можно только по приглашению."
                      if closed else "открытая: в неё попадает каждый в вашей сети, кто введёт то же "
                      "название, а через интернет — по приглашению."), None, wrap=True))
    row = QHBoxLayout()
    row.addWidget(button("Пригласить друга", None, lambda: InviteDialog(win, core).exec()))
    row.addWidget(button("Присоединиться по коду", "secondary", lambda: JoinDialog(win, core).exec()))

    def new_closed():
        from .dialogs import ask_text
        name = ask_text(win, "Закрытая комната", "Название", placeholder="Например, Команда мечты",
                        ok="Создать", text="В неё можно попасть только по вашему приглашению. "
                        "Приложение перезапустится.")
        if name:
            core.create_closed_room(name)
            win.restart()
    row.addWidget(button("Создать закрытую комнату", "secondary", new_closed))
    row.addStretch(1)
    v.addSpacing(6)
    v.addLayout(row)
    v.addWidget(section("Открытая комната по названию", "Для компании в одной локальной сети или Radmin VPN. "
                        "После смены приложение перезапустится."))
    room = QLineEdit(s["room"])
    room.setMaxLength(32)
    rrow = QHBoxLayout()
    rrow.addWidget(room, 1)

    def apply_room():
        name = " ".join(room.text().split())
        if name and (name != s["room"] or s["room_secret"]):
            s["room"], s["room_secret"] = name, ""
            s.save()
            win.restart()
    rrow.addWidget(button("Перейти", "secondary", apply_room))
    v.addLayout(rrow)

    v.addWidget(section("Режим сети", "После смены приложение перезапустится."))
    group = QButtonGroup(v.parentWidget())
    modes = (("internet", "Интернет и локальная сеть — пробивать NAT, при необходимости через ретранслятор"),
             ("lan", "Только локальная сеть / Radmin VPN — никаких внешних сервисов"))
    for key, text in modes:
        rb = QRadioButton(text)
        rb.setChecked(s["network_mode"] == key)
        group.addButton(rb)
        rb.toggled.connect(lambda on, k=key: on and k != s["network_mode"] and
                           (s.__setitem__("network_mode", k), s.save(), win.restart()))
        v.addWidget(rb)

    v.addWidget(section("Участники"))
    peers = QListWidget()
    peers.setFixedHeight(170)

    def fill_peers():
        current = peers.currentItem().data(Qt.UserRole) if peers.currentItem() else None
        peers.clear()
        connected = core.mesh.peers()
        rows = []
        for uid, p in connected.items():
            how = "через ретранслятор" if p.get("via_relay") else "напрямую"
            rtt = f", {p['rtt']} мс" if p.get("rtt") is not None else ""
            rows.append((uid, "signal", c["green"], f"{core.name_of(uid)}  ·  {how}{rtt}"))
        for uid, p in core.mesh.discovered().items():
            if uid not in connected:
                rows.append((uid, "signal", c["yellow"], f"{p['name'] or '?'}  ·  в локальной сети, соединяемся…"))
        room = s.room_id()
        for uid, p in (s["known_peers"] or {}).items():
            if uid not in connected and p.get("room") == room:
                rows.append((uid, "signal", c["muted"], f"{p.get('name') or '?'}  ·  не в сети"))
        for uid, icon_name, color, text in rows:
            item = QListWidgetItem(icons.icon(icon_name, color, 16), text)
            item.setData(Qt.UserRole, uid)
            peers.addItem(item)
            if uid == current:
                peers.setCurrentItem(item)
        if not rows:
            peers.addItem("Пока никого — пригласите друга")
    fill_peers()
    QTimer(peers, interval=1500, timeout=fill_peers).start()
    v.addWidget(peers)

    def forget():
        item = peers.currentItem()
        uid = item.data(Qt.UserRole) if item else None
        if uid:
            core.forget_peer(uid)
            fill_peers()
    frow = QHBoxLayout()
    frow.addWidget(button("Забыть выбранного", "secondary", forget))
    frow.addStretch(1)
    v.addLayout(frow)

    v.addWidget(section("Ваш ID", "Публичный ключ: по нему вас находят и по нему проверяют подпись "
                        "ваших сообщений."))
    idrow = QHBoxLayout()
    ident = QLabel(s.uid[:16] + "…" + s.uid[-8:])
    ident.setStyleSheet(f"color: {c['header']}; font-family: Consolas; font-size: {T.px(11)}pt;")
    copy = IconButton("copy", "Скопировать полностью", 18, 30)
    copy.clicked.connect(lambda: (QApplication.clipboard().setText(s.uid), win.toast("ID скопирован")))
    idrow.addWidget(ident)
    idrow.addWidget(copy)
    idrow.addStretch(1)
    v.addLayout(idrow)

    v.addWidget(section("Брандмауэр Windows",
                        f"Для прямых соединений нужны входящие UDP {DISCOVERY_PORT} (поиск в локальной сети) и "
                        f"{PEER_PORT} (всё остальное). Без них тоже работает, но чаще через ретранслятор."))

    def firewall():
        fw.setEnabled(False)
        fw.setText("Ждём подтверждения Windows…")

        def done(ok):
            fw.setEnabled(True)
            fw.setText("Открыть порты в брандмауэре")
            win.toast("Порты открыты" if ok is True else "Правила не добавлены", "info" if ok is True else "error")
        view._fw_bridge = run_async(system.open_firewall, done)
    fw = button("Открыть порты в брандмауэре", "secondary", firewall)
    v.addWidget(fw, 0, Qt.AlignLeft)


# ── updates ─────────────────────────────────────────────────────────
def page_updates(view, v):
    s, win = view.s, view.win
    c = T.c
    v.addWidget(label("Обновления", "h2"))
    v.addWidget(label(f"Установлена версия {VERSION}. Новые версии выходят как релизы на GitHub — "
                      f"с описанием изменений и готовым архивом.", "muted", wrap=True))
    v.addWidget(switch_row("Проверять при запуске", "Тихо проверять, не вышел ли новый релиз, и показывать "
                           "полоску сверху, если вышел.", s["check_updates"],
                           lambda on: (s.__setitem__("check_updates", on), s.save())))
    row = QHBoxLayout()
    check = button("Проверить сейчас", "secondary")
    apply_btn = button("Обновить", "success")
    apply_btn.hide()
    releases = button("Все релизы", "link",
                      lambda: __import__("webbrowser").open(updater.RELEASES_PAGE))
    row.addWidget(check)
    row.addWidget(apply_btn)
    row.addStretch(1)
    row.addWidget(releases)
    v.addSpacing(16)
    v.addLayout(row)
    v.addSpacing(10)
    status = label("", "muted", wrap=True)
    v.addWidget(status)
    last = {}                   # the latest check result: what «Обновить» installs

    card = QFrame()
    card.setStyleSheet(f"QFrame {{ background: {c['side']}; border-radius: 8px; }}")
    cl = QVBoxLayout(card)
    cl.setContentsMargins(18, 14, 18, 16)
    cl.setSpacing(6)
    head = QHBoxLayout()
    title = QLabel()
    title.setStyleSheet(f"color: {c['header']}; font-weight: 700; font-size: {T.px(12)}pt;")
    badge = QLabel()
    date = label("", "hint")
    head.addWidget(title)
    head.addWidget(badge)
    head.addStretch(1)
    head.addWidget(date)
    cl.addLayout(head)
    notes = QLabel()
    notes.setTextFormat(Qt.MarkdownText)
    notes.setWordWrap(True)
    notes.setOpenExternalLinks(True)
    notes.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
    cl.addWidget(notes)
    open_page = button("Открыть релиз на GitHub", "link",
                       lambda: __import__("webbrowser").open(last.get("url", updater.RELEASES_PAGE)))
    cl.addWidget(open_page, 0, Qt.AlignLeft)
    card.hide()
    v.addSpacing(8)
    v.addWidget(card)

    def checked(res):
        check.setEnabled(True)
        if isinstance(res, Exception):
            status.setText(f"Не удалось проверить: {res}")
            return
        last.clear()
        last.update(res)
        if res["available"]:
            status.setText(f"Вышла версия {res['latest']} — у вас {res['current']}.")
            apply_btn.setText(f"Обновить до {res['latest']}")
            apply_btn.show()
            badge.setText("НОВАЯ")
            badge.setStyleSheet(f"background: {c['green']}; color: white; border-radius: 4px;"
                                f"font-size: {T.px(7)}pt; font-weight: 800; padding: 1px 6px;")
        else:
            status.setText(f"У вас последняя версия ({res['current']}).")
            apply_btn.hide()
            badge.setText("УСТАНОВЛЕНА")
            badge.setStyleSheet(f"background: {c['active']}; color: {c['text']}; border-radius: 4px;"
                                f"font-size: {T.px(7)}pt; font-weight: 800; padding: 1px 6px;")
        title.setText(res["title"])
        date.setText(".".join(reversed(res["date"].split("-"))) if res["date"] else "")
        notes.setText(res["notes"] or "_Описание не загрузилось — откройте релиз на GitHub._")
        card.show()

    def do_check():
        check.setEnabled(False)
        status.setText("Проверяю…")
        view._upd_bridge = run_async(updater.check, checked)

    def applied(res):
        if isinstance(res, Exception):
            apply_btn.setEnabled(True)
            status.setText(f"Не удалось обновить: {res}")
            return
        status.setText(f"Установлена версия {res}. Перезапуск…")
        QTimer.singleShot(600, win.restart)

    def do_apply():
        apply_btn.setEnabled(False)
        status.setText(f"Скачиваю МойДискорд {last.get('latest', '')}…")
        info = dict(last)
        view._apply_bridge = run_async(lambda: updater.apply(info or None), applied)

    check.clicked.connect(do_check)
    apply_btn.clicked.connect(do_apply)
    do_check()
