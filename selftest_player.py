# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Salt1145
# https://github.com/Salt1145/Busled-subtitle-player
"""
selftest_player.py —— 播放器自检（不弹视频窗口、不出声、不连串口）

干什么：
    把 led_player 跑一遍：解析字幕 -> 干跑发字幕 -> 手动发送 -> 清屏 -> 改宽度 ->
    改打字机 -> 改横移 -> 拖进度 -> 暂停 -> 返回设置 -> 重新开始。
    全过程 ffplay 用 -nodisp -volume 0 启动，所以不会弹视频窗口、也没有声音。
    最后打印日志，用来确认"字幕有没有按时间发对"。

素材怎么来：
    1. 优先用目录里现成的视频 + 字幕（没有就找 examples/demo.srt）
    2. 都没有：自动生成一个演示字幕，并用 ffmpeg 生成一段 30 秒测试视频
    3. 连 ffmpeg 都没有：只跑字幕时间轴那部分（不开播放器界面）

用法：
    python selftest_player.py
"""

import os
import shutil
import subprocess as sp
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import led_player                                        # noqa: E402
from led_player import LedPlayerApp                       # noqa: E402
from led_screen import (SubtitleEngine, build_events, find_subtitle,  # noqa: E402
                        find_video, LEDScreen, load_subtitles)

DEMO_SRT = """1
00:00:00,500 --> 00:00:03,000
字幕会按屏幕宽度自动重排

2
00:00:03,000 --> 00:00:07,000
这一行超过一屏，所以会先逐字打满一行再一格一格横移滚动

3
00:00:07,000 --> 00:00:10,000
A stop job is running for this led panel (0min 0s/4min 0s)

4
00:00:10,000 --> 00:00:11,200
短句

5
00:00:11,200 --> 00:00:14,000
自己买的屏幕，自己写协议，自己发字幕

6
00:00:14,000 --> 00:00:17,500
逐字出现就是 我 / 我要 / 我要玩 / 我要玩原神 这样一个个蹦出来

7
00:00:17,500 --> 00:00:20,000
:: 宽度、速度、开关都能调

8
00:00:20,000 --> 00:00:23,000
[led@bus ~]$ echo "hello led" > /dev/ttyUSB0

9
00:00:23,000 --> 00:00:26,000
超长句横移：一个字一个字往左滚，直到尾巴露出来为止

10
00:00:26,000 --> 00:00:29,000
MIT 协议，随便改
"""


# --- ffplay 打桩：把视频和声音都关掉，只留播放时钟 -------------------------

REAL_POPEN = sp.Popen


class ShimModule:
    """把 led_player 里用的 subprocess 换掉，给 ffplay 塞 -nodisp -volume 0"""

    PIPE = sp.PIPE
    DEVNULL = sp.DEVNULL
    STDOUT = sp.STDOUT
    run = staticmethod(sp.run)
    call = staticmethod(sp.call)

    @staticmethod
    def Popen(cmd, **kw):
        if cmd and os.path.basename(str(cmd[0])).lower().startswith("ffplay"):
            cmd = [cmd[0], "-nodisp", "-volume", "0"] + list(cmd[1:])
        return REAL_POPEN(cmd, **kw)


def ensure_inputs():
    """返回 (视频路径 或 None, 字幕路径)"""
    sub = find_subtitle(HERE) or find_subtitle(os.path.join(HERE, "examples"))
    if not sub:
        sub = os.path.join(HERE, "_selftest_demo.srt")
        if not os.path.exists(sub):
            with open(sub, "w", encoding="utf-8") as f:
                f.write(DEMO_SRT)

    video = find_video(HERE)
    if video:
        return video, sub
    video = os.path.join(HERE, "_selftest_demo.mp4")
    if os.path.exists(video):
        return video, sub
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None, sub
    print("没有现成视频，用 ffmpeg 生成 30 秒测试片段…", flush=True)
    for vcodec in ("libx264", "mpeg4"):
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", "testsrc=size=480x270:rate=15",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
               "-t", "30", "-c:v", vcodec]
        if vcodec == "libx264":
            cmd += ["-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "aac", "-shortest", video]
        try:
            if sp.run(cmd, timeout=180).returncode == 0 and os.path.exists(video):
                return video, sub
        except Exception:
            pass
    return None, sub


def probe_duration(video):
    ffprobe = shutil.which("ffprobe")
    if not (ffprobe and video):
        return 0.0
    try:
        out = sp.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                      "-of", "csv=p=0", video], capture_output=True, text=True, timeout=20)
        return float((out.stdout or "0").strip().splitlines()[0])
    except Exception:
        return 0.0


def headless_engine_test(cues):
    """没有视频可用时：只验证"时间 -> 发什么字"这条链路"""
    print("跳过播放器界面测试，只跑字幕时间轴（干跑）：", flush=True)
    events = build_events(cues, 24, "auto", 0, typewriter="auto", marquee=True)
    screen = LEDScreen(dry_run=True, log=lambda m: print("   ", m))
    eng = SubtitleEngine(events, screen)
    end = int(events[-1]["end"]) if events else 0
    for t in range(0, min(end, 30000), 20):
        eng.tick(t)
    print("共发送 %d 条（事件 %d 条）" % (screen.count, len(events)))


def main():
    video, sub = ensure_inputs()
    print("视频: %s" % (video or "（无，跳过界面测试）"))
    print("字幕: %s" % sub, flush=True)

    cues = load_subtitles(sub)
    if not cues:
        print("字幕解析失败")
        return 2
    if not video:
        headless_engine_test(cues)
        return 0

    led_player.subprocess = ShimModule()
    led_player.INI_PATH = os.path.join(HERE, "_selftest.ini")   # 别写用户的配置

    dur = probe_duration(video)
    start_s = min(60.0, max(1.0, dur - 20.0)) if dur else 1.0
    run_s = 15.0

    import tkinter as tk
    root = tk.Tk()
    app = LedPlayerApp(root, [])
    app.var_video.set(video)
    app.var_sub.set(sub)
    app.var_dry.set(True)
    app.var_countdown.set("0")
    app.var_start.set("%g" % start_s)
    app.var_mode.set("auto")
    app.var_typewriter.set("自动")
    app.on_typewriter_toggle()

    events = build_events(cues, 24, "auto", 0, typewriter="auto", marquee=True)
    expect = [e for e in events if start_s * 1000 <= e["t"] <= (start_s + run_s) * 1000]
    print("预期 %d 帧落在 [%gs, %gs]" % (len(expect), start_s, start_s + run_s))
    for e in expect[:20]:
        kind = "横移" if e.get("scroll") else ("打字" if e.get("typing") else "整句")
        print("   %8.3f %-12s %s" % (e["t"] / 1000.0,
                                     "%s %d/%d" % (kind, e["frame"] + 1, e["nframe"]),
                                     e["text"]))
    sys.stdout.flush()

    def step(delay, name, fn):
        root.after(int(delay * 1000),
                   lambda: (print("STEP:", name), sys.stdout.flush(), fn()))

    step(0.5, "start", app.on_start_clicked)
    step(5.0, "manual send", lambda: (app.var_manual.set("手动测试 OK"), app.on_manual_send()))
    step(6.0, "clear screen", app.on_clear_screen)
    step(6.5, "typewriter 关", lambda: (app.var_typewriter.set("关"), app.on_typewriter_toggle()))
    step(7.5, "typewriter 总是", lambda: (app.var_typewriter.set("总是"), app.on_typewriter_toggle()))
    step(8.5, "width 9 全角字", lambda: (app.var_width_fw.set("9"), app.on_width_change()))
    step(9.0, "横移 关", lambda: (app.var_marquee.set(False), app.on_marquee_toggle()))
    step(9.5, "横移 开", lambda: (app.var_marquee.set(True), app.on_marquee_toggle()))
    step(10.0, "restart from head", app.on_restart)
    step(11.0, "seek to 70%", lambda: (app.var_scale.set(max(1, app.duration_ms) * 0.7),
                                       app.on_scale_press(None), app.on_scale_release(None)))
    step(12.0, "pause button", app.on_pause)
    step(12.5, "back to config", app.on_back_to_config)
    step(13.0, "start again", app.on_start_clicked)

    def dump():
        print("\n----- 日志（尾部） -----")
        try:
            lines = app.txt_log.get("1.0", "end").strip().splitlines()
        except Exception as ex:
            lines = ["<读不到日志 %s>" % ex]
        for line in lines[-18:]:
            print(line)
        print("----- 发送条数=%s 状态=%s -----" % (app.send_count, app.lbl_status.cget("text")))
        print("LED 预览: [%s]" % app.lbl_led.cget("text"))
        print("LED 信息: %s" % app.lbl_led_info.cget("text"))
        sys.stdout.flush()
        app.on_quit()

    root.after(int((run_s + 0.5) * 1000), dump)
    root.mainloop()
    print("自检结束：上面没有报错（Exception / 异常堆栈）就是通过了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
