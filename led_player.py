# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Salt1145
# https://github.com/Salt1145/Busled-subtitle-player
"""
led_player.py —— LED 字幕视频播放器

干什么的：
    放视频，同时按视频的**真实播放进度**把字幕切好、发到 LED 点阵屏（串口）。
    暂停、快进、拖进度条、变速，字幕都会自动跟上（时钟取自 ffplay 报的播放位置）。

打开后的流程（不会一打开就播）：
    1. 先出配置窗口：选视频、字幕、串口、波特率、倒计时秒数、分段策略……
    2. 点「开始播放」-> 倒计时 N 秒 -> 视频开始，字幕同步发到屏幕
    3. 播放界面里有：暂停/继续、±10 秒、全屏、重播、屏幕 24 槽位预览、发送日志、手动发一条

视频用本机已装的 ffplay 播放（这个 mp4 是 HEVC + PCM 音轨，浏览器放不了），
字幕时间轴来自 led_screen.py（24 槽位分段规则）。

命令行：
    python led_player.py
    python led_player.py --dry-run            不连串口，只在界面里看要发什么
    python led_player.py --com COM9 --baud 19200 --countdown 5
"""

from __future__ import annotations

import configparser
import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from led_screen import (DEFAULT_BAUD, DEFAULT_MIN_SEG_MS, DEFAULT_TYPE_STEP_MS,
                        FULLWIDTH_WIDTH, LEDScreen, SubtitleEngine, analyze_subtitles,
                        build_events, find_subtitle, find_video, fit_slots, fw_to_slots,
                        list_ports, load_subtitles, merge_incremental, parse_time_ms,
                        resolve_offset, sanitize, shift_cues, slots_to_fw, text_slots)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INI_PATH = os.path.join(SCRIPT_DIR, "led_player.ini")

APP_TITLE = "LED 字幕播放器"
FFPLAY_TITLE = "LEDSUB_FFPLAY"

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
VK_SPACE, VK_LEFT, VK_RIGHT, VK_F, VK_Q = 0x20, 0x25, 0x27, 0x46, 0x51
MAPVK_VK_TO_VSC = 0
# 扩展键（方向键、小键盘等），发按键消息时要带第 24 位
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
                0x2D, 0x2E, 0x6F, 0x90, 0xA3, 0xA5}

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".ts", ".wmv", ".m4v")
SUB_EXTS = (".srt", ".lrc", ".ass", ".ssa")
SIZE_CHOICES = ("640x360", "960x540", "1280x720", "1600x900", "1920x1080")
BAUD_CHOICES = ("4800", "9600", "19200", "38400", "57600", "115200")

# 打字机模式：界面上用中文，内部用 off/auto/always
TYPE_MODE_LABELS = ("关", "自动", "总是")
TYPE_LABEL_TO_MODE = {"关": "off", "自动": "auto", "总是": "always"}
TYPE_MODE_TO_LABEL = {v: k for k, v in TYPE_LABEL_TO_MODE.items()}
# 老配置文件里写的是 0/1，兼容一下
TYPE_MODE_TO_LABEL.update({"0": "关", "false": "关", "1": "自动", "true": "自动", "on": "自动"})


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def strip_ansi(s):
    out = []
    i = 0
    while i < len(s):
        if s[i] == "\x1b":
            j = i + 1
            while j < len(s) and not s[j].isalpha():
                j += 1
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def fmt_time(ms):
    if ms is None or ms < 0:
        ms = 0
    s = ms / 1000.0
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    return "%d:%02d:%02d" % (h, m, sec) if h else "%d:%02d" % (m, sec)


def fmt_offset(ms, signed=False):
    """偏移量写成 -1:32:35.2 这种看得懂的样子"""
    sign = "-" if ms < 0 else ("+" if signed else "")
    ms = abs(int(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, _ = divmod(rem, 1000)
    if h:
        return "%s%d:%02d:%02d" % (sign, h, m, s)
    return "%s%d:%02d" % (sign, m, s)


def find_ffplay():
    p = shutil.which("ffplay")
    if p:
        return p
    for cand in (r"C:\ffmpeg\bin\ffplay.exe",
                 os.path.expanduser(r"~\scoop\shims\ffplay.exe")):
        if os.path.exists(cand):
            return cand
    return None


def find_ffprobe():
    return shutil.which("ffprobe")


def dpi_scale():
    """
    本机缩放比例（这台机器是 4K + 200%，就是 2.0）。
    ffplay 是按物理像素解释 -x/-y 的，所以要乘上去，
    不然在缩放屏上视频窗口会缩成一小块。
    """
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 90)   # LOGPIXELSY
        ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def list_files(exts):
    """列出程序目录（含 examples/）里的媒体文件"""
    files = []
    for folder, prefix in ((SCRIPT_DIR, ""), (os.path.join(SCRIPT_DIR, "examples"), "examples/")):
        try:
            names = [f for f in os.listdir(folder)
                     if os.path.splitext(f)[1].lower() in exts]
        except OSError:
            continue
        names.sort()
        files += [prefix + f for f in names]
    return files


def pick_font(root, families):
    """挑一个中英文等宽、且中文正好是英文两倍宽的字体（用于 24 槽位预览）"""
    try:
        available = set(tk.font.families(root)) if hasattr(tk, "font") else set()
    except Exception:
        available = set()
    if not available:
        try:
            from tkinter import font as tkfont
            available = set(tkfont.families(root))
        except Exception:
            available = set()
    for name in families:
        if name in available:
            return name
    return "TkFixedFont"


# --------------------------------------------------------------------------
# ffplay 控制 + 播放时钟
# --------------------------------------------------------------------------

class FfplayController:
    """
    起一个 ffplay 放视频，然后：
      * 从它的 stderr 里读播放位置（ffplay 每秒刷 30 次状态行，第一个数字就是主时钟）
      * 用 PostMessage 给它发按键（空格=暂停，左右=±10 秒，f=全屏）
    位置读不到也无所谓，界面会显示"未播放"。
    """

    def __init__(self, ffplay, video, size="1280x720", fullscreen=False, log=None,
                 scale=1.0):
        self.ffplay = ffplay
        self.video = video
        self.size = size                 # 逻辑尺寸（会乘缩放比例再交给 ffplay）
        self.scale = scale or 1.0
        self.fullscreen = fullscreen
        self.extra_args = []           # 额外 ffplay 参数（自检脚本会塞 -nodisp 做无声测试）
        self.log = log or (lambda m: None)
        self.proc = None
        self.hwnd = 0
        self.clock_ms = 0.0
        self.clock_wall = 0.0          # 收到上面这个位置的时刻（monotonic）
        self.last_change = 0.0         # 位置最后一次变化的时刻
        self.started_wall = 0.0
        self.start_offset_ms = 0
        self.returncode = None
        self._lock = threading.Lock()

    # ---- 生命周期 ----
    def start(self, start_ms=0):
        self.stop()
        w, h = (self.size.split("x") + ["720"])[:2]
        cmd = [self.ffplay, "-hide_banner", "-stats", "-autoexit",
               "-window_title", FFPLAY_TITLE,
               "-x", str(int(int(w) * self.scale)), "-y", str(int(int(h) * self.scale))]
        if self.fullscreen:
            cmd.append("-fs")
        if start_ms and start_ms > 0:
            cmd += ["-ss", "%.3f" % (start_ms / 1000.0)]
        cmd += list(self.extra_args)
        cmd += ["-i", self.video]
        self.start_offset_ms = int(start_ms or 0)
        self.clock_ms = float(start_ms or 0)
        self.clock_wall = time.monotonic()
        self.last_change = self.clock_wall
        self.started_wall = self.clock_wall
        self.returncode = None
        self.hwnd = 0
        self.proc = subprocess.Popen(cmd, stderr=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        self.log("启动视频: %s (从 %s 开始)" % (os.path.basename(self.video), fmt_time(start_ms)))
        threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._grab_window, args=(self.proc,), daemon=True).start()

    def stop(self):
        p = self.proc
        self.proc = None
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
        self.hwnd = 0

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def finished(self):
        return self.proc is not None and self.proc.poll() is not None

    # ---- stderr -> 播放位置 ----
    def _read_stderr(self, proc):
        buf = b""
        try:
            while True:
                ch = proc.stderr.read(1)
                if not ch:
                    break
                if ch in (b"\r", b"\n"):
                    line = buf.strip()
                    buf = b""
                    if not line:
                        continue
                    s = strip_ansi(line.decode("utf-8", "replace")).strip()
                    if not s:
                        continue
                    tok = s.split(" ")[0].split("\t")[0]
                    try:
                        val = float(tok)
                    except ValueError:
                        continue
                    if val != val:          # nan
                        continue
                    with self._lock:
                        ms = max(0.0, val * 1000.0)
                        if abs(ms - self.clock_ms) > 0.5:
                            self.last_change = time.monotonic()
                        self.clock_ms = ms
                        self.clock_wall = time.monotonic()
                else:
                    buf += ch
        except Exception:
            pass

    # ---- 找窗口 & 发按键 ----
    def _grab_window(self, proc, tries=60):
        u32 = ctypes.windll.user32
        u32.FindWindowW.restype = ctypes.c_void_p
        for _ in range(tries):
            if proc.poll() is not None:
                return
            h = u32.FindWindowW(None, FFPLAY_TITLE)
            if h:
                self.hwnd = h
                try:
                    u32.SetForegroundWindow(ctypes.c_void_p(h))
                except Exception:
                    pass
                return
            time.sleep(0.1)

    def focus(self):
        """把视频窗口顶到前面（收掉全屏黑屏之后用）"""
        u32 = ctypes.windll.user32
        u32.FindWindowW.restype = ctypes.c_void_p
        h = self.hwnd or u32.FindWindowW(None, FFPLAY_TITLE)
        if not h:
            return False
        self.hwnd = h
        try:
            u32.SetForegroundWindow(ctypes.c_void_p(h))
        except Exception:
            return False
        return True

    def send_key(self, vk):
        if not self.running():
            return False
        u32 = ctypes.windll.user32
        u32.FindWindowW.restype = ctypes.c_void_p
        u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                     ctypes.c_ulonglong, ctypes.c_longlong]
        if not self.hwnd:
            self.hwnd = u32.FindWindowW(None, FFPLAY_TITLE) or 0
        if not self.hwnd:
            return False
        scan = u32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        # 方向键/小键盘等是"扩展键"，lParam 第 24 位必须置 1，
        # 否则 SDL 会当成小键盘数字，ffplay 就不认（实测：不带这一位左右键无效）
        ext = 0x01000000 if vk in EXTENDED_VKS else 0
        lp = (scan << 16) | 1 | ext
        u32.PostMessageW(ctypes.c_void_p(self.hwnd), WM_KEYDOWN, vk, lp)
        time.sleep(0.03)
        u32.PostMessageW(ctypes.c_void_p(self.hwnd), WM_KEYUP, vk, lp | 0xC0000000)
        return True

    # ---- 时钟查询 ----
    def position_ms(self):
        """当前播放位置（带一点线性外推，让字幕卡点更准）"""
        with self._lock:
            ms, wall, changed = self.clock_ms, self.clock_wall, self.last_change
        if not self.running():
            return ms, True
        now = time.monotonic()
        dt = now - wall
        if dt > 0.5:                     # 太久没更新，别外推
            dt = 0.0
        paused = (now - changed) > 0.8
        return ms + (0 if paused else dt * 1000.0), paused


# --------------------------------------------------------------------------
# 全屏黑屏倒计时（配合「视频全屏开始」用）
# --------------------------------------------------------------------------

class CountdownOverlay:
    """
    整屏黑底 + 大数字倒计时。
    视频全屏开始时用它顶住屏幕，免得先露出桌面、再"啪"地跳出视频窗口；
    倒完不马上关，等 ffplay 的窗口真的出来了再关（这样中间不会闪一下桌面）。
    """

    def __init__(self, root, on_cancel=None):
        self.root = root
        self.on_cancel = on_cancel
        self.win = None
        self.lbl_num = None
        self.lbl_hint = None

    def _create(self):
        w = tk.Toplevel(self.root)
        w.configure(bg="black")
        w.title("倒计时")
        try:
            w.attributes("-fullscreen", True)
        except tk.TclError:
            w.overrideredirect(True)
            w.geometry("%dx%d+0+0" % (w.winfo_screenwidth(), w.winfo_screenheight()))
        try:
            w.attributes("-topmost", True)
        except tk.TclError:
            pass
        sh = max(480, w.winfo_screenheight())
        self.lbl_num = tk.Label(w, text="", fg="#ffffff", bg="black",
                                font=("Microsoft YaHei UI", -max(80, sh // 4), "bold"))
        self.lbl_num.pack(expand=True)
        self.lbl_hint = tk.Label(w, text="按 Esc 取消", fg="#777777", bg="black",
                                 font=("Microsoft YaHei UI", -max(14, sh // 45)))
        self.lbl_hint.pack(side="bottom", pady=max(20, sh // 16))
        w.bind("<Escape>", lambda _e: self.on_cancel and self.on_cancel())
        self.win = w
        try:
            w.focus_force()
        except tk.TclError:
            pass

    def show(self, text, hint=None):
        if self.win is None or not self.win.winfo_exists():
            self._create()
        self.lbl_num.config(text=text)
        if hint is not None:
            self.lbl_hint.config(text=hint)
        self.win.deiconify()
        self.win.lift()

    def close(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None
            self.lbl_num = None
            self.lbl_hint = None

    @property
    def visible(self):
        return self.win is not None


# --------------------------------------------------------------------------
# 主界面
# --------------------------------------------------------------------------

class LedPlayerApp:
    def __init__(self, root, argv=None):
        self.root = root
        self.cfg = configparser.ConfigParser()
        self.load_cfg()

        self.cfg_values = self.read_cfg()
        overrides = self.parse_args(argv or [])
        self.cfg_values.update({k: v for k, v in overrides.items() if v is not None})

        self.ffplay = find_ffplay()
        self.ffprobe = find_ffprobe()
        self.screen = None
        self.engine = None
        self.ff = None
        self.cues = []
        self.cues_raw = []              # 没加偏移的原始字幕，改偏移时从它重算
        self.events = []
        self.duration_ms = 0
        self.send_count = 0
        self.loading = False
        self.countdown_job = None
        self.stopped = False
        self._tick_running = False
        self.overlay = None            # 全屏黑屏倒计时
        self.overlay_wait = None

        root.title(APP_TITLE)
        self.ui_scale = dpi_scale()          # 注意：进度条已经叫 self.scale 了
        root.geometry("%dx%d" % (int(880 * self.ui_scale), int(680 * self.ui_scale)))
        root.minsize(int(820 * self.ui_scale), int(620 * self.ui_scale))
        self.font_mono = pick_font(root, ["NSimSun", "SimSun", "MS Gothic",
                                          "Sarasa Mono SC", "Consolas", "Courier New"])

        self.build_config_view()
        self.build_play_view()
        self.show_config()

    # ---------------- 配置读写 ----------------
    def read_cfg(self):
        c = self.cfg
        d = {}
        if c.has_section("player"):
            p = c["player"]
            d["video"] = p.get("video", "") or ""
            d["subtitle"] = p.get("subtitle", "") or ""
            d["com"] = p.get("com", "COM8")
            d["baud"] = p.get("baud", str(DEFAULT_BAUD))
            d["countdown"] = p.get("countdown", "3")
            d["size"] = p.get("size", "1280x720")
            d["mode"] = p.get("mode", "auto")
            d["min_seg"] = p.get("min_seg", str(DEFAULT_MIN_SEG_MS))
            d["width"] = p.get("width", str(FULLWIDTH_WIDTH))
            d["dry_run"] = p.get("dry_run", "0")
            d["fullscreen"] = p.get("fullscreen", "0")
            d["start"] = p.get("start", "0")
            d["typewriter"] = p.get("typewriter", "off")
            d["type_step"] = p.get("type_step", str(DEFAULT_TYPE_STEP_MS))
            d["marquee"] = p.get("marquee", "1")
            d["width_fw"] = p.get("width_fw", "") or str(
                slots_to_fw(p.get("width", FULLWIDTH_WIDTH) or FULLWIDTH_WIDTH))
            d["offset"] = p.get("offset", "0")
            d["merge_inc"] = p.get("merge_inc", "0")
        return d

    def load_cfg(self):
        try:
            self.cfg.read(INI_PATH, encoding="utf-8")
        except Exception:
            pass

    def save_cfg(self):
        self.cfg["player"] = {k: str(v) for k, v in self.cfg_values.items()}
        try:
            with open(INI_PATH, "w", encoding="utf-8") as f:
                self.cfg.write(f)
        except OSError:
            pass

    def parse_args(self, argv):
        out = {}
        i = 0
        while i < len(argv):
            a = argv[i]
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if a == "--com" and nxt:
                out["com"] = nxt; i += 2; continue
            if a == "--baud" and nxt:
                out["baud"] = nxt; i += 2; continue
            if a == "--countdown" and nxt:
                out["countdown"] = nxt; i += 2; continue
            if a == "--video" and nxt:
                out["video"] = nxt; i += 2; continue
            if a == "--sub" and nxt:
                out["subtitle"] = nxt; i += 2; continue
            if a == "--dry-run":
                out["dry_run"] = "1"; i += 1; continue
            if a == "--mode" and nxt:
                out["mode"] = nxt; i += 2; continue
            if a == "--autostart":
                out["autostart"] = "1"; i += 1; continue
            i += 1
        return out

    # ---------------- 配置界面 ----------------
    def build_config_view(self):
        v = self.cfg_values
        f = ttk.Frame(self.root, padding=14)
        self.config_frame = f
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="LED 字幕播放器", font=("Microsoft YaHei UI", 16, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        ttk.Label(f, text="屏幕宽度 24 槽位（全角 12 字 / 半角 24 字）：超长自动按时间轴分段，"
                          "时间不够则截断到 24 槽。",
                  foreground="#555").grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        row = 2
        # 视频
        ttk.Label(f, text="视频文件").grid(row=row, column=0, sticky="w", pady=4)
        self.var_video = tk.StringVar(value=v.get("video") or "")
        vids = list_files(VIDEO_EXTS)
        cb = ttk.Combobox(f, textvariable=self.var_video, values=vids)
        cb.grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(f, text="浏览…", command=self.browse_video).grid(row=row, column=2, padx=6)
        row += 1

        # 字幕
        ttk.Label(f, text="字幕文件").grid(row=row, column=0, sticky="w", pady=4)
        self.var_sub = tk.StringVar(value=v.get("subtitle") or "")
        subs = list_files(SUB_EXTS)
        ttk.Combobox(f, textvariable=self.var_sub, values=subs).grid(
            row=row, column=1, sticky="ew", pady=4)
        ttk.Button(f, text="浏览…", command=self.browse_sub).grid(row=row, column=2, padx=6)
        row += 1

        ttk.Separator(f, orient="horizontal").grid(row=row, column=0, columnspan=3,
                                                   sticky="ew", pady=10)
        row += 1

        # 串口
        ttk.Label(f, text="串口 (COM)").grid(row=row, column=0, sticky="w", pady=4)
        port_frame = ttk.Frame(f)
        port_frame.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        ports = [p for p, _ in list_ports()]
        default_port = v.get("com") or "COM8"
        if default_port not in ports and ports:
            default_port = "COM9" if "COM9" in ports else ports[0]
        self.var_com = tk.StringVar(value=default_port)
        self.combo_com = ttk.Combobox(port_frame, textvariable=self.var_com, values=ports, width=12)
        self.combo_com.pack(side="left")
        ttk.Button(port_frame, text="刷新串口", command=self.refresh_ports).pack(side="left", padx=6)
        self.lbl_ports = ttk.Label(port_frame, text="", foreground="#666")
        self.lbl_ports.pack(side="left")
        row += 1

        # 波特率
        ttk.Label(f, text="波特率").grid(row=row, column=0, sticky="w", pady=4)
        self.var_baud = tk.StringVar(value=v.get("baud") or str(DEFAULT_BAUD))
        ttk.Combobox(f, textvariable=self.var_baud, values=BAUD_CHOICES, width=12).grid(
            row=row, column=1, sticky="w", pady=4)
        row += 1

        # 倒计时
        ttk.Label(f, text="倒计时（秒）").grid(row=row, column=0, sticky="w", pady=4)
        self.var_countdown = tk.StringVar(value=v.get("countdown") or "3")
        ttk.Spinbox(f, from_=0, to=120, textvariable=self.var_countdown, width=12).grid(
            row=row, column=1, sticky="w", pady=4)
        ttk.Label(f, text="点开始后倒计时几秒再放视频", foreground="#666").grid(
            row=row, column=2, sticky="w")
        row += 1

        ttk.Separator(f, orient="horizontal").grid(row=row, column=0, columnspan=3,
                                                   sticky="ew", pady=10)
        row += 1

        # 分段设置
        ttk.Label(f, text="分段策略").grid(row=row, column=0, sticky="w", pady=4)
        self.var_mode = tk.StringVar(value=v.get("mode") or "auto")
        mode_frame = ttk.Frame(f)
        mode_frame.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        for text, val in (("自动（时间不够就截断）", "auto"),
                          ("总是分段", "split"),
                          ("只截断不分段", "truncate")):
            ttk.Radiobutton(mode_frame, text=text, value=val, variable=self.var_mode).pack(
                side="left", padx=(0, 10))
        row += 1

        ttk.Label(f, text="每段最短显示(ms)").grid(row=row, column=0, sticky="w", pady=4)
        self.var_min_seg = tk.StringVar(value=v.get("min_seg") or str(DEFAULT_MIN_SEG_MS))
        ttk.Spinbox(f, from_=0, to=5000, increment=50, textvariable=self.var_min_seg,
                    width=12).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(f, text="0 = 不限制（实测屏幕跟得上，不会去截断）；填 400 就是老行为",
                  foreground="#666").grid(row=row, column=2, sticky="w")
        row += 1

        # 打字机
        ttk.Label(f, text="逐字出现").grid(row=row, column=0, sticky="w", pady=4)
        tw = ttk.Frame(f)
        tw.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        self.var_typewriter = tk.StringVar(
            value=TYPE_MODE_TO_LABEL.get(v.get("typewriter", "off"), "关"))
        cb_type = ttk.Combobox(tw, textvariable=self.var_typewriter, values=TYPE_MODE_LABELS,
                               width=6, state="readonly")
        cb_type.pack(side="left")
        cb_type.bind("<<ComboboxSelected>>", lambda _e: self.on_typewriter_toggle())
        ttk.Label(tw, text="  关 / 自动（来得及才打字）/ 总是（用满时间硬打）",
                  foreground="#666").pack(side="left")
        row += 1

        ttk.Label(f, text="超长横移").grid(row=row, column=0, sticky="w", pady=4)
        mq = ttk.Frame(f)
        mq.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        self.var_marquee = tk.BooleanVar(value=v.get("marquee", "1") != "0")
        cb_mq = ttk.Checkbutton(mq, text="超长句横向滚动（先打字打满一行，再一格一格往左滚）",
                                variable=self.var_marquee, command=self.on_marquee_toggle)
        cb_mq.pack(side="left")
        ttk.Label(mq, text="  关掉就是老做法：切成 2/3 段或截断", foreground="#666").pack(side="left")
        row += 1

        ttk.Label(f, text="增量字幕").grid(row=row, column=0, sticky="w", pady=4)
        mi = ttk.Frame(f)
        mi.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        self.var_merge_inc = tk.BooleanVar(value=v.get("merge_inc", "0") == "1")
        ttk.Checkbutton(mi, text="合并增量字幕（字幕本身是 我 / 我要 / 我要玩 这种碎片时，"
                                 "合并成整句再让播放器演）",
                        variable=self.var_merge_inc,
                        command=self.on_merge_toggle).pack(side="left")
        row += 1

        ttk.Label(f, text="每字最短间隔(ms)").grid(row=row, column=0, sticky="w", pady=4)
        tw2 = ttk.Frame(f)
        tw2.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        self.var_type_step = tk.StringVar(value=v.get("type_step") or str(DEFAULT_TYPE_STEP_MS))
        ttk.Spinbox(tw2, from_=30, to=2000, increment=10, textvariable=self.var_type_step,
                    width=6).grid(row=0, column=0, sticky="w")
        ttk.Label(tw2, text="  自动模式下低于这个间隔就整句发送（19200 波特建议 80）",
                  foreground="#666").grid(row=0, column=1, sticky="w")
        row += 1

        # 屏幕宽度：按全角字算
        ttk.Label(f, text="每行宽度").grid(row=row, column=0, sticky="w", pady=4)
        wf = ttk.Frame(f)
        wf.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        self.var_width_fw = tk.StringVar(value=v.get("width_fw") or "12")
        sp = ttk.Spinbox(wf, from_=2, to=40, increment=1, textvariable=self.var_width_fw,
                         width=6, command=self.update_width_label)
        sp.grid(row=0, column=0, sticky="w")
        sp.bind("<KeyRelease>", lambda _e: self.update_width_label())
        sp.bind("<FocusOut>", lambda _e: self.on_width_change())
        sp.bind("<Return>", lambda _e: self.on_width_change())
        ttk.Label(wf, text=" 全角字（= 2 倍半角槽位）").grid(row=0, column=1, sticky="w")
        self.lbl_width = ttk.Label(wf, text="", foreground="#070")
        self.lbl_width.grid(row=0, column=2, sticky="w", padx=(10, 0))
        row += 1

        # 窗口 & 其它
        ttk.Label(f, text="视频窗口大小").grid(row=row, column=0, sticky="w", pady=4)
        self.var_size = tk.StringVar(value=v.get("size") or "1280x720")
        ttk.Combobox(f, textvariable=self.var_size, values=SIZE_CHOICES, width=12).grid(
            row=row, column=1, sticky="w", pady=4)
        row += 1

        opts = ttk.Frame(f)
        opts.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.var_fullscreen = tk.BooleanVar(value=v.get("fullscreen") == "1")
        ttk.Checkbutton(opts, text="视频全屏开始", variable=self.var_fullscreen).pack(side="left")
        self.var_dry = tk.BooleanVar(value=v.get("dry_run") == "1")
        ttk.Checkbutton(opts, text="干跑（不连串口，只预览）",
                        variable=self.var_dry).pack(side="left", padx=14)
        row += 1

        ttk.Label(f, text="从第几秒开始").grid(row=row, column=0, sticky="w", pady=4)
        self.var_start = tk.StringVar(value=v.get("start") or "0")
        ttk.Entry(f, textvariable=self.var_start, width=12).grid(
            row=row, column=1, sticky="w", pady=4)
        ttk.Label(f, text="可填 0 / 83 / 1:23", foreground="#666").grid(
            row=row, column=2, sticky="w")
        row += 1

        # 字幕时间偏移（字幕跟视频对不上时用）
        ttk.Label(f, text="字幕时间偏移").grid(row=row, column=0, sticky="w", pady=4)
        off = ttk.Frame(f)
        off.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        self.var_offset = tk.StringVar(value=v.get("offset") or "0")
        e_off = ttk.Entry(off, textvariable=self.var_offset, width=12)
        e_off.grid(row=0, column=0, sticky="w")
        e_off.bind("<Return>", lambda _e: self.on_offset_change())
        e_off.bind("<FocusOut>", lambda _e: self.on_offset_change())
        ttk.Button(off, text="对齐到 0", command=self.on_align_offset).grid(
            row=0, column=1, padx=6)
        ttk.Label(off, text='  auto=把第一句拉到 0；也可填 -1:32:35 / +2.5',
                  foreground="#666").grid(row=0, column=2, sticky="w")
        row += 1

        # 按钮
        btns = ttk.Frame(f)
        btns.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(18, 0))
        ttk.Button(btns, text="开始播放", command=self.on_start_clicked, width=16).pack(side="left")
        ttk.Button(btns, text="测试串口（发 READY）", command=self.on_test_port).pack(
            side="left", padx=8)
        ttk.Button(btns, text="刷新串口", command=self.refresh_ports).pack(side="left")
        ttk.Button(btns, text="退出", command=self.on_quit).pack(side="right")
        row += 1

        self.lbl_cfg_hint = ttk.Label(f, text="", foreground="#a00")
        self.lbl_cfg_hint.grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 0))

        self.refresh_ports()
        self.auto_detect_files()
        self.update_width_label()

    def auto_detect_files(self):
        if not self.var_video.get():
            v = find_video(SCRIPT_DIR)
            if v:
                self.var_video.set(os.path.basename(v))
        if not self.var_sub.get():
            s = find_subtitle(SCRIPT_DIR)
            if s:
                self.var_sub.set(os.path.basename(s))

    def refresh_ports(self):
        ports = [p for p, _ in list_ports()]
        self.combo_com["values"] = ports
        cur = self.var_com.get()
        if ports and cur not in ports:
            self.var_com.set("COM8" if "COM8" in ports else ports[0])
        self.lbl_ports.config(text=("检测到: " + ", ".join(ports)) if ports else "没有检测到串口")

    def browse_video(self):
        p = filedialog.askopenfilename(initialdir=SCRIPT_DIR, title="选择视频",
                                       filetypes=[("视频", "*.mp4 *.mkv *.mov *.avi *.flv *.webm *.ts"),
                                                  ("所有文件", "*.*")])
        if p:
            self.var_video.set(p)

    def browse_sub(self):
        p = filedialog.askopenfilename(initialdir=SCRIPT_DIR, title="选择字幕",
                                       filetypes=[("字幕", "*.srt *.lrc *.ass *.ssa"),
                                                  ("所有文件", "*.*")])
        if p:
            self.var_sub.set(p)

    def resolve_path(self, name):
        if not name:
            return None
        if os.path.isabs(name) and os.path.exists(name):
            return name
        p = os.path.join(SCRIPT_DIR, name)
        return p if os.path.exists(p) else None

    # ---------------- 播放界面 ----------------
    def build_play_view(self):
        f = ttk.Frame(self.root, padding=10)
        self.play_frame = f
        f.columnconfigure(0, weight=1)

        self.lbl_status = ttk.Label(f, text="未开始", font=("Microsoft YaHei UI", 12, "bold"))
        self.lbl_status.grid(row=0, column=0, sticky="w")

        self.lbl_meta = ttk.Label(f, text="", foreground="#555")
        self.lbl_meta.grid(row=1, column=0, sticky="w", pady=(2, 8))

        # 进度
        prog = ttk.Frame(f)
        prog.grid(row=2, column=0, sticky="ew")
        prog.columnconfigure(1, weight=1)
        self.lbl_pos = ttk.Label(prog, text="0:00", width=6)
        self.lbl_pos.grid(row=0, column=0)
        self.var_scale = tk.DoubleVar(value=0.0)
        self.scale = ttk.Scale(prog, from_=0, to=1000, variable=self.var_scale,
                               command=self.on_scale_move)
        self.scale.grid(row=0, column=1, sticky="ew", padx=6)
        self.scale.bind("<ButtonPress-1>", self.on_scale_press)
        self.scale.bind("<ButtonRelease-1>", self.on_scale_release)
        self.lbl_dur = ttk.Label(prog, text="0:00", width=6)
        self.lbl_dur.grid(row=0, column=2)
        self.seeking = False

        # LED 预览
        box = ttk.LabelFrame(f, text="LED 屏幕预览（24 槽位）", padding=8)
        box.grid(row=3, column=0, sticky="ew", pady=10)
        box.columnconfigure(0, weight=1)
        self.lbl_led = tk.Label(box, text="", font=(self.font_mono, 20),
                                bg="#101010", fg="#40ff40", anchor="w", padx=10, pady=10)
        self.lbl_led.grid(row=0, column=0, sticky="ew")
        self.lbl_led_info = ttk.Label(box, text="", foreground="#555")
        self.lbl_led_info.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.lbl_source = ttk.Label(box, text="", foreground="#333", wraplength=820,
                                    justify="left")
        self.lbl_source.grid(row=2, column=0, sticky="w", pady=(2, 0))

        # 控制按钮
        ctl = ttk.Frame(f)
        ctl.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        self.btn_pause = ttk.Button(ctl, text="⏸ 暂停", command=self.on_pause)
        self.btn_pause.pack(side="left")
        ttk.Button(ctl, text="⏪ -10 秒", command=lambda: self.ff and self.ff.send_key(VK_LEFT)).pack(
            side="left", padx=4)
        ttk.Button(ctl, text="⏩ +10 秒", command=lambda: self.ff and self.ff.send_key(VK_RIGHT)).pack(
            side="left", padx=4)
        ttk.Button(ctl, text="⏮ 从头播", command=self.on_restart).pack(side="left", padx=4)
        ttk.Button(ctl, text="⛶ 视频全屏(f)", command=lambda: self.ff and self.ff.send_key(VK_F)).pack(
            side="left", padx=4)
        ttk.Button(ctl, text="⏹ 停止", command=self.on_stop).pack(side="left", padx=4)
        ttk.Label(ctl, text="逐字出现").pack(side="left", padx=(12, 2))
        self.cmb_type = ttk.Combobox(ctl, textvariable=self.var_typewriter,
                                     values=TYPE_MODE_LABELS, width=4, state="readonly")
        self.cmb_type.pack(side="left")
        self.cmb_type.bind("<<ComboboxSelected>>", lambda _e: self.on_typewriter_toggle())
        self.chk_marquee = ttk.Checkbutton(ctl, text="超长横移", variable=self.var_marquee,
                                           command=self.on_marquee_toggle)
        self.chk_marquee.pack(side="left", padx=(10, 0))
        ttk.Button(ctl, text="⚙ 返回设置", command=self.on_back_to_config).pack(side="right")

        # 手动发送
        man = ttk.LabelFrame(f, text="手动发送到屏幕", padding=8)
        man.grid(row=5, column=0, sticky="ew")
        man.columnconfigure(0, weight=1)
        self.var_manual = tk.StringVar()
        e = ttk.Entry(man, textvariable=self.var_manual)
        e.grid(row=0, column=0, sticky="ew")
        e.bind("<Return>", lambda _e: self.on_manual_send())
        ttk.Button(man, text="发送", command=self.on_manual_send).grid(row=0, column=1, padx=6)
        ttk.Button(man, text="清屏(发空格)", command=self.on_clear_screen).grid(row=0, column=2)
        self.lbl_manual = ttk.Label(man, text="", foreground="#666")
        self.lbl_manual.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # 日志
        logbox = ttk.LabelFrame(f, text="日志", padding=6)
        logbox.grid(row=6, column=0, sticky="nsew", pady=(10, 0))
        f.rowconfigure(6, weight=1)
        logbox.columnconfigure(0, weight=1)
        logbox.rowconfigure(0, weight=1)
        self.txt_log = tk.Text(logbox, height=10, wrap="none", font=("Consolas", 9))
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logbox, orient="vertical", command=self.txt_log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt_log.config(yscrollcommand=sb.set, state="disabled")

        for seq, fn in (("<space>", lambda e: self.on_pause()),
                        ("<Left>", lambda e: self.ff and self.ff.send_key(VK_LEFT)),
                        ("<Right>", lambda e: self.ff and self.ff.send_key(VK_RIGHT)),
                        ("<Escape>", lambda e: self.on_stop())):
            self.root.bind(seq, fn)

    # ---------------- 界面切换 ----------------
    def show_config(self):
        self.play_frame.pack_forget()
        self.config_frame.pack(fill="both", expand=True)

    def show_play(self):
        self.config_frame.pack_forget()
        self.play_frame.pack(fill="both", expand=True)

    # ---------------- 日志 ----------------
    def log(self, msg, to_console=False):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        try:
            self.txt_log.config(state="normal")
            self.txt_log.insert("end", line + "\n")
            self.txt_log.see("end")
            self.txt_log.config(state="disabled")
        except Exception:
            pass
        if to_console:
            try:
                print(line)
            except Exception:
                pass

    # ---------------- 配置 -> 播放 ----------------
    def collect_config(self):
        v = self.cfg_values
        v["video"] = self.var_video.get().strip()
        v["subtitle"] = self.var_sub.get().strip()
        v["com"] = self.var_com.get().strip() or "COM8"
        v["baud"] = str(int(float(self.var_baud.get() or DEFAULT_BAUD)))
        v["countdown"] = str(int(float(self.var_countdown.get() or 0)))
        v["size"] = self.var_size.get().strip() or "1280x720"
        v["mode"] = self.var_mode.get()
        v["min_seg"] = str(self._int(self.var_min_seg.get(), DEFAULT_MIN_SEG_MS))
        fw = self._int(self.var_width_fw.get(), 12)
        v["width_fw"] = str(fw)
        v["width"] = str(fw_to_slots(fw))          # 内部一律用槽位（1 全角字 = 2 槽）
        v["dry_run"] = "1" if self.var_dry.get() else "0"
        v["fullscreen"] = "1" if self.var_fullscreen.get() else "0"
        v["start"] = self.var_start.get().strip() or "0"
        v["typewriter"] = self.typewriter_mode()
        v["type_step"] = str(self._int(self.var_type_step.get(), DEFAULT_TYPE_STEP_MS))
        v["marquee"] = "1" if self.var_marquee.get() else "0"
        v["offset"] = (self.var_offset.get() or "0").strip() or "0"
        v["merge_inc"] = "1" if self.var_merge_inc.get() else "0"
        return v

    def typewriter_mode(self):
        return TYPE_LABEL_TO_MODE.get(self.var_typewriter.get(), "off")

    def update_width_label(self):
        fw = self._int(self.var_width_fw.get(), 12)
        slots = fw_to_slots(fw)
        self.lbl_width.config(text="= %d 槽位（半角 %d 个，全角 %d 个字）" % (slots, slots, fw))

    def on_width_change(self):
        """改了每行宽度：更新说明，播放中则立刻按新宽度重排"""
        self.update_width_label()
        self.collect_config()
        self.save_cfg()
        if self.cues:
            self.rebuild_events()
            self.log("每行宽度改为 %s 全角字（%s 槽），已按新宽度重排"
                     % (self.var_width_fw.get(), self.cfg_values.get("width")),
                     to_console=True)

    # ---- 字幕时间偏移：字幕跟视频对不上时的救命开关 ----

    def apply_offset(self, log_it=False):
        """
        按「字幕时间偏移」把整条字幕时间轴平移。
        auto / -1:32:35 / +2.5 都行；播放中改也能立刻生效。
        """
        raw = getattr(self, "cues_raw", None) or []
        text = (self.var_offset.get() or "0").strip()
        off = resolve_offset(text, raw, self.duration_ms)
        self.cues = shift_cues(raw, off)
        if self.engine:
            self.rebuild_events()
        if log_it and raw:
            if off:
                self.log("字幕时间偏移 %s：第一句 %s → %s（共 %d 句，偏移后剩 %d 句）"
                         % (fmt_offset(off), fmt_time(raw[0][0]), fmt_time(self.cues[0][0])
                            if self.cues else "-", len(raw), len(self.cues)), to_console=True)
            else:
                self.log("字幕时间偏移 0（第一句 %s）" % fmt_time(raw[0][0]), to_console=True)
        return off

    def on_align_offset(self):
        """「对齐到 0」：把字幕第一句挪到 0，并把具体数值填进输入框（看得见、可再手调）"""
        sub = self.resolve_path(self.var_sub.get().strip())
        if not sub:
            self.lbl_cfg_hint.config(text="先选好字幕文件再对齐", foreground="#a00")
            return
        try:
            cues = load_subtitles(sub)
        except Exception as e:
            self.lbl_cfg_hint.config(text="字幕读不了：%s" % e, foreground="#a00")
            return
        if not cues:
            self.lbl_cfg_hint.config(text="字幕里没有可用内容", foreground="#a00")
            return
        off = -cues[0][0]
        self.var_offset.set(fmt_offset(off, signed=True))
        self.lbl_cfg_hint.config(
            text="已填偏移 %s：第一句 %s → 0:00（全片 %d 句）"
                 % (fmt_offset(off, signed=True), fmt_time(cues[0][0]), len(cues)),
            foreground="#070")
        if self.cues_raw:
            self.apply_offset(log_it=True)

    def on_offset_change(self):
        self.collect_config()
        self.save_cfg()
        if self.cues_raw:
            self.apply_offset(log_it=True)

    # ---- 增量字幕：字幕自己就是 我/我要/我要玩 这种碎片时，可选合并成整句 ----

    def prepare_cues(self, log_it=True):
        """
        从原始字幕出发：可选「合并增量字幕」-> 时间偏移 -> 得到 self.cues。
        播放中调用会立刻重排。
        """
        src = list(getattr(self, "file_cues", []) or [])
        if src and self.var_merge_inc.get():
            src, away = merge_incremental(src)
            if log_it:
                self.log("增量合并：合并掉 %d 句碎片（我/我要/我要玩 → 我要玩原神），剩 %d 句"
                         % (away, len(src)), to_console=True)
        self.cues_raw = src
        return self.apply_offset(log_it=log_it)

    def on_merge_toggle(self):
        self.collect_config()
        self.save_cfg()
        if self.file_cues:
            self.prepare_cues(log_it=True)

    def report_subtitle_type(self, cues):
        """看看这份字幕本身是不是动画字幕，是就提示一句"""
        try:
            ana = analyze_subtitles(cues)
        except Exception:
            return
        if ana["animated"] and ana["advice"]:
            self.log("字幕检查：%s（相邻句里增量 %d 处 / 滚动 %d 处，时间相接的动画 %d 处，占 %.1f%%）。%s"
                     % (ana["verdict"], ana["counts"]["grow"], ana["counts"]["scroll"],
                        ana["animated"], ana["ratio"] * 100, ana["advice"]),
                     to_console=True)

    @staticmethod
    def _int(text, default):
        try:
            return int(float(str(text).strip()))
        except (TypeError, ValueError):
            return default

    def rebuild_events(self, log_list=None):
        """按当前配置重建"什么时间发什么字"的事件表（播放中改打字机/宽度也会走这里）"""
        v = self.cfg_values
        self.events = build_events(
            self.cues,
            self._int(v.get("width"), FULLWIDTH_WIDTH),
            v.get("mode", "auto"),
            self._int(v.get("min_seg"), DEFAULT_MIN_SEG_MS),
            log=(log_list.append if log_list is not None else None),
            typewriter=self.typewriter_mode(),
            type_min_step_ms=self._int(v.get("type_step"), DEFAULT_TYPE_STEP_MS),
            baud=self._int(v.get("baud"), DEFAULT_BAUD),
            marquee=v.get("marquee", "1") != "0")
        if self.engine:
            self.engine.set_events(self.events)
        return self.events

    def on_typewriter_toggle(self):
        """配置界面改了：下次开始生效；播放中改了：立刻重建事件表生效"""
        self.collect_config()
        self.save_cfg()
        if not self.cues:
            return
        self.rebuild_events()
        self.log("设置已更新：%s（逐字打字的段 %d 个，事件 %d 条）｜每行全角 %s 字"
                 % (self.effect_summary(), self._n_typing(), len(self.events),
                    self.var_width_fw.get()), to_console=True)

    def on_marquee_toggle(self):
        """超长横移开关：播放中也能立刻生效"""
        self.collect_config()
        self.save_cfg()
        if not self.cues:
            return
        self.rebuild_events()
        self.log("设置已更新：%s（横移的句 %d 个，事件 %d 条）"
                 % (self.effect_summary(), self._n_marquee(), len(self.events)),
                 to_console=True)

    def _n_typing(self):
        return len({(e["cue"], e["seg"]) for e in self.events if e.get("typing")})

    def _n_marquee(self):
        return len({e["cue"] for e in self.events if e.get("scroll")})

    def effect_summary(self):
        return "逐字出现=%s，超长横移=%s" % (
            self.var_typewriter.get(), "开" if self.var_marquee.get() else "关")

    def on_test_port(self):
        v = self.collect_config()
        self.save_cfg()
        try:
            scr = LEDScreen(v["com"], int(v["baud"]), self.var_dry.get(),
                            log=lambda m: self.log(m))
            scr.open()
            scr.send("READY")
            scr.close()
            self.lbl_cfg_hint.config(text="测试完成：%s @ %s 已发送 READY" % (v["com"], v["baud"]),
                                     foreground="#070")
        except OSError as e:
            self.lbl_cfg_hint.config(text="串口测试失败：%s" % e, foreground="#a00")

    def on_start_clicked(self):
        v = self.collect_config()
        video = self.resolve_path(v["video"])
        sub = self.resolve_path(v["subtitle"])
        if not video:
            self.lbl_cfg_hint.config(text="找不到视频文件", foreground="#a00"); return
        if not sub:
            self.lbl_cfg_hint.config(text="找不到字幕文件", foreground="#a00"); return
        if not self.ffplay:
            messagebox.showerror(APP_TITLE, "没有找到 ffplay，请先安装 ffmpeg\n"
                                            "（本机之前用过 ffmpeg，确认一下 PATH）")
            return
        self.save_cfg()

        # 解析字幕 -> 事件表
        try:
            self.file_cues = load_subtitles(sub)
            if not self.file_cues:
                messagebox.showerror(APP_TITLE, "字幕文件里没解析出内容")
                return
            self.report_subtitle_type(self.file_cues)
            self.prepare_cues(log_it=True)
            logs = []
            self.events = self.rebuild_events(logs)
        except Exception as e:
            messagebox.showerror(APP_TITLE, "字幕解析失败：%s" % e)
            return
        if not self.events:
            messagebox.showerror(APP_TITLE, "字幕里没有可用内容")
            return
        # 字幕整体跑到很后面去了？提醒一句（这十有八九是字幕和视频对不上）
        off_ms = resolve_offset(v["offset"], self.cues_raw, self.duration_ms)
        if not off_ms and self.cues_raw[0][0] > 30000:
            self.log("提示：字幕第一句在 %s 才开始，如果视频比它短，前面都不会有字幕——"
                     "把「字幕时间偏移」填 auto（或点「对齐到 0」）就能把整条时间轴拉到开头"
                     % fmt_time(self.cues_raw[0][0]), to_console=True)

        # 打开串口（先开，免得倒计时完才发现串口打不开）
        try:
            if self.screen:
                self.screen.close()
        except Exception:
            pass
        try:
            self.screen = LEDScreen(v["com"], int(v["baud"]), v["dry_run"] == "1",
                                    log=lambda m: self.log(m, to_console=True))
            self.screen.open()
        except OSError as e:
            if not messagebox.askyesno(APP_TITLE, "串口打开失败：%s\n\n要不要用干跑模式继续（只在界面预览）？" % e):
                return
            v["dry_run"] = "1"
            self.screen = LEDScreen(v["com"], int(v["baud"]), True,
                                    log=lambda m: self.log(m, to_console=True))
            self.screen.open()

        self.engine = SubtitleEngine(self.events, self.screen,
                                     log=lambda m: self.log(m))
        self.send_count = 0
        self.stopped = False

        self.show_play()
        n_split = len({e["cue"] for e in self.events if e["nseg"] > 1})
        n_trunc = len({e["cue"] for e in self.events if e["truncated"]})
        n_type = self._n_typing()
        n_marq = self._n_marquee()
        self.lbl_meta.config(text="%s ｜ 字幕 %s（%d 句 → %d 条，分段 %d 句 / 截断 %d 句 / 打字 %d 段 / 横移 %d 句）｜ 每行 %s 全角字 ｜ %s ｜ %s" % (
            os.path.basename(video), os.path.basename(sub), len(self.cues),
            len(self.events), n_split, n_trunc, n_type, n_marq,
            v.get("width_fw", "12"), self.effect_summary(),
            ("干跑模式" if v["dry_run"] == "1" else "%s @ %s" % (v["com"], v["baud"]))))
        self.lbl_led.config(text="")
        self.lbl_led_info.config(text="")
        self.lbl_source.config(text="")

        self.ff = FfplayController(self.ffplay, video, v["size"], v["fullscreen"] == "1",
                                   log=lambda m: self.log(m, to_console=True),
                                   scale=self.ui_scale)

        # 视频总长
        self.duration_ms = 0
        if self.ffprobe:
            threading.Thread(target=self._probe_duration, args=(self.ffprobe, video),
                             daemon=True).start()

        # 倒计时
        cd = int(v["countdown"])
        start_ms = self.parse_start(v["start"])
        self.overlay_wait = None
        self.overlay = CountdownOverlay(self.root, on_cancel=self.on_stop)
        if v["fullscreen"] == "1":
            # 全屏模式：倒计时也整屏黑屏显示（倒完等视频窗口出来再关，中间不闪桌面）
            self.overlay.show(str(cd) if cd > 0 else "开始",
                              hint="视频马上开始…（按 Esc 取消）")
        if cd > 0:
            self.log("倒计时 %d 秒后开始…%s" % (cd, "（全屏黑屏倒计时）" if v["fullscreen"] == "1" else ""))
            self.countdown(cd, start_ms)
        else:
            self.begin_playback(start_ms)

    def _probe_duration(self, ffprobe, video):
        try:
            out = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                                  "format=duration", "-of", "csv=p=0", video],
                                 capture_output=True, text=True, timeout=20)
            dur = float((out.stdout or "0").strip().splitlines()[0])
            self.duration_ms = int(dur * 1000)
            self.root.after(0, lambda: self.scale.config(to=max(1, self.duration_ms)))
            self.root.after(0, lambda: self.lbl_dur.config(text=fmt_time(self.duration_ms)))
        except Exception:
            pass

    def countdown(self, n, start_ms):
        self.lbl_status.config(text="倒计时 %d 秒…" % n, foreground="#c60")
        self.lbl_led.config(text=("%d" % n).center(12))
        if self.overlay and self.overlay.visible:
            self.overlay.show(str(n), hint="视频马上开始…（按 Esc 取消）")
        if n <= 0:
            self.countdown_job = None
            self.begin_playback(start_ms)
            return
        self.countdown_job = self.root.after(
            1000, lambda: self.countdown(n - 1, start_ms))

    def begin_playback(self, start_ms):
        if self.stopped or not self.ff:
            return
        self.lbl_led.config(text="")
        self.ff.start(start_ms)
        self.loading = True
        self.lbl_status.config(text="播放中", foreground="#070")
        self.engine.reset()
        self._last_tick = time.monotonic()
        # 全屏黑屏还留着，等 ffplay 窗口真的出来了再关（最多等 3 秒）
        if self.overlay and self.overlay.visible:
            self.overlay.show("开始", hint="正在打开视频…")
            self.overlay_wait = time.monotonic()
        if not self._tick_running:
            self._tick_running = True
            self.root.after(20, self.tick)

    def parse_start(self, text):
        text = (text or "0").strip()
        try:
            if ":" in text:
                sec = 0.0
                for p in text.split(":"):
                    sec = sec * 60 + float(p)
                return int(sec * 1000)
            return int(float(text) * 1000)
        except ValueError:
            return 0

    # ---------------- 播放循环 ----------------
    def tick(self):
        if self.stopped:
            self._tick_running = False
            return
        if not self.ff:
            self._tick_running = False
            return
        pos, paused = self.ff.position_ms()
        if self.loading and pos > 0:
            self.loading = False

        # 视频窗口出来了（或等超时了）就把全屏黑屏收掉
        if self.overlay_wait is not None:
            ready = bool(getattr(self.ff, "hwnd", 0))
            if ready or (time.monotonic() - self.overlay_wait) > 3.0:
                if self.overlay:
                    self.overlay.close()
                self.overlay_wait = None
                self.ff.focus()          # 黑屏收掉后把视频窗口顶到前面，键盘直接能用

        now = time.monotonic()
        if self.ff.finished():
            self.lbl_status.config(text="播放结束", foreground="#06c")
            self.update_progress(pos)
            self.root.after(400, self.tick)
            return

        if not self.loading and not paused:
            try:
                ev = self.engine.tick(pos)
            except OSError as e:
                # 串口掉了也不能让播放循环死掉：报错 + 试着重连
                self.log("串口发送失败：%s（尝试重连…）" % e, to_console=True)
                self.lbl_status.config(text="串口错误：%s" % e, foreground="#a00")
                try:
                    self.screen.close()
                    self.screen.open()
                except OSError:
                    pass
                ev = None
            if ev is not None:
                self.show_sent(ev)
                self.send_count += 1

        self.update_progress(pos)
        if self.loading:
            self.lbl_status.config(text="缓冲中…", foreground="#c60")
        elif paused:
            self.lbl_status.config(text="已暂停（按空格继续）", foreground="#c60")
            self.btn_pause.config(text="▶ 继续")
        else:
            self.lbl_status.config(text="播放中", foreground="#070")
            self.btn_pause.config(text="⏸ 暂停")
        self.root.after(25, self.tick)

    def update_progress(self, pos):
        if not self.seeking:
            self.var_scale.set(min(pos, max(1, self.duration_ms or 1)))
        self.lbl_pos.config(text=fmt_time(pos))

    def show_sent(self, ev):
        width = int(self.cfg_values.get("width") or FULLWIDTH_WIDTH)
        used = text_slots(ev["text"])
        pad = ev["text"] + " " * max(0, width - used)
        self.lbl_led.config(text=pad)
        if ev.get("scroll"):
            seg = "横移 %d/%d" % (ev["frame"] + 1, ev["nframe"])
        elif ev.get("typing"):
            seg = "打字 %d/%d" % (ev["frame"] + 1, ev["nframe"])
        elif ev["nseg"] > 1:
            seg = "第 %d/%d 段" % (ev["seg"] + 1, ev["nseg"])
        else:
            seg = "整句"
        if ev["nseg"] > 1 and (ev.get("typing") or ev.get("scroll")):
            seg += "（第 %d/%d 段）" % (ev["seg"] + 1, ev["nseg"])
        mark = "（截断）" if ev["truncated"] else ""
        # 宽度按全角字报（"9/12" 这样看更直观）
        self.lbl_led_info.config(
            text="%s %s ｜ 全角 %.1f/%d 字（%d/%d 槽）｜ 第 %d 条 ｜ 发送时刻 %s" % (
                seg, mark, used / 2.0, width // 2, used, width,
                self.send_count + 1, fmt_time(ev["t"])))
        self.lbl_source.config(text="原句: " + sanitize(ev["source"]))

    # ---------------- 控制 ----------------
    def on_pause(self):
        if self.ff and self.ff.send_key(VK_SPACE):
            self.log("发送按键: 暂停/继续")

    def on_restart(self):
        if not self.ff:
            return
        self.log("从头重播")
        self.engine.reset()
        self.loading = True
        self.ff.start(0)

    def on_stop(self):
        self.stopped = True
        if self.countdown_job:
            self.root.after_cancel(self.countdown_job)
            self.countdown_job = None
        if self.overlay:
            self.overlay.close()
        self.overlay_wait = None
        if self.ff:
            self.ff.stop()
        self.lbl_status.config(text="已停止", foreground="#888")
        self.log("已停止")

    def on_back_to_config(self):
        self.on_stop()
        self.lbl_cfg_hint.config(text="已停止播放，可以改配置后重新开始", foreground="#070")
        self.show_config()

    def on_scale_press(self, _e):
        self.seeking = True

    def on_scale_release(self, _e):
        self.seeking = False
        if not self.ff:
            return
        target = int(self.var_scale.get())
        self.log("跳到 %s" % fmt_time(target))
        self.engine.reset()
        self.loading = True
        self.ff.start(target)

    def on_scale_move(self, val):
        if self.seeking:
            self.lbl_pos.config(text=fmt_time(float(val)))

    def on_manual_send(self):
        text = self.var_manual.get().strip()
        if not text or not self.screen:
            return
        try:
            self.screen.send(text)
            self.lbl_manual.config(
                text="已发送: %s（%d 槽）" % (fit_slots(sanitize(text)), text_slots(sanitize(text))))
            self.var_manual.set("")
        except OSError as e:
            self.lbl_manual.config(text="发送失败: %s" % e)

    def on_clear_screen(self):
        if not self.screen:
            return
        width = int(self.cfg_values.get("width") or FULLWIDTH_WIDTH)
        try:
            self.screen.send(" " * width)
            self.lbl_manual.config(text="已发送 %d 个空格（%d 槽，有的屏固件不会清屏，看实际效果）"
                                        % (width, width))
        except OSError as e:
            self.lbl_manual.config(text="发送失败: %s" % e)

    def on_quit(self):
        self.on_stop()
        try:
            if self.screen:
                self.screen.close()
        except Exception:
            pass
        try:
            if self.overlay:
                self.overlay.close()
        except Exception:
            pass
        self.root.destroy()


# --------------------------------------------------------------------------

def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = LedPlayerApp(root, sys.argv[1:])
    if app.cfg_values.get("autostart") == "1":
        root.after(300, app.on_start_clicked)
    root.protocol("WM_DELETE_WINDOW", app.on_quit)
    root.mainloop()


if __name__ == "__main__":
    main()
