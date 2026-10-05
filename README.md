# LED 字幕播放器 · LED Subtitle Player

放视频，把字幕按屏幕宽度重新排版，通过串口一行一行打到 LED 点阵屏上。
典型用途是自己买的车载 LED 屏 / 公交车报站屏：
**24 槽位 = 全角 12 字 / 半角 24 字**，走的是**凯伦3G 协议**里的
**「临时用语」指令**（屏幕收到就整屏替换显示，所以能按毫秒级节奏连发）。实测屏：海信车载屏。

> **Windows + Python 3.8+**，视频用本机的 `ffplay` 播放，不装第三方 Python 包（串口是直接调 Win32 API）。
> 协议和排版逻辑跟系统无关，想搬到 Linux 只要把串口那几十行换掉。

## 特性

- **字幕跟着视频真实进度走**：时钟取自 ffplay 报的播放位置，暂停 / 快进 / 拖进度条 / 变速都不会越放越偏
- **宽度按槽位精确算**：全角 2 槽、半角 1 槽；一行多少全角字自己填（默认 12 字 = 24 槽，也可以填 9 = 18 槽）
- **超长句三种玩法**：分段（按时间中点切） / 横移（先打满一行再往左滚） / 截断
- **逐字出现（打字机）**：`我` → `我要` → `我要玩` → `我要玩原神`
- 全部**可开关、可调**，播放中改也**立刻生效**（自动重排）
- **干跑模式**：不接屏幕也能在界面里看 LED 预览 + 发送日志
- 视频放完自动停；串口掉了会自动报错并重连，不会把播放循环搞死

## 快速开始

```bat
git clone https://github.com/Salt1145/Busled-subtitle-player.git
```

1. 装好 [ffmpeg](https://www.gyan.dev/ffmpeg/builds/)（`winget install Gyan.FFmpeg`），确认 `ffplay`、`ffprobe` 在 PATH 里
2. 把视频（`.mp4` 等）和字幕（`.srt` / `.lrc`）放到本目录，程序会自动找到
3. 双击 **`start_player.cmd`**（或 `python led_player.py`）
4. **不会立刻开始播**，先出配置窗口：

| 配置项 | 说明 |
| --- | --- |
| 视频 / 字幕 | 默认自动选中目录里的 |
| 串口 (COM) / 波特率 | 默认 COM8 / 19200，可刷新列表；先点「测试串口（发 READY）」确认线通 |
| 倒计时（秒） | 点开始后倒数几秒再放；勾了「视频全屏开始」时倒计时是**整屏黑屏大数字** |
| 每行宽度 | **填全角字数**（12 = 24 槽，9 = 18 槽），界面实时换算 |
| 逐字出现 | 关 / 自动（来得及才打字）/ 总是（用满时间硬打） |
| 每字最短间隔 | 自动模式下低于这个间隔就整句发送（默认 80ms） |
| 超长横移 | 超长句横向滚动，不切段（默认开） |
| 分段策略 | 自动 / 总是分段 / 只截断（横移关掉时才用得上） |
| 每段最短显示 | 默认 **0 = 不限制**（实测屏幕反应得过来）；填 400 就是"太短就截断"的老行为 |
| 干跑模式 | 不连串口，只在界面里预览 |
| 从第几秒开始 | 填 `0` / `83` / `1:23` |

播放界面：进度条可拖、暂停/继续、±10 秒、从头播、视频全屏、停止、LED 24 槽位实时预览、
发送日志、手动发一条文本、清屏。快捷键：`空格` 暂停继续、`←` `→` ±10 秒、`Esc` 停止。

> 视频窗口是独立的 ffplay 窗口（不是嵌进去的），按 `f` 或界面上的「视频全屏」可全屏。

## 宽度：一切按"槽位"算

| 单位 | 换算 |
| --- | --- |
| 全角（中文、全角标点） | 2 槽 |
| 半角（英文、数字、半角符号） | 1 槽 |
| **一行** | 默认 24 槽 = 全角 12 字 = 半角 24 字 |

界面上按**全角字数**填（更符合看屏幕的习惯），内部一律换算成槽位，等于 GBK 编码后的字节数——
也就是屏幕固件眼里的长度。中英混排也不会把全角字切一半。

## 超长句怎么处理

超过一行的句子，按「超长横移」开关走两条路：

**① 横移（默认开）**：先把字一个一个打满一行，再整行一格一格往左滚，直到尾巴露出来。
`我要玩原神明朝攫取零啊啊啊啊杀杀杀`（17 字，一行 12 字）实际发出去的样子：

```
我 / 我要 / …（打字阶段，省略重复内容）
我要玩原神明朝攫取零啊啊     <- 打满一行
要玩原神明朝攫取零啊啊啊     <- 开始往左滚
玩原神明朝攫取零啊啊啊啊
原神明朝攫取零啊啊啊啊杀
神明朝攫取零啊啊啊啊杀杀
明朝攫取零啊啊啊啊杀杀杀     <- 尾巴露出来，停住
```

**② 分段（横移关掉时）**：按 `ceil(总槽位 / 宽度)` 切段，时间均分——
2 段就后半句落在时间中点，3 段落在 1/3、2/3；优先在空格标点处断开。时间实在来不及就往后截断。

## 打字机：为什么有些句子没打字？

因为**打字机是把前缀反复重发**，线路上跑的是 `1+2+3+…+n` 个字符，**平方级开销**。19200 波特率实测：

| 句子 | 打完需要的线路时间 |
| --- | --- |
| `我要玩原神`（5 字 / 10 槽） | 47 ms |
| `>即使在我歌唱得不好之时`（12 字 / 23 槽） | 150 ms |
| `A stop job is running for miku (0min 6s/4min 0s)`（48 槽） | 912 ms |

「自动」档要求两件事同时满足才打字：① 每字间隔 ≥「每字最短间隔」 ② 整串帧的线路时间装得进这段显示时间。
不满足就整句发，日志里会写原因：`打字机跳过（这根线 19200 波特全速也要 912ms，这段只有 800ms）`。

想让它更多时候能打字 / 能滚：**提高波特率**（38400 / 57600 / 115200，线路时间直接除以 2/3/6，
记得屏幕那边也要设成一样）→ 调小「每字最短间隔」→ 或者选「总是」硬打。

## 不放视频、只发字幕（命令行）

```bat
python lrc2com8.py --list-ports                 :: 看有哪些串口
python lrc2com8.py --dry-run --list             :: 只列出切分结果，不开串口
python lrc2com8.py --com COM8 --baud 19200 --countdown 5
python lrc2com8.py --addr 01                    :: 指定屏号（默认 FF 广播）
python lrc2com8.py --width-fw 9                 :: 每行按全角 9 字 = 18 槽位
python lrc2com8.py --typewriter auto            :: 打字机（来得及才打）
python lrc2com8.py --typewriter always          :: 打字机（总是打）
python lrc2com8.py --no-marquee                 :: 关掉横移（回到切段/截断）
python lrc2com8.py --mode truncate --min-seg 400:: 只截断不分段
python lrc2com8.py --speed 20 --dry-run         :: 20 倍速跑一遍，检查时间轴
python lrc2com8.py --start 1:23                 :: 从 1 分 23 秒开始发
```

## 串口协议：凯伦3G「临时用语」（想适配别的屏看这里）

**凯伦3G** 车载 LED 屏协议，这里用的是其中的**临时用语**指令（实测屏：海信车载屏）。
一帧就是一个包，GBK 编码，没有换行符：

```
7E <addr> 01 05 0B 00 <len> 07 00 <n> <文本 GBK> <XOR> 7F
```

| 字节 | 含义 |
| --- | --- |
| `7E` / `7F` | 包头 / 包尾 |
| `addr` | 屏号（地址），`FF` = 广播（默认）。一车多屏时可以指定，命令行 `--addr 01` |
| `01 05 0B 00` | 固定头，照抄实测值（用途没去逆，反正屏认） |
| `len` | `n + 3` |
| `07` | **命令字：临时用语** |
| `00` | 固定 `00` |
| `n` | 文本字节数（也就是槽位数） |
| `文本` | GBK 编码 |
| `XOR` | 从 `7E` 开始逐字节异或 |

代码就是 [`karen3g_packet()`](led_screen.py) 那 20 行（`karen_packet` / `hisense_packet` 是别名）。
**换个牌子的屏**：同族 `7E … 7F` 帧格式的报站屏一般能用，但命令字和固定头可能不一样，
先拿屏的说明书核对 `07` 这一位（有的协议里 `07` 是别的含义），改这一行就行。

屏幕不支持的字（GBK 编不出来的）会被替换成 `?`，保证槽位算得准、不会发出半个字。
自检里会拿最早那版脚本的算法**逐字节对拍**，确认字节流没被改坏。

> 临时用语是"插一条话"的指令，屏幕收到就整屏替换，所以这里可以按毫秒级节奏连发（打字机 / 横移就是靠这个）。
> 如果你的屏还要显示报站信息，注意临时用语的显示时长/退出方式，别把正常报站顶掉。

## 自检（不接屏幕也能跑）

```bat
python led_screen.py --selftest     :: 槽位计算 / 协议字节对拍 / 分段 / 打字机 / 横移 / 引擎
python selftest_player.py           :: 整个播放器跑一遍（不弹视频窗口、不出声、不连串口）
```

`selftest_player.py` 找不到素材时会自己生成演示字幕，并用 ffmpeg 造一段 30 秒测试视频。

## 目录结构

```
led_player.py        播放器：配置窗口 + 播放界面 + 字幕发送
led_screen.py        核心库：串口协议、槽位计算、分段/打字/横移、时间轴引擎
lrc2com8.py          纯字幕发送工具（命令行，不放视频）
selftest_player.py   播放器自检
start_player.cmd     双击启动
examples/demo.srt    演示字幕（自己写的，20 多秒，覆盖各种排版情况）
led_player.ini       程序自动生成，记住上次设置
```

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 提示没有 ffplay | 装 ffmpeg，确认 `ffplay` 在 PATH 里 |
| 串口打不开 | 没插 / 被别的程序占用（串口助手、Arduino IDE…）；点「刷新串口」重选；先勾「干跑」验证排版 |
| 屏幕全是乱码 | 波特率不对（试 9600 / 19200 / 38400），或屏幕那边没设成一样 |
| 打字/横移没生效 | 看日志里 `打字机跳过（…）` / `横移来不及（…）` 的原因；多半是波特率太低，或者把「每字最短间隔」调小、选「总是」 |
| 字幕整体偏早/偏晚 | 字幕文件自己的时间轴问题，跟播放器无关（可用 `--speed 20 --dry-run` 快速核对） |

## 免责声明

- 本项目只用于**自己拥有的屏幕和设备**。公交车上的报站屏属于车载设备，
  请不要去改动运营车辆上的设备，不要影响报站和行车安全。
- 仓库不包含任何视频、音乐、字幕素材；`examples/demo.srt` 是项目自己写的演示文本。

## License

[MIT](LICENSE) © 2026 Salt1145 —— 随便用、随便改、随便商用，**保留版权声明和许可声明**就行。
每个源文件顶部也带了一行 `SPDX-License-Identifier: MIT`，单个文件被拷走时声明不会丢。

---

## English (short)

A Windows LED-matrix subtitle player for vehicle LED panels / bus stop displays speaking the
**Karen 3G (凯伦3G)** protocol: plays a video with `ffplay`, re-flows each subtitle line to fit the
panel (full-width char = 2 slots, half-width = 1 slot, default 12 full-width chars per line), and
pushes the text over serial using the protocol's **temporary-phrase command**
(`7E <addr> 01 05 0B 00 <len> 07 00 <n> <GBK text> <XOR> 7F`, tested on a Hisense panel).
Subtitle timing is driven by ffplay's real playback clock, so pause / seek / speed changes stay in
sync. Features typing effect, horizontal marquee for over-long lines, and time-based segmentation.
No third-party Python packages (serial via Win32 API). The serial protocol and layout logic are OS
independent — only the serial layer is Windows-only.

```bat
python led_screen.py --selftest     :: protocol + layout + engine self test
python selftest_player.py           :: full player dry-run self test
python lrc2com8.py --dry-run --list :: just print what would be sent
```
