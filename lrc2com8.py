# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Salt1145
# https://github.com/Salt1145/Busled-subtitle-player
"""
lrc2com8.py  —— 纯字幕发送工具（不放视频，只按时间轴往 LED 屏发字幕）

这是原来那个 lrc2com8.py 的改造版：
  * 支持 .srt（现在用的就是 srt）和 .lrc
  * 屏幕只有 24 槽位（全角 12 字 / 半角 24 字）：
      - 一句话超过 24 槽 -> 按时间均匀切成 n 段（2 段时后半句落在时间中点，3 段就切 3 段）
      - 分完之后每段显示时间太短（默认 <400ms 看不清）-> 自动退化成"往后截断"（只显示前 24 槽）
  * 串口、波特率、倒计时都可配置、可保存
  * 协议字节流和原来一模一样，没有改动

用法示例：
    python lrc2com8.py --list-ports                 列串口
    python lrc2com8.py --dry-run                    干跑：只打印要发的内容，不开串口
    python lrc2com8.py --com COM8 --baud 19200 --countdown 5
    python lrc2com8.py --speed 20 --dry-run         20 倍速跑一遍时间轴，检查切段效果

想边放视频边发字幕（推荐）请用：python led_player.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from led_screen import (DEFAULT_BAUD, DEFAULT_MARQUEE_STEP_MS, DEFAULT_MIN_SEG_MS,
                        DEFAULT_TYPE_STEP_MS, FULLWIDTH_WIDTH, KIND_LABELS,
                        LEDScreen, SubtitleEngine, analyze_subtitles, build_events,
                        find_subtitle, list_ports, load_subtitles,
                        merge_incremental, resolve_offset, shift_cues, text_slots)


def _fmt_ms(ms):
    s = max(0, ms) / 1000.0
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    return "%d:%02d:%02d" % (h, m, int(s % 60)) if h else "%d:%02d" % (m, int(s % 60))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def parse_start(text):
    """'1:23.5' / '83' / '01:23' -> 秒（None 或空 -> 0）"""
    if text is None:
        return 0.0
    text = str(text).strip()
    if not text:
        return 0.0
    if ":" in text:
        parts = [float(p) for p in text.split(":")]
        sec = 0.0
        for p in parts:
            sec = sec * 60 + p
        return sec
    return float(text)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把字幕按时间轴发到 LED 屏（24 槽位，超长自动分段）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", "-f", default=None, help="字幕文件（默认自动找目录里的 .srt/.lrc）")
    ap.add_argument("--com", default="COM8", help="串口号，默认 COM8")
    ap.add_argument("--addr", default="FF",
                    help="屏号/地址（两位十六进制），默认 FF = 广播；一车多屏时可以指定，如 01")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="波特率，默认 19200")
    ap.add_argument("--countdown", type=int, default=3, help="按回车后再倒计时几秒开始，默认 3")
    ap.add_argument("--width", type=int, default=FULLWIDTH_WIDTH,
                    help="屏幕槽位数，默认 24（= 全角 12 字）")
    ap.add_argument("--width-fw", type=int, default=None,
                    help="每行全角字数（例如 9 就是 18 槽位）；填了就以它为准")
    ap.add_argument("--mode", choices=["auto", "split", "truncate"], default="auto",
                    help="auto=时间够就分段、不够就截断(默认) split=总是分段 truncate=只截断")
    ap.add_argument("--min-seg", type=int, default=DEFAULT_MIN_SEG_MS,
                    help="自动模式下每段最短显示毫秒，默认 %d（0 = 不限制，实测屏幕跟得上）"
                         % DEFAULT_MIN_SEG_MS)
    ap.add_argument("--typewriter", nargs="?", const="auto", default="off",
                    choices=["auto", "always"],
                    help="打字机效果：auto=来得及才打字 always=总是打字（不给这个参数就是关）")
    ap.add_argument("--analyze", action="store_true",
                    help="分析这份字幕本身是不是增量/滚动（自带打字机/横移）式，分析完退出")
    ap.add_argument("--merge-incremental", action="store_true",
                    help="把 我/我要/我要玩 这种增量碎片合并成整句，再交给播放器自己演")
    ap.add_argument("--type-step", type=int, default=DEFAULT_TYPE_STEP_MS,
                    help="打字机每个字最短间隔毫秒，默认 %d" % DEFAULT_TYPE_STEP_MS)
    ap.add_argument("--no-marquee", dest="marquee", action="store_false", default=True,
                    help="关掉超长句横移（默认开：超长句滚动显示，不切段）")
    ap.add_argument("--marquee-step", type=int, default=DEFAULT_MARQUEE_STEP_MS,
                    help="横移每格最短间隔毫秒，默认 %d" % DEFAULT_MARQUEE_STEP_MS)
    ap.add_argument("--start", default=None, help="从第几秒开始（1:23 或 83）")
    ap.add_argument("--offset", default="0",
                    help="字幕时间偏移：auto=把第一句对齐到 0（字幕跟视频对不上时用）；"
                         "也可填 -1:32:35 / +2.5")
    ap.add_argument("--speed", type=float, default=1.0, help="倍速（测试用，2 就是 2 倍速发送）")
    ap.add_argument("--dry-run", action="store_true", help="不开串口，只打印")
    ap.add_argument("--list", action="store_true", help="列出切分后的事件表，不发送")
    ap.add_argument("--list-ports", action="store_true", help="列出本机串口后退出")
    ap.add_argument("--no-wait", action="store_true", help="不等回车，直接开始")
    args = ap.parse_args(argv)

    if args.list_ports:
        ports = list_ports()
        if not ports:
            print("没有找到串口")
        for port, desc in ports:
            print("%-8s %s" % (port, desc))
        return 0

    sub_path = args.file or find_subtitle(SCRIPT_DIR)
    if not sub_path or not os.path.exists(sub_path):
        print("找不到字幕文件（.srt / .lrc），用 --file 指定")
        return 2

    cues_raw = load_subtitles(sub_path)
    if not cues_raw:
        print("字幕文件里没解析出内容：%s" % sub_path)
        return 2
    print("字幕: %s (%d 句)" % (os.path.basename(sub_path), len(cues_raw)))

    # 先看看这份字幕本身是不是"动画字幕"
    ana = analyze_subtitles(cues_raw)
    if ana["animated"] or ana["advice"]:
        print("字幕类型: %s（相邻句里 %s %d 处 / %s %d 处 / 时间相接的动画 %d 处，占 %.1f%%）"
              % (ana["verdict"], KIND_LABELS["grow"], ana["counts"]["grow"],
                 KIND_LABELS["scroll"], ana["counts"]["scroll"],
                 ana["animated"], ana["ratio"] * 100))
        if ana["advice"]:
            print("建议: %s" % ana["advice"])
    if args.analyze:
        print("相邻句关系统计: " + " / ".join(
            "%s %d" % (KIND_LABELS[k], v) for k, v in ana["counts"].items() if v))
        for idx, t1, t2, r in ana["sample"]:
            print("  第 %d 句: %-24s -> %-24s [%s%s]"
                  % (idx + 1, t1[:24], t2[:24], KIND_LABELS[r["kind"]],
                     (" 新增" + r["delta"]) if r["delta"] and r["kind"] == "grow" else
                     (" 左移%d字" % r["shift"]) if r["shift"] else ""))
        return 0

    if args.merge_incremental:
        before = len(cues_raw)
        cues_raw, away = merge_incremental(cues_raw)
        print("增量合并: %d 句 -> %d 句（合并掉 %d 句碎片）" % (before, len(cues_raw), away))

    off = resolve_offset(args.offset, cues_raw, 0)
    cues = shift_cues(cues_raw, off)
    if off:
        print("时间偏移: %+d ms —— 第一句 %s -> %s（剩 %d 句）"
              % (off, _fmt_ms(cues_raw[0][0]), _fmt_ms(cues[0][0]) if cues else "-", len(cues)))
    elif cues_raw[0][0] > 30000:
        print("提示: 字幕第一句在 %s 才开始，前面不会有字幕；想整体提前就加 --offset auto"
              % _fmt_ms(cues_raw[0][0]))

    logs = []
    width = args.width_fw * 2 if args.width_fw else args.width
    events = build_events(cues, width, args.mode, args.min_seg,
                          log=lambda m: logs.append(m),
                          typewriter=args.typewriter,
                          type_min_step_ms=args.type_step,
                          baud=args.baud,
                          marquee=args.marquee,
                          marquee_min_step_ms=args.marquee_step)
    n_split = len({e["cue"] for e in events if e["nseg"] > 1})
    n_trunc = len({e["cue"] for e in events if e["truncated"]})
    n_type = len({(e["cue"], e["seg"]) for e in events if e.get("typing")})
    n_marq = len({e["cue"] for e in events if e.get("scroll")})
    print("事件: %d 条（分段 %d 句 / 截断 %d 句 / 逐字打字 %d 段 / 横移 %d 句）"
          "屏幕宽度 %d 槽（全角 %d 字）%s%s"
          % (len(events), n_split, n_trunc, n_type, n_marq, width, width // 2,
             "，打字机=%s" % args.typewriter if args.typewriter != "off" else "",
             "，横移开" if args.marquee else "，横移关"))
    for m in logs[:10]:
        print("  " + m)

    if args.list:
        for ev in events:
            flag = ("分段%d/%d" % (ev["seg"] + 1, ev["nseg"])) if ev["nseg"] > 1 else \
                   ("截断" if ev["truncated"] else "整句")
            print("  %8.3fs %-10s [%2d槽] %s"
                  % (ev["t"] / 1000.0, flag, text_slots(ev["text"]), ev["text"]))
        return 0

    screen = LEDScreen(args.com, args.baud, args.dry_run or args.list,
                       log=lambda m: print("  " + m), addr=int(args.addr, 16))
    if not args.dry_run:
        try:
            screen.open()
        except OSError as e:
            print("打开串口失败：%s" % e)
            return 3
    else:
        print("干跑模式：不打开串口")
        screen.open()

    if not args.no_wait:
        input("按回车开始（先倒计时 %d 秒）..." % args.countdown)
    for i in range(args.countdown, 0, -1):
        print("  %d..." % i, end="", flush=True)
        time.sleep(1)
    if args.countdown:
        print()

    engine = SubtitleEngine(events, screen)
    start_at = parse_start(args.start) * 1000.0
    speed = args.speed if args.speed > 0 else 1.0
    t0 = time.perf_counter()
    print("开始！(t=%.1fs, %.2fx)" % (start_at / 1000.0, speed))
    try:
        while True:
            t = start_at + (time.perf_counter() - t0) * 1000.0 * speed
            engine.tick(t)
            if events and t > events[-1]["end"] + 1000:
                break
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        screen.close()
    print("发送完成，共 %d 条" % screen.count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
