"""Settings pages that talk to hardware or the network."""

import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QRadioButton,
                               QSlider, QVBoxLayout, QWidget)

from .. import stream, system, updater, voice
from ..config import CONTROL_PORT, DISCOVERY_PORT, STREAM_PORT, VERSION, VOICE_PORT
from ..net import local_ips
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
    key_btn = button(system.key_name(s["ptt_key"]), "secondary")
    key_btn.setMinimumWidth(200)
    pb.addWidget(label("Клавиша:", "muted"))
    pb.addWidget(key_btn)
    pb.addWidget(slider_row("Задержка отпускания", s["ptt_release_ms"], 0, 1000,
                            lambda x: s.__setitem__("ptt_release_ms", x), " мс"), 1)
    v.addWidget(ptt_box)
    capture = QTimer(key_btn, interval=30)
    deadline = {"n": 0}

    def start_capture():
        key_btn.setText("Нажмите клавишу или кнопку мыши…")
        deadline["n"] = 200
        QTimer.singleShot(250, capture.start)  # let the click itself be released first

    def poll():
        deadline["n"] -= 1
        vk = system.pressed_key()
        if vk or deadline["n"] <= 0:
            capture.stop()
            if vk:
                s["ptt_key"] = vk
                s.save()
            key_btn.setText(system.key_name(s["ptt_key"]))
    capture.timeout.connect(poll)
    key_btn.clicked.connect(start_capture)

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
    v.addWidget(label("Эхоподавления нет: чтобы собеседники не слышали сами себя, используйте "
                      "наушники.", "hint", wrap=True))


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
    s, core, win = view.s, view.core, view.win
    v.addWidget(label("Сеть", "h2"))
    v.addWidget(label("Серверов нет: компьютеры находят друг друга сами — в одной локальной сети или "
                      "в одной сети Radmin VPN. Все, у кого совпадает комната, видят общие каналы.",
                      "muted", wrap=True))

    v.addWidget(section("Комната", "Меняйте, чтобы разделить разные компании друзей в одной сети. "
                        "После смены приложение перезапустится."))
    room = QLineEdit(s["room"])
    room.setMaxLength(32)
    row = QHBoxLayout()
    row.addWidget(room, 1)

    def apply_room():
        name = " ".join(room.text().split())
        if name and name != s["room"]:
            s["room"] = name
            s.save()
            win.restart()
    row.addWidget(button("Сменить", "secondary", apply_room))
    v.addLayout(row)

    v.addWidget(section("Ваши адреса", "Если друзья вас не видят, пусть добавят один из них вручную."))
    for ip in local_ips():
        r = QHBoxLayout()
        lb = QLabel(ip + ("   · Radmin VPN" if ip.startswith("26.") else ""))
        lb.setStyleSheet(f"color: {T.c['header']}; font-family: Consolas; font-size: {T.px(11)}pt;")
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        copy = IconButton("copy", "Скопировать", 18, 30)
        copy.clicked.connect(lambda _=False, x=ip: (QApplication.clipboard().setText(x),
                                                    win.toast(f"{x} скопирован")))
        r.addWidget(lb)
        r.addWidget(copy)
        r.addStretch(1)
        v.addLayout(r)

    v.addWidget(section("Участники рядом"))
    peers = QListWidget()
    peers.setFixedHeight(150)

    def fill_peers():
        peers.clear()
        connected = core.mesh.peers()
        for uid, p in connected.items():
            peers.addItem(QListWidgetItem(icons.icon("signal", T.c["green"], 16),
                                          f"{core.name_of(uid)}  —  {p['ip']}  ·  подключён"))
        for uid, p in core.mesh.discovered().items():
            if uid not in connected:
                peers.addItem(QListWidgetItem(icons.icon("signal", T.c["yellow"], 16),
                                              f"{p['name'] or '?'}  —  {p['ip']}  ·  соединяемся…"))
        if not peers.count():
            peers.addItem("Пока никого не видно")
    fill_peers()
    t = QTimer(peers, interval=1500, timeout=fill_peers)
    t.start()
    v.addWidget(peers)

    v.addWidget(section("Адреса вручную", "Для случаев, когда автоматический поиск не срабатывает "
                        "(например, друг в другой подсети). Формат: 26.12.34.56 или 26.12.34.56:8891"))
    manual = QListWidget()
    manual.setFixedHeight(110)
    for ip in s["peers"]:
        manual.addItem(ip)
    v.addWidget(manual)
    add_row = QHBoxLayout()
    entry = QLineEdit()
    entry.setPlaceholderText("IP-адрес друга")

    def add():
        ip = entry.text().strip()
        if ip and ip not in s["peers"]:
            s["peers"].append(ip)
            s.save()
            manual.addItem(ip)
            entry.clear()

    def remove():
        item = manual.currentItem()
        if item and item.text() in s["peers"]:
            s["peers"].remove(item.text())
            s.save()
            manual.takeItem(manual.row(item))
    entry.returnPressed.connect(add)
    add_row.addWidget(entry, 1)
    add_row.addWidget(button("Добавить", None, add))
    add_row.addWidget(button("Удалить", "secondary", remove))
    v.addLayout(add_row)

    v.addWidget(section("Брандмауэр Windows",
                        f"Приложению нужны входящие порты: TCP {CONTROL_PORT}, UDP {DISCOVERY_PORT}, "
                        f"{VOICE_PORT} (голос), {STREAM_PORT} (трансляция). Windows спросит подтверждение."))

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
    v.addWidget(label("Обновления", "h2"))
    v.addWidget(label(f"Установлена версия {VERSION}. Обновления берутся из репозитория проекта на GitHub.",
                      "muted", wrap=True))
    v.addWidget(switch_row("Проверять при запуске", "Раз в запуск тихо проверять, не вышла ли новая версия.",
                           s["check_updates"], lambda on: (s.__setitem__("check_updates", on), s.save())))
    status = label("", "muted", wrap=True)
    notes = label("", "hint", wrap=True)
    row = QHBoxLayout()
    check = button("Проверить сейчас", "secondary")
    apply_btn = button("Обновить и перезапустить", "success")
    apply_btn.hide()
    row.addWidget(check)
    row.addWidget(apply_btn)
    row.addStretch(1)
    v.addSpacing(16)
    v.addLayout(row)
    v.addSpacing(10)
    v.addWidget(status)
    v.addWidget(notes)

    def checked(res):
        check.setEnabled(True)
        if isinstance(res, Exception):
            status.setText(f"Не удалось проверить: {res}")
            return
        if res["available"]:
            status.setText(f"Доступна версия {res['latest']} (у вас {res['current']}).")
            apply_btn.show()
        else:
            status.setText(f"У вас последняя версия ({res['current']}).")
        if res["notes"]:
            notes.setText("Последние изменения:\n• " + "\n• ".join(res["notes"][:6]))

    def do_check():
        check.setEnabled(False)
        status.setText("Проверяю…")
        view._upd_bridge = run_async(updater.check, checked)

    def applied(res):
        if isinstance(res, Exception):
            apply_btn.setEnabled(True)
            status.setText(f"Не удалось обновить: {res}")
            return
        status.setText(f"Обновлено до {res}. Перезапуск…")
        QTimer.singleShot(600, win.restart)

    def do_apply():
        apply_btn.setEnabled(False)
        status.setText("Скачиваю…")
        view._apply_bridge = run_async(updater.apply, applied)

    check.clicked.connect(do_check)
    apply_btn.clicked.connect(do_apply)
    do_check()
