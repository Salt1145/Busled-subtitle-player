# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Salt1145
# https://github.com/Salt1145/Busled-subtitle-player
"""
led_screen.py  —— LED 点阵屏串口发送 + 字幕分段核心库

屏幕规格：全角 12 字 = 半角 24 字 = 24 个"槽位"(slot)
    - 一个半角字符(ASCII)  占 1 槽
    - 一个全角字符(中文等) 占 2 槽
    - 槽位 == GBK 编码后的字节数（这就是屏幕固件眼里的长度）

分段规则（按需求）：
    1. 一句话超过 24 槽 -> 均匀切成 n 段（24 槽一档：25~48 槽切 2 段，49~72 槽切 3 段……）
    2. 每段按字幕时间轴均匀分布：2 段时后半句放在时间中点，3 段时放在 1/3、2/3 处
    3. 切分优先在空格、标点处断开，避免把词劈开
    4. 「自动」模式下，如果分完之后每段显示时间太短（默认 <400ms，屏幕根本看不清），
       就退化成"往后截断"：只显示前 24 槽，占满整句时间

被 led_player.py（视频播放器）和 lrc2com8.py（纯字幕发送）共用。
"""

from __future__ import annotations

import bisect
import ctypes
import os
import re
import sys
import time
from ctypes import wintypes

# --------------------------------------------------------------------------
# 一、Win32 串口（沿用原始 lrc2com8.py 的写法，纯 ctypes，不需要 pyserial）
# --------------------------------------------------------------------------

GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

DEFAULT_BAUD = 19200


class DCB(ctypes.Structure):
    _fields_ = [("DCBlength", wintypes.DWORD), ("BaudRate", wintypes.DWORD),
                ("fBinary", wintypes.DWORD), ("fParity", wintypes.DWORD),
                ("fOutxCtsFlow", wintypes.DWORD), ("fOutxDsrFlow", wintypes.DWORD),
                ("fDtrControl", wintypes.DWORD), ("fDsrSensitivity", wintypes.DWORD),
                ("fTXContinueOnXoff", wintypes.DWORD), ("fOutX", wintypes.DWORD),
                ("fInX", wintypes.DWORD), ("fErrorChar", wintypes.DWORD),
                ("fNull", wintypes.DWORD), ("fRtsControl", wintypes.DWORD),
                ("fAbortOnError", wintypes.DWORD), ("fDummy2", wintypes.DWORD),
                ("wReserved", wintypes.WORD), ("XonLim", wintypes.WORD),
                ("XoffLim", wintypes.WORD), ("ByteSize", wintypes.BYTE),
                ("Parity", wintypes.BYTE), ("StopBits", wintypes.BYTE),
                ("XonChar", ctypes.c_char), ("XoffChar", ctypes.c_char),
                ("ErrorChar", ctypes.c_char), ("EofChar", ctypes.c_char),
                ("EvtChar", ctypes.c_char), ("wReserved1", wintypes.WORD)]


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [("ReadIntervalTimeout", wintypes.DWORD),
                ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
                ("ReadTotalTimeoutConstant", wintypes.DWORD),
                ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
                ("WriteTotalTimeoutConstant", wintypes.DWORD)]


def list_ports():
    """枚举本机串口，返回 [('COM8', '设备描述'), ...]"""
    import winreg
    ports = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                ports.append((str(value), str(name)))
                i += 1
    except OSError:
        pass
    ports.sort(key=lambda p: int(re.sub(r"\D", "", p[0]) or 0))
    return ports


def open_com(port, baud=DEFAULT_BAUD):
    """打开串口，返回内核句柄"""
    kernel32 = ctypes.windll.kernel32
    name = "\\\\.\\%s" % port
    h = kernel32.CreateFileW(name, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE:
        raise OSError("无法打开串口 %s（被占用？没插？）" % port)
    dcb = DCB()
    dcb.DCBlength = ctypes.sizeof(DCB)
    kernel32.GetCommState(h, ctypes.byref(dcb))
    dcb.BaudRate = int(baud)
    dcb.ByteSize = 8
    dcb.Parity = 0            # NOPARITY
    dcb.StopBits = 0          # ONESTOPBIT
    if not kernel32.SetCommState(h, ctypes.byref(dcb)):
        kernel32.CloseHandle(h)
        raise OSError("设置串口参数失败：%s @ %s" % (port, baud))
    to = COMMTIMEOUTS()
    to.WriteTotalTimeoutConstant = 1000
    kernel32.SetCommTimeouts(h, ctypes.byref(to))
    return h


def write_com(h, data):
    kernel32 = ctypes.windll.kernel32
    written = wintypes.DWORD()
    buf = (ctypes.c_byte * len(data))(*data)
    ok = kernel32.WriteFile(h, buf, len(data), ctypes.byref(written), None)
    if not ok:
        raise OSError("串口写入失败（屏幕被拔了？）")
    return written.value


def close_com(h):
    if h:
        ctypes.windll.kernel32.CloseHandle(h)


# --------------------------------------------------------------------------
# 二、协议：凯伦3G（车载 LED 屏 / 公交报站屏常用）
#
#     7E <addr> 01 05 0B 00 <len> 07 00 <n> <GBK 文本> <XOR> 7F
#
#     凯伦3G 协议，本条用的是其中的「临时用语」指令
#     （临时插一条话，屏幕收到就整屏替换显示 —— 打字机/横移那种毫秒级连发就是靠它）
#     实测屏：海信车载屏；同族 7E…7F 帧格式的报站屏一般也能用，但命令字可能不一样，换屏先确认
#
#     addr  = 屏号/地址，FF 是广播（默认，一车多屏时可以指定）
#     07    = 命令字：临时用语
#     len   = n + 3，n = 文本字节数 = 槽位数
#     XOR   = 从 7E 开始逐字节异或
#
#     字节流和最早那版 lrc2com8.py 逐字节一致，没有改动；
#     自检里会用老算法对拍，保证没写错。
# --------------------------------------------------------------------------

def karen3g_packet(text, addr=0xFF):
    """凯伦3G「临时用语」指令包（GBK 编码，一帧一条）"""
    data = text.encode("gbk")
    n = len(data)
    packet = bytes([0x7E, addr & 0xFF, 0x01, 0x05, 0x0B, 0x00, n + 3, 0x07, 0x00, n]) + data
    cs = 0
    for b in packet:
        cs ^= b
    return packet + bytes([cs, 0x7F])


hisense_packet = karen3g_packet    # 别名：海信屏上也是这条
karen_packet = karen3g_packet      # 别名：最早那版脚本里的名字


# --------------------------------------------------------------------------
# 三、槽位计算 / 截断 / 分段
# --------------------------------------------------------------------------

FULLWIDTH_WIDTH = 24          # 屏幕宽度（槽位）
DEFAULT_MIN_SEG_MS = 0        # 默认不限制"每段最短显示"：实测屏幕反应得过来，
                              # 就不因为时间短去截断了（想恢复老行为就填 400）
MIN_SEND_GAP_MS = 60          # 两次发送之间的硬下限，防止刷屏（原来 120ms）

# 断句机会（在这些字符之后可以断开）
BREAK_AFTER = set(" \t,.;:!?)]}>，。、；：！？）】》」』’”…-—/\\|")


def sanitize(text):
    """整理成可以发到屏幕的文本：换行/制表变空格，去掉不可编码字符"""
    if text is None:
        return ""
    out = []
    for ch in str(text):
        if ch in "\r\n\t":
            out.append(" ")
            continue
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            continue
        try:
            ch.encode("gbk")
        except UnicodeEncodeError:
            ch = "?"       # 屏幕字库不支持的字，替换掉，保证槽位可算
        out.append(ch)
    return "".join(out)


def char_slots(ch):
    """单字符占几个槽位：半角 1，全角 2（等价于 GBK 字节数）"""
    if ord(ch) < 0x80:
        return 1
    try:
        return len(ch.encode("gbk"))
    except UnicodeEncodeError:
        return 2


def text_slots(text):
    """整串占多少槽位"""
    return sum(char_slots(c) for c in text)


def fw_to_slots(fw):
    """全角字数 -> 槽位（1 个全角字 = 2 槽）。屏幕上习惯按全角字说宽度，所以界面上让用户填全角字数。"""
    return max(1, int(round(float(fw) * 2)))


def slots_to_fw(slots):
    """槽位 -> 全角字数"""
    return int(round(int(slots) / 2.0))


def fit_slots(text, width=FULLWIDTH_WIDTH):
    """按槽位截断（不会把全角字切一半），返回不超过 width 槽的最长前缀"""
    acc = 0
    out = []
    for ch in text:
        w = char_slots(ch)
        if acc + w > width:
            break
        out.append(ch)
        acc += w
    return "".join(out)


# 兼容旧名字
truncate_slots = fit_slots


def wrap_slots(text, width=FULLWIDTH_WIDTH):
    """
    按槽位切成若干段，每段 <= width 槽。

    段数按 "ceil(总槽位 / width)" 算：24 槽切 1 段，25~48 槽切 2 段，49~72 槽切 3 段……
    在此前提下，尽量在空格/标点处断开，避免把单词劈开；
    但如果"优雅断句"会多出一段（多占一段显示时间），就老老实实按 24 槽硬切。
    """
    text = sanitize(text).strip()
    if not text:
        return []
    total = text_slots(text)
    n_total = max(1, -(-total // width))
    chunks = []
    rest = text
    while rest:
        if text_slots(rest) <= width:
            chunks.append(rest.strip())
            break
        # 硬切点：能塞进 width 槽的最长前缀
        acc = 0
        cut = 0
        for i, ch in enumerate(rest):
            w = char_slots(ch)
            if acc + w > width:
                break
            acc += w
            cut = i + 1
        if cut <= 0:                     # 理论上不会发生（单字最大 2 槽）
            chunks.append(fit_slots(rest, width))
            break
        # 在硬切点前面找一个"断句点"（只看最近的一个）
        # 只有当"在这里断开后，后面的字还能装进剩下的段数"时才用它，
        # 这样既尽量不断开单词，又不会为了断句多切出一段。
        room = (n_total - len(chunks) - 1) * width     # 后面几段还能装多少槽
        for i in range(cut, max(0, cut - 8), -1):
            if rest[i - 1] in BREAK_AFTER:
                if text_slots(rest[i:].lstrip()) <= room:
                    cut = i
                break
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    return [c for c in chunks if c]


def segment_text(text, width=FULLWIDTH_WIDTH, mode="auto",
                 min_seg_ms=DEFAULT_MIN_SEG_MS, duration_ms=None):
    """
    返回一句话该显示的若干段文本。

    mode = "auto"      : 需要几段就切几段；如果每段显示时间 < min_seg_ms，退化成截断
           "split"     : 总是切分（哪怕一闪而过）
           "truncate"  : 从不切分，直接截断到 width 槽
    """
    text = sanitize(text).strip()
    if not text:
        return []
    if mode == "truncate":
        return [fit_slots(text, width)]
    chunks = wrap_slots(text, width)
    if len(chunks) <= 1:
        return [fit_slots(text, width)]
    if mode == "auto" and duration_ms is not None:
        if duration_ms / len(chunks) < min_seg_ms:
            return [fit_slots(text, width)]
    return chunks


def split_times(start_ms, end_ms, n):
    """把 [start, end] 均分成 n 份，返回 n 个起点（后半句落在时间中点）"""
    if n <= 1:
        return [start_ms]
    step = max(0.0, (end_ms - start_ms) / float(n))
    return [start_ms + step * i for i in range(n)]


DEFAULT_TYPE_STEP_MS = 80      # 打字机：每个字最少间隔（再快屏幕/串口也来不及）
DEFAULT_TYPE_RATIO = 0.8       # 打字占这段显示时间的比例，剩下的时间让整句停住看清
DEFAULT_TYPE_HARD_MS = 35      # "总是打字"模式下的硬底线（串口再快也就这样了）
DEFAULT_MARQUEE_STEP_MS = 40   # 横移：每滚一格最少间隔（滚动不必每格看清，可以比打字快）


def packet_wire_ms(text_bytes, baud):
    """这么多字节在串口上跑完要多少毫秒（8N1 每字节 10 位）"""
    return text_bytes * 10.0 / max(1, baud) * 1000.0


def typing_wire_ms(text, baud=DEFAULT_BAUD):
    """
    打字机把一句话"逐字打完"总共要在线路上跑多少毫秒（含协议头尾，粗略）。
    说明为什么有些句子打不了字：这是 1+2+3+…+n 的平方级开销。
    """
    total = 0
    for i in range(1, len(text) + 1):
        payload = len(text[:i].encode("gbk", errors="replace"))
        total += payload + 12          # 协议头 10 字节 + 长度/校验/尾 2 字节
    return packet_wire_ms(total, baud)


def frames_wire_ms(texts, baud=DEFAULT_BAUD):
    """一串帧全发一遍要多少毫秒线路时间"""
    total = 0
    for t in texts:
        total += len(t.encode("gbk", errors="replace")) + 12
    return packet_wire_ms(total, baud)


def visible_chars(chars, i, width):
    """从第 i 个字开始，最多能塞几个字进 width 个槽位"""
    acc = 0
    k = 0
    for ch in chars[i:]:
        w = char_slots(ch)
        if acc + w > width:
            break
        acc += w
        k += 1
    return k


def marquee_frames(text, width, start_ms, end_ms, typing=True,
                   ratio=DEFAULT_TYPE_RATIO, min_step_ms=DEFAULT_MARQUEE_STEP_MS,
                   force=False, hard_floor_ms=DEFAULT_TYPE_HARD_MS, baud=DEFAULT_BAUD):
    """
    超长句横移（滚动）：先把字一个一个打出来装满一行，然后整行一格一格往左滚，
    直到最后一个字露出来。宽度按槽位算，中英混排也不会切坏半个字。

        我要玩原神明朝攫取零啊啊啊啊杀杀杀（17 字，一行只能放 12 字）
          我 / 我要 / … / 我要玩原神明朝攫取零啊啊     <- 打字，打满一行
          要玩原神明朝攫取零啊啊啊                     <- 开始横移
          玩原神明朝攫取零啊啊啊啊
          原神明朝攫取零啊啊啊啊杀
          神明朝攫取零啊啊啊啊杀杀
          明朝攫取零啊啊啊啊杀杀杀                     <- 尾巴露出来了

    返回 [(时间ms, 文本, 类型)] 或 None（时间/线路来不及，交给调用方去切段或截断）。
    """
    chars = list(sanitize(text))
    n = len(chars)
    if n < 2:
        return None
    texts = []          # [(文本, 类型)]
    k0 = visible_chars(chars, 0, width)
    if k0 <= 0:
        return None
    if typing:
        for i in range(1, k0 + 1):
            texts.append(("".join(chars[:i]), "typing"))
    i = 0
    while True:
        k = visible_chars(chars, i, width)
        win = "".join(chars[i:i + k])
        if not texts or texts[-1][0] != win:
            texts.append((win, "scroll"))
        if i + k >= n:
            break
        i += 1
    if len(texts) < 2:
        return None

    span = max(1.0, float(end_ms) - float(start_ms))
    if force:
        ratio = max(ratio, 0.9)
    ideal = span * ratio / len(texts)
    wire = frames_wire_ms([t for t, _ in texts], baud)
    if ideal >= min_step_ms and (force or wire <= span * ratio):
        step = ideal
    elif force:
        step = max(float(hard_floor_ms), ideal)
    else:
        return None
    frames = [(start_ms + i * step, txt, kind) for i, (txt, kind) in enumerate(texts)]
    if force and step > ideal:      # 硬底线导致滚不完：裁掉超出这段时间的尾巴
        kept = [f for f in frames if f[0] < end_ms]
        frames = kept or frames[:1]
    return frames


def typing_frames(text, start_ms, end_ms, min_step_ms=DEFAULT_TYPE_STEP_MS,
                  ratio=DEFAULT_TYPE_RATIO, force=False,
                  hard_floor_ms=DEFAULT_TYPE_HARD_MS, baud=DEFAULT_BAUD):
    """
    打字机效果：把一句话拆成"一个字一个字蹦出来"的若干帧。

        "我要玩原神"（1 秒）->  0ms:我  160ms:我要  320ms:我要玩  480ms:我要玩原
                              640ms:我要玩原神（后面一直显示整句）

    自动模式（force=False）满足两个条件才打字，否则返回 [(start, 整句)]：
        1. 每字间隔 >= min_step_ms（太密了看不清）
        2. 整句"逐字打完"需要的线路时间 <= 可用时间（19200 波特下是平方级开销，打不完就是打不完）
    总是模式（force=True）：只要每字间隔不低于 hard_floor_ms 就硬打，用满这段显示时间。
    """
    chars = list(text)
    n = len(chars)
    span = max(1.0, float(end_ms) - float(start_ms))
    if n < 2:
        return [(start_ms, text)]
    if force:
        ratio = max(ratio, 0.9)
    ideal = span * ratio / n
    wire = typing_wire_ms(text, baud)
    if ideal >= min_step_ms and (force or wire <= span * ratio):
        step = ideal
    elif force:
        step = max(float(hard_floor_ms), ideal)
    else:
        return [(start_ms, text)]
    frames = [(start_ms + i * step, "".join(chars[:i + 1])) for i in range(n)]
    if force and step > ideal:      # 硬底线导致打不完：裁掉超出这段时间的尾巴
        kept = [f for f in frames if f[0] < end_ms]
        frames = kept or frames[:1]
    return frames


# --------------------------------------------------------------------------
# 四、字幕解析（SRT / LRC）
# --------------------------------------------------------------------------

_SRT_TIME = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def _ms(h, m, s, frac):
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(str(frac).ljust(3, "0")[:3])


def parse_srt(path):
    """返回 [(start_ms, end_ms, text), ...]"""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        raw = f.read()
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            m = _SRT_TIME.search(line)
            if m:
                g = [int(x) for x in m.groups()]
                start = _ms(*g[0:4])
                end = _ms(*g[4:8])
                text = "\n".join(lines[i + 1:]).strip()
                if text:
                    cues.append((start, end, text))
                break
    cues.sort(key=lambda c: c[0])
    return cues


_LRC_TIME = re.compile(r"\[(\d+):(\d+)[.:](\d+)\]([^\]]*)")


def parse_lrc(path):
    """返回 [(start_ms, end_ms, text), ...]（结束时间 = 下一句开始）"""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        lines = f.read().splitlines()
    tmp = []
    for line in lines:
        if line.startswith("[") and not _LRC_TIME.match(line) and ":" not in line[:6]:
            continue
        for m in _LRC_TIME.finditer(line):
            ms = int(m.group(1)) * 60000 + int(m.group(2)) * 1000 + \
                int(m.group(3).ljust(3, "0")[:3])
            text = m.group(4).strip()
            if text:
                tmp.append((ms, text))
    tmp.sort(key=lambda x: x[0])
    cues = []
    for i, (ms, text) in enumerate(tmp):
        end = tmp[i + 1][0] if i + 1 < len(tmp) else ms + 3000
        cues.append((ms, max(end, ms + 200), text))
    return cues


def load_subtitles(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".lrc":
        return parse_lrc(path)
    return parse_srt(path)


# ---- 时间轴工具：解析 "1:32:35" / "92.5" / "-1:32:35" 这类写法 ----

def parse_time_ms(text, default=0):
    """'1:32:35' / '92.5' / '-1:32:35' / '+3' / '' -> 毫秒"""
    if text is None:
        return default
    s = str(text).strip()
    if not s:
        return default
    sign = 1
    if s[0] in "+-":
        if s[0] == "-":
            sign = -1
        s = s[1:].strip()
    try:
        if ":" in s:
            sec = 0.0
            for part in s.split(":"):
                sec = sec * 60 + float(part)
            return int(sign * sec * 1000)
        return int(sign * float(s) * 1000)
    except ValueError:
        return default


def shift_cues(cues, offset_ms):
    """整条字幕时间轴平移（可负）。负偏移之后整句跑到 0 之前的，直接丢掉。"""
    if not offset_ms:
        return list(cues)
    out = []
    for start, end, text in cues:
        s2, e2 = start + offset_ms, end + offset_ms
        if e2 <= 0:
            continue
        out.append((max(0, s2), max(0, e2), text))
    return out


def resolve_offset(text, cues, video_ms=0):
    """
    把界面/命令行里填的偏移量变成毫秒：
      'auto'  -> 自动对齐（把第一句挪到 0；如果第一句已经在视频里，就挪到视频起点）
      '1:32:35' / '-92.5' / '92.5' -> 直接当偏移量
      空 -> 0
    """
    s = str(text or "").strip().lower()
    if s in ("auto", "自动", "对齐", "对齐到0", "align"):
        if not cues:
            return 0
        first = cues[0][0]
        # 字幕整体跑到视频后面去了 -> 拉到 0；否则对齐到视频起点
        if video_ms and first > video_ms:
            return -first
        return -first if first > 30000 else 0
    return parse_time_ms(text, 0)


# --------------------------------------------------------------------------
# 四点五、相邻字幕关系分析：这句是不是"增量字幕 / 滚动字幕"
#
# 有些字幕文件本身就把动画做进去了，相邻两句是这样的：
#     我 -> 我要 -> 我要玩 -> 我要玩原神                 （增量 / 打字机式）
#     我要玩原神明朝攫取零啊啊 -> 要玩原神明朝攫取零啊啊啊   （滚动 / 横移式）
# 判断出来之后可以：① 提示"别再叠加播放器自己的打字机/横移" ② 把碎片合并成整句
# --------------------------------------------------------------------------

def _norm_text(s):
    """比较前先归一化：去掉首尾空白，中间连续空白压成一个"""
    return re.sub(r"\s+", " ", (s or "").strip())


def _lcp(a, b):
    """最长公共前缀的长度"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _lcs(a, b):
    """最长公共后缀的长度"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[len(a) - 1 - i] == b[len(b) - 1 - i]:
        i += 1
    return i


def classify_pair(a, b, gap_ms=None, contiguity_ms=400):
    """
    判断相邻两句字幕的关系（a 在前，b 在后）：

      same      两句完全一样（重复）
      grow      b 是在 a 后面接着长出来的，a 是 b 的前缀   -> 增量字幕（字幕自带打字机）
      shrink    b 是 a 的前缀，内容在变少                   -> 逐字消失 / 收尾
      scroll    b 像是 a 往左滚了 k 个字（a 去掉开头 k 个字就是 b 的开头）-> 字幕自带横移
      similar   公共前后缀很长，多半是同一句的小改动（标点、错字）
      unrelated 没关系

    另外给一个 contiguous：两句在时间上是不是紧挨着（gap <= contiguity_ms 或重叠），
    只有"文字增量 + 时间相接"才算真的自带动画，光文字像不算数。
    """
    A, B = _norm_text(a), _norm_text(b)
    res = {"kind": "unrelated", "delta": "", "shift": 0,
           "lcp": _lcp(A, B), "lcs": _lcs(A, B), "gap_ms": gap_ms,
           "contiguous": (gap_ms is not None and gap_ms <= contiguity_ms)}
    if not A or not B:
        return res
    if A == B:
        res["kind"] = "same"
        return res
    if B.startswith(A):                       # 我 -> 我要
        res["kind"] = "grow"
        res["delta"] = B[len(A):]
        return res
    if A.startswith(B):                       # 我要玩 -> 我要
        res["kind"] = "shrink"
        res["delta"] = A[len(B):]
        return res
    for k in range(1, min(len(A), 8) + 1):    # 我要玩原神明朝 -> 要玩原神明朝攫
        if len(A) - k >= 2 and B.startswith(A[k:]):
            res["kind"] = "scroll"
            res["shift"] = k
            res["delta"] = B[len(A) - k:]
            return res
    m = min(len(A), len(B))
    if res["lcp"] >= max(3, int(m * 0.6)) or res["lcs"] >= max(3, int(m * 0.6)):
        res["kind"] = "similar"
    return res


INCREMENTAL_KINDS = ("grow", "shrink", "scroll")
KIND_LABELS = {"same": "重复", "grow": "增量", "shrink": "递减",
               "scroll": "滚动", "similar": "相似", "unrelated": "无关"}


def analyze_subtitles(cues, contiguity_ms=400, samples=8):
    """
    扫一遍整份字幕，看看它本身是不是"动画字幕"。
    返回 {total, counts, animated, ratio, verdict, advice, sample}
      animated  文字增量 + 时间相接 的处数（真正自带打字机/横移的）
    """
    counts = {k: 0 for k in KIND_LABELS}
    sample = []
    animated = 0
    for i in range(len(cues) - 1):
        s1, e1, t1 = cues[i]
        s2, e2, t2 = cues[i + 1]
        r = classify_pair(t1, t2, gap_ms=s2 - e1, contiguity_ms=contiguity_ms)
        counts[r["kind"]] += 1
        if r["kind"] in INCREMENTAL_KINDS and r["contiguous"]:
            animated += 1
            if len(sample) < samples:
                sample.append((i, t1, t2, r))
    total = max(0, len(cues) - 1)
    ratio = (animated / total) if total else 0.0
    grow_like = counts["grow"] + counts["shrink"]
    # 阈值：长片子按比例，短片子给个 3 处的最低门槛（不然十几句的小文件永远判不出来）
    if counts["scroll"] >= max(3, total * 0.15):
        verdict, advice = ("字幕本身是滚动（横移）式",
                           "建议把播放器的「超长横移」关掉，或者用「合并增量字幕」让它别重复演")
    elif grow_like >= max(3, total * 0.25):
        verdict, advice = ("字幕本身是逐字（打字机）式",
                           "建议把「逐字出现」设成关（字幕自己已经在逐字了），"
                           "或者勾「合并增量字幕」让播放器重新演一遍")
    else:
        verdict, advice = "普通字幕", ""
    return {"total": total, "counts": counts, "animated": animated,
            "ratio": ratio, "verdict": verdict, "advice": advice, "sample": sample}


def merge_incremental(cues, contiguity_ms=400, max_items=80):
    """
    把"一句一句长出来"的碎片合并成整句，交给播放器自己去打字/横移：

        我(0.0-0.5) 我要(0.5-1.0) 我要玩(1.0-1.5) 我要玩原神(1.5-2.5)
          -> 我要玩原神(0.0-2.5)  外加一个字段记着"这段原本是 4 帧"

    只合并 grow / same 且时间相接的；滚动式、递减式不动（合并了意思会变）。
    返回 (合并后的 cues, 合并掉的句数)
    """
    out = []
    merged_away = 0
    i = 0
    while i < len(cues):
        start, end, text = cues[i]
        run = 1
        j = i + 1
        while j < len(cues) and run < max_items:
            s2, e2, t2 = cues[j]
            r = classify_pair(text, t2, gap_ms=s2 - end, contiguity_ms=contiguity_ms)
            if r["kind"] in ("grow", "same") and r["contiguous"]:
                text, end = t2, e2
                run += 1
                j += 1
                continue
            break
        if run > 1:
            merged_away += run - 1
        out.append((start, end, text))
        i = j
    return out, merged_away


def _list_by_ext(directory, exts):
    try:
        files = [f for f in os.listdir(directory)
                 if os.path.splitext(f)[1].lower() in exts]
    except OSError:
        return []
    files.sort(key=lambda f: exts.index(os.path.splitext(f)[1].lower()))
    return files


def find_subtitle(directory, prefer=(".srt", ".lrc", ".ass", ".ssa", ".txt")):
    files = _list_by_ext(directory, prefer)
    return os.path.join(directory, files[0]) if files else None


def find_video(directory, prefer=(".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".ts", ".wmv")):
    files = _list_by_ext(directory, prefer)
    return os.path.join(directory, files[0]) if files else None


# --------------------------------------------------------------------------
# 五、事件表：把字幕变成"什么时间发什么字"
# --------------------------------------------------------------------------

def build_events(cues, width=FULLWIDTH_WIDTH, mode="auto",
                 min_seg_ms=DEFAULT_MIN_SEG_MS, log=None, gap_ms=MIN_SEND_GAP_MS,
                 typewriter=False, type_min_step_ms=DEFAULT_TYPE_STEP_MS,
                 type_ratio=DEFAULT_TYPE_RATIO, baud=DEFAULT_BAUD,
                 marquee=True, marquee_min_step_ms=DEFAULT_MARQUEE_STEP_MS):
    """
    返回 [{t, end, text, cue, seg, nseg, source, truncated, typing, scroll,
           frame, nframe, gap}, ...]，按时间排序。
    第 i 段/帧从 t 显示到 end（end = 下一帧起点 / 这段结束）。

    typewriter : False/"off" 不打字 | True/"auto" 有时间才打字 | "always" 总是打字
    marquee    : True 时，超过一行的句子不上切段，改成横移滚动（打字机开着就"先打字打满再滚"）
    """
    tw_mode = ("always" if typewriter == "always"
               else "auto" if typewriter in (True, 1, "1", "auto", "on") else "off")
    events = []

    def emit(frames, ci, source_text, seg_i, nseg, truncated, hold_end):
        """frames: [(t, text, kind)]，kind = whole / typing / scroll"""
        nf = len(frames)
        # 帧之间可以挨得近一点（否则每帧都被防刷屏逻辑吃掉）
        # 下限 25ms：播放器每 25ms 走一次时钟，再密也分不出来了
        step = (frames[1][0] - frames[0][0]) if nf > 1 else 0.0
        ev_gap = min(float(gap_ms), max(25.0, step * 0.5)) if nf > 1 else gap_ms
        for fi, (ft, ftext, kind) in enumerate(frames):
            if events and ft < events[-1]["t"] + ev_gap:
                if log and fi == 0:
                    log("跳过（时间太挤）: %s" % ftext)
                continue
            fend = frames[fi + 1][0] if fi + 1 < nf else max(hold_end, ft + 1)
            events.append({"t": ft, "end": max(fend, ft + 1), "text": ftext,
                           "cue": ci, "seg": seg_i, "nseg": nseg,
                           "source": source_text, "truncated": truncated,
                           "typing": kind == "typing" and nf > 1,
                           "scroll": kind == "scroll",
                           "frame": fi, "nframe": nf, "gap": ev_gap})

    for ci, (start, end, text) in enumerate(cues):
        # 下一句来得比本句结束还早时，本句实际可用时间要按下一句的起点算
        next_start = cues[ci + 1][0] if ci + 1 < len(cues) else None
        eff_end = end if next_start is None else min(end, max(next_start, start))
        dur = max(0, eff_end - start)
        too_long = text_slots(sanitize(text)) > width

        # ① 超长 + 开了横移：整句滚动，不切段也不截断
        if marquee and too_long:
            mf = marquee_frames(text, width, start, eff_end,
                                typing=(tw_mode != "off"),
                                ratio=type_ratio, min_step_ms=marquee_min_step_ms,
                                force=(tw_mode == "always"), baud=baud)
            if mf:
                emit(mf, ci, text, 0, 1, False, eff_end)
                continue
            if log:
                plan = marquee_frames(text, width, start, eff_end,
                                      typing=(tw_mode != "off"), ratio=type_ratio,
                                      min_step_ms=0.001, force=True, baud=1e9)
                span = max(1.0, eff_end - start)
                if plan:
                    texts = [t for _, t, _ in plan]
                    need = frames_wire_ms(texts, baud)
                    if need > span * type_ratio:
                        why = ("%d 帧要 %.0fms 线路时间，这段只有 %.0fms"
                               % (len(plan), need, span * type_ratio))
                    else:
                        why = ("%d 帧每格只有 %.0fms < %dms"
                               % (len(plan), span * type_ratio / len(plan),
                                  marquee_min_step_ms))
                else:
                    why = "太短没法滚"
                log("横移来不及（%s），改回切段/截断: %s" % (why, sanitize(text)[:20]))

        # ② 常规：切段 / 截断
        chunks = segment_text(text, width, mode, min_seg_ms, dur)
        if not chunks:
            continue
        truncated = too_long and len(chunks) == 1
        n = len(chunks)
        starts = split_times(start, eff_end, n)
        for i, chunk in enumerate(chunks):
            t = starts[i]
            seg_end = max(starts[i + 1] if i + 1 < n else eff_end, t + 1)
            if tw_mode != "off":
                raw = typing_frames(chunk, t, seg_end, type_min_step_ms, type_ratio,
                                    force=(tw_mode == "always"), baud=baud)
                if log and len(raw) <= 1 and len(chunk) > 1:
                    span = max(1.0, seg_end - t)
                    wire = typing_wire_ms(chunk, baud)
                    if wire > span * type_ratio:
                        why = ("这根线 %d 波特全速也要 %.0fms，这段只有 %.0fms"
                               % (baud, wire, span * type_ratio))
                    else:
                        why = ("每字只有 %.0fms < %dms" % (span * type_ratio / len(chunk),
                                                          type_min_step_ms))
                    log("打字机跳过（%s）: %s" % (why, chunk))
            else:
                raw = [(t, chunk)]
            frames = [(ft, ftext, "typing" if len(raw) > 1 else "whole") for ft, ftext in raw]
            emit(frames, ci, text, i, n, truncated, seg_end)
    events.sort(key=lambda e: e["t"])
    return events


class SubtitleEngine:
    """
    时间 -> 发送。时钟由外面喂进来：
      * 播放器：用 ffplay 报的播放位置（毫秒）
      * 纯字幕工具：用墙钟
    这样暂停、快进、拖进度条、变速，字幕都会自动跟上。
    """

    def __init__(self, events, screen, log=None, gap_ms=MIN_SEND_GAP_MS):
        self.events = events
        self.starts = [e["t"] for e in events]
        self.screen = screen
        self.log = log or (lambda msg: None)
        self.gap_ms = gap_ms
        self.last_key = None
        self.last_t = None
        self.sent = 0

    def set_events(self, events):
        """换一套事件表（比如播放中打开/关掉打字机效果）"""
        self.events = events
        self.starts = [e["t"] for e in events]
        self.reset()
        self.last_t = None

    def active(self, t_ms):
        """当前应该显示的事件（没有则 None）"""
        if not self.events:
            return None
        i = bisect.bisect_right(self.starts, t_ms) - 1
        if i < 0:
            return None
        ev = self.events[i]
        if t_ms < ev["end"]:
            return ev
        return None

    def reset(self):
        self.last_key = None

    def tick(self, t_ms):
        """喂当前播放位置，返回本次真正发出的事件（没发就 None）"""
        ev = self.active(t_ms)
        if ev is None:
            return None
        key = (ev["cue"], ev["seg"], ev.get("frame", 0))
        if key == self.last_key:
            return None
        # 防刷屏：打字机帧的间隔比普通事件短，用事件自带的 gap
        gap = ev.get("gap", self.gap_ms)
        if self.last_t is not None and abs(t_ms - self.last_t) < gap:
            return None
        self.screen.send(ev["text"])
        self.last_key = key
        self.last_t = t_ms
        self.sent += 1
        return ev


# --------------------------------------------------------------------------
# 六、发送器（真串口 / 干跑）
# --------------------------------------------------------------------------

class LEDScreen:
    def __init__(self, port="COM8", baud=DEFAULT_BAUD, dry_run=False,
                 log=None, packet_log=None, addr=0xFF):
        self.port = port
        self.baud = int(baud)
        self.addr = int(addr) & 0xFF      # 屏号/地址，FF = 广播
        self.dry_run = dry_run
        self.log = log or (lambda msg: None)
        self.packet_log = packet_log
        self.handle = None
        self.last_text = None
        self.count = 0

    @property
    def opened(self):
        return self.handle is not None

    def open(self):
        if self.dry_run:
            self.log("[干跑] 不打开串口，只打印要发的内容")
            return True
        self.handle = open_com(self.port, self.baud)
        self.log("串口已打开: %s @ %d 8N1" % (self.port, self.baud))
        return True

    def send(self, text):
        text = fit_slots(sanitize(text))
        pkt = karen3g_packet(text, self.addr)
        if self.dry_run:
            self.log("[干跑] %2d槽 | %s" % (text_slots(text), text))
        else:
            if not self.opened:
                self.open()
            write_com(self.handle, pkt)
            self.log("%2d槽 | %s" % (text_slots(text), text))
        if self.packet_log:
            try:
                with open(self.packet_log, "ab") as f:
                    f.write(("%s\t%s\n" % (time.strftime("%H:%M:%S"), pkt.hex())).encode("ascii"))
            except OSError:
                pass
        self.last_text = text
        self.count += 1
        return pkt

    def close(self):
        if self.handle is not None:
            close_com(self.handle)
            self.handle = None


# --------------------------------------------------------------------------
# 七、自检：python led_screen.py --selftest
# --------------------------------------------------------------------------

def _reference_packet(text):        # 原 lrc2com8.py 的算法，用来对齐字节
    data = text.encode('gbk')
    n = len(data)
    ll = n + 3
    packet = bytes([0x7E, 0xFF, 0x01, 0x05, 0x0B, 0x00, ll, 0x07, 0x00, n]) + data
    cs = 0
    for b in packet:
        cs ^= b
    return packet + bytes([cs, 0x7F])


def _selftest():
    print("=== 1. 槽位计算 ===")
    samples = ["咬着葱", "A stop job is running for miku (0min 0s/4min 0s)",
               "(1/2) 正在删除 miku               [VOCALOID] 22%"]
    for s in samples:
        print("  %3d 槽 | %s" % (text_slots(s), s))

    print("\n=== 2. 凯伦3G 临时用语包：新代码 == 最早那版 lrc2com8.py（逐字节对拍） ===")
    for s in ["READY", "咬着葱", "A stop job is running for miku (0min 0s/4min 0s)"]:
        a, b = karen3g_packet(s), _reference_packet(s)
        print("  %-6s %s" % ("OK" if a == b else "差异!", a.hex()))
    print("  指定屏号(比如 01)时：%s" % karen3g_packet("READY", addr=0x01).hex())

    print("\n=== 3. 分段 ===")
    for s in ["咬着葱 仰望着天空泪水滑落而下", "A stop job is running for miku (0min 6s/4min 0s)",
              "删除失败：xxx.wav 文件正在使用", "短句"]:
        chunks = segment_text(s, 24, "auto", 400, 1000)
        print("  %-46s -> %d 段: %s" % (s[:46], len(chunks), " | ".join(chunks)))

    print("\n=== 4. 时间轴（前后半句落在时间中点） ===")
    cues = [(0, 2000, "咬着葱 仰望着天空泪水滑落而下"),
            (2000, 2400, "删除失败：xxx.wav 文件正在使用"),   # 太短 -> 截断
            (3000, 4000, "short line")]
    for ev in build_events(cues, 24, "auto", 400):
        print("  %7.0fms-%7.0fms [第%d/%d段]%s %s" %
              (ev["t"], ev["end"], ev["seg"] + 1, ev["nseg"],
               " (截断)" if ev["truncated"] else "", ev["text"]))

    print("\n=== 5. 引擎走时（干跑，模拟 4 秒播放） ===")
    screen = LEDScreen(dry_run=True, log=lambda m: print("   ", m))
    eng = SubtitleEngine(build_events(cues, 24, "auto", 400), screen)
    for t in range(0, 4000, 50):
        eng.tick(t)

    print("\n=== 6. 打字机效果（逐字出现） ===")
    print("  开关关掉时（默认）：")
    for ev in build_events([(0, 1000, "我要玩原神")], 24, "auto", 400):
        print("    %5.0fms  %s" % (ev["t"], ev["text"]))
    print("  自动模式，时间够：")
    tw = build_events([(0, 1000, "我要玩原神"), (1200, 1500, "这句太短来不及打字")],
                      24, "auto", 400, typewriter="auto")
    for ev in tw:
        print("    %5.0fms  %-14s %s" % (ev["t"], ev["text"],
                                         "(打字 %d/%d)" % (ev["frame"] + 1, ev["nframe"])
                                         if ev["typing"] else "(整句/来不及打字)"))
    print("  总是模式，同一句话硬打（用满时间）：")
    for ev in build_events([(1200, 1500, "这句太短来不及打字")], 24, "auto", 400,
                           typewriter="always"):
        print("    %5.0fms  %-14s %s" % (ev["t"], ev["text"],
                                         "(打字 %d/%d)" % (ev["frame"] + 1, ev["nframe"])
                                         if ev["typing"] else "(整句)"))
    print("  打字机的串口开销（19200 波特，把前缀反复重发，是平方级）：")
    for s in ["我要玩原神", ">即使在我歌唱得不好之时",
              "A stop job is running for miku (0min 6s/4min 0s)"]:
        print("    %2d 字 / %2d 槽  ->  打完需要 %.0f ms 线路时间 | %s"
              % (len(s), text_slots(s), typing_wire_ms(s), s[:28]))

    print("\n=== 7. 超长句横移（滚动） ===")
    demo = [(0, 4000, "我要玩原神明朝攫取零啊啊啊啊杀杀杀")]
    print("  横移关掉（老做法：切成 2 段）:")
    for ev in build_events(demo, 24, "auto", 400, typewriter="auto", marquee=False):
        print("    %6.0fms  %-24s %s" % (ev["t"], ev["text"],
                                         "打字" if ev["typing"] else "整句"))
    print("  横移打开（先打字打满一行，再一格一格往左滚）:")
    for ev in build_events(demo, 24, "auto", 400, typewriter="auto", marquee=True):
        print("    %6.0fms  %-24s %s" % (ev["t"], ev["text"],
                                         "横移" if ev["scroll"] else
                                         ("打字" if ev["typing"] else "整句")))
    print("  横移 + 打字机关掉（只滚不打字）:")
    for ev in build_events(demo, 24, "auto", 400, typewriter="off", marquee=True)[:4]:
        print("    %6.0fms  %-24s %s" % (ev["t"], ev["text"],
                                         "横移" if ev["scroll"] else "整句"))

    print("\n=== 8. 相邻句关系：这句是不是增量字幕（自带打字机/横移） ===")
    for a, b in [("我", "我要"), ("我要玩", "我要玩原神"), ("我要玩原神", "我要玩"),
                 ("我要玩原神明朝攫取零啊啊", "要玩原神明朝攫取零啊啊啊"),
                 ("咬着葱 仰望着天空", "泪水滑落而下")]:
        r = classify_pair(a, b, gap_ms=0)
        extra = ("新增 " + r["delta"]) if r["delta"] and r["kind"] == "grow" else \
                ("左移 %d 字" % r["shift"]) if r["shift"] else ""
        print("    %-20s -> %-20s %-4s %s" % (a[:20], b[:20], KIND_LABELS[r["kind"]], extra))
    frag = [(0, 500, "我"), (500, 1000, "我要"), (1000, 1500, "我要玩"),
            (1500, 2500, "我要玩原神"), (3000, 4000, "完全无关的一句")]
    merged, away = merge_incremental(frag)
    print("  合并碎片: %d 句 -> %d 句（合并掉 %d）: %s"
          % (len(frag), len(merged), away, merged))
    ana = analyze_subtitles(frag)
    print("  判定: %s（增量 %d 处，占 %.0f%%）"
          % (ana["verdict"], ana["counts"]["grow"], ana["ratio"] * 100))

    print("\n=== 9. 打字机 + 引擎（干跑，1.5 秒） ===")
    screen2 = LEDScreen(dry_run=True, log=lambda m: print("   ", m))
    eng2 = SubtitleEngine(tw, screen2)
    for t in range(0, 1500, 20):
        eng2.tick(t)


def _main(argv):
    if "--selftest" in argv or not argv:
        _selftest()
        return 0
    if "--list-ports" in argv:
        for port, desc in list_ports():
            print("%-8s %s" % (port, desc))
        return 0
    if "--send" in argv:
        i = argv.index("--send")
        text = argv[i + 1] if i + 1 < len(argv) else "READY"
        port = "COM8"
        baud = DEFAULT_BAUD
        dry = "--dry-run" in argv
        if "--com" in argv:
            port = argv[argv.index("--com") + 1]
        if "--baud" in argv:
            baud = int(argv[argv.index("--baud") + 1])
        screen = LEDScreen(port, baud, dry, log=lambda m: print(m))
        screen.open()
        screen.send(text)
        screen.close()
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
