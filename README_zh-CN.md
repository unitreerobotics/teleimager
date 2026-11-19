<div align="center">
  <img src="teleimager_logo.png" alt="Teleimager Logo" width="100%">
  <a href="https://www.unitree.com/" target="_blank">
    <img src="src/teleimager/unitree_logo.svg" alt="Unitree LOGO" width="15%">
  </a>
  <p align="center">
    <a href="README.md"> English</a> | <a>中文</a>
  </p>
  <p align="center">
    <a href="https://github.com/unitreerobotics/xr_teleoperate/wiki" target="_blank"> <img src="https://img.shields.io/badge/GitHub-Wiki-181717?logo=github" alt="Unitree LOGO"></a> <a href="https://discord.gg/ZwcVwxv5rq" target="_blank"><img src="https://img.shields.io/badge/-Discord-5865F2?style=flat&logo=Discord&logoColor=white" alt="Unitree LOGO"> <a href="https://deepwiki.com/unitreerobotics/teleimager"><img src="https://img.shields.io/badge/Ask-DeepWiki-4B8BBE" alt="Ask DeepWiki"></a> </a>
  </p>
</div>

[**TeleImager**](https://github.com/unitreerobotics/teleimager) 是宇树（Unitree）的图像服务：**服务端**从多路摄像头（UVC、V4L2、GStreamer、RealSense等）采集视频，并通过 **ZeroMQ** 或 **WebRTC** 发布到网络；**客户端**订阅并解码这些视频流。它为 [xr_teleoperate](https://github.com/unitreerobotics/xr_teleoperate) 提供遥操作视频链路。

## ✨ 特性

- 📸 支持多路 UVC / V4L2 / GStreamer / Intel RealSense 摄像头
- 📢 通过 **ZeroMQ PUB-SUB**（局域网高质量）与 **WebRTC**（低延迟、浏览器）发布视频帧
- 🌊 通过 ZeroMQ 发布与彩色图对齐的 RealSense 深度图（zstd 压缩的 z16）
- 💬 通过 **ZeroMQ REQ-REP** 响应图像配置指令
- 🆔 五种摄像头识别方式：物理路径、序列号、bcd_device、vid_pid、video 路径
- ⚙️ 可配置分辨率与帧率
- 🚀 使用三重环形缓冲区实现高效帧传递

---

## 1. 📦 安装

TeleImager 分为**两种角色**，按需安装即可：

| 角色 | 作用 | 运行位置 |
|------|------|---------|
| **客户端（Client）** | 订阅并解码视频流（遥操作、数据录制、自定义 CV 代码） | 工作站 / 机器人主机 |
| **服务端（Server）** | 从摄像头采集并通过 ZMQ/WebRTC 发布 | 摄像头所连接的机器人设备 |

### 1.0 系统前置依赖（两种角色都需要）

两种角色都通过 **PyTurboJPEG** 做 JPEG 编解码。它是一个 ctypes 绑定,在 import 时加载系统库 **libjpeg-turbo(要求 3.0+)**。这个原生库 pip 无法安装,每台机器安装一次即可。三选一:

**方法 1 —— Conda(推荐,跨平台)**

```bash
conda install -c conda-forge libjpeg-turbo
```

**方法 2 —— Homebrew(macOS)**

```bash
brew install jpeg-turbo
```

<details>
<summary><b>方法 3 —— 从源码编译(Ubuntu/Debian)</b></summary>

```bash
git clone https://github.com/libjpeg-turbo/libjpeg-turbo.git
cd libjpeg-turbo && mkdir build && cd build
cmake -DCMAKE_INSTALL_PREFIX=/opt/libjpeg-turbo ..
make -j$(nproc) && sudo make install
echo 'export LD_LIBRARY_PATH=/opt/libjpeg-turbo/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc
```

</details>

> ⚠️ **不要**用 PyPI 上那个名为 `turbojpeg` 的包——它是另一个不兼容的库；本项目用的是 `PyTurboJPEG`(已随 `pip install teleimager` 装好)。
> Ubuntu 的 `sudo apt install libturbojpeg` 版本常低于 3.0,因此不推荐。若该库缺失,TeleImager 会在首次 import 时抛出与上面等价的详细安装提示。

### 1.1 环境准备（conda）

```bash
# 安装 Miniconda（已装可跳过）。Jetson/ARM 请用 aarch64 安装包。
mkdir -p ~/miniconda3
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda3/miniconda.sh
bash ~/miniconda3/miniconda.sh -b -u -p ~/miniconda3 && rm ~/miniconda3/miniconda.sh
source ~/miniconda3/bin/activate && conda init --all

# 创建并激活项目环境
conda create -n teleimager python=3.10 -y
conda activate teleimager
```

### 1.2 客户端安装

| 需求 | 命令 |
|------|------|
| **无界面（headless）** —— 订阅 ZMQ → BGR `ndarray`（喂给你自己的代码） | `pip install teleimager` |
| **+ 可视化窗口** —— OpenCV 开窗显示 | `pip install "teleimager[viewer]"` |

> 可视化会拉入 `opencv-python`

### 1.3 服务端安装

先按需选后端，再执行下面的安装命令（用 WebRTC 的话最后配一次证书）。

#### 1.3.1 选择后端（摄像头驱动）

「后端」就是 TeleImager 采集某类摄像头所用的驱动，按需叠加：

| 后端（摄像头驱动） | pip 选项 | 说明 |
|------|---------|------|
| 基座（V4L2） | `[server]` | 需要 `v4l2-ctl`：`sudo apt install -y v4l-utils` |
| + UVC | `[uvc]` | 需 USB 权限——安装后执行一次 `sudo "$(which teleimager-server)" --setup-permissions`；wheel 已自带 `libturbojpeg`、`libusb`，无需 apt |
| + RealSense | `[realsense]` | 发布深度图需再加 `[depth]`（zstandard） |
| + 全部（不含 GStreamer） | `[all]` | 等价于 UVC + RealSense + depth |
| + GStreamer | 无 pip 包 | PyGObject + 系统插件——见表格下方的引用块 |

> `driver_type: gstreamer` 是唯一没有 pip extra 的后端：PyGObject 必须跟解释器走，GStreamer 本体来自系统。
>
> ```bash
> conda install -c conda-forge pygobject   # 即 1.1 建的 env；apt 的 python3-gi 只服务系统 python
> sudo apt install gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good
> ```
>
> pipeline 要解 H.264/H.265 再加 `gstreamer1.0-libav`（`avdec_h264`），要硬件解码加 `gstreamer1.0-plugins-bad`；`v4l2src` 和 MJPG-over-RTP 只要 base + good。用 `python -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; print(Gst.version_string())"` 确认。
>
> **它用来做什么。** 普通 USB 相机用 V4L2 或 UVC 就够了，优先走那两条路。GStreamer 的价值在于接入前两者够不着的相机：嵌入式板上的 GMSL2 / MIPI 模组（如 `nvarguscamerasrc`），以及网络相机（`udpsrc`、RTSP）。这也是该后端接收的是一整条 `gst-launch` pipeline 字符串、而不是设备路径的原因。
>
> TeleImager 从 appsink 取出的是 JPEG——ZMQ 发布端原样转发这些编码字节，只有 WebRTC 才需要解码——所以 pipeline 末端必须输出 JPEG：`... ! image/jpeg ! appsink name=sink`。本身已输出 JPEG 的源（`v4l2src` 的 MJPG、JPEG 的 RTP 流）无需额外处理；输出裸数据的源则要在 appsink 前加 `videoconvert ! jpegenc`（或硬件编码器，如 `nvjpegenc`）。

#### 1.3.2 安装

选好后端（把下面的 `[server]` 换成对应选项），二选一执行安装：

```bash
# 方式一：从源码（推荐）
git clone https://github.com/unitreerobotics/teleimager.git
cd teleimager
pip install -e ".[server]"        # 如 ".[uvc]"、".[all]"

# 方式二：从 PyPI
pip install "teleimager[server]"  # 如 "teleimager[uvc]"、"teleimager[all]"
```

#### 1.3.3 授予相机访问权限（仅 UVC 需要）

V4L2 开箱即用（`/dev/video*` 带 logind 给的 ACL）；USB video class 不行——节点保持 root 所有，`libusb_open()` 打开 `/dev/bus/usb/...` 会 EACCES。执行一次即可（它写一条 udev 规则、一条 sudoers 条目，并把你加入 `video` 组）：

```bash
sudo "$(which teleimager-server)" --setup-permissions   # $(which ...) 才能在 sudo 的 secure_path 下找到
```

跳过的话，服务端启动时会提示你并给出同一条命令。执行完之后 SSH 需重连，桌面终端需重新登录。

#### 1.3.3 配置 TLS 证书（仅 WebRTC）

详情参见 [CA 说明](https://github.com/unitreerobotics/xr_teleoperate/wiki/CA)。也可用 openssl 快速生成一份自签名证书：

```bash
# 生成一份有效期 365 天的自签名证书（cert.pem / key.pem）
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout key.pem -out cert.pem -subj "/CN=teleimager"
```

生成后，用以下任一方式让 TeleImager 找到它：

```bash
# 方式 A —— 默认配置目录（推荐，与服务端 yaml 同一目录）
mkdir -p ~/.config/teleimager/
cp cert.pem key.pem ~/.config/teleimager/

# 方式 B —— 在命令行指定任意路径
teleimager-server --cert /路径/cert.pem --key /路径/key.pem
```

---

## 2. 🚀 运行服务端

### 2.1 查找已连接的摄像头

按你拥有的摄像头加上对应后端标志扫描（`--uvc`、`--v4l2`、`--gst`、`--rs`，可自由组合）：

```bash
teleimager-server --cf --uvc --v4l2        # RealSense 加 --rs，GStreamer 加 --gst
```

输出示例：

```text
[Teleimager] 🎯 Camera Finder Report
├─ UVCCamera (3 found)   [driver_type: uvc]
│  ├─ 📷 USB HDR Camera (Generic)
│  │  ├─ physical_path : /sys/devices/pci0000:00/0000:00:14.0/usb1/1-3/1-3.1/1-3.1:1.0
│  │  ├─ serial_number : 200901010001
│  │  ├─ bcdDevice     : 0200           (USB device release number)
│  │  ├─ vid : pid     : 1e45:2050      (VendorID : ProductID)
│  │  ├─ video_id      : 0              (/dev/video0)
│  │  └─ modes (MJPG)  [width x height @ fps]:
│  │     ├─ 320x240 @ [30, 60]
│  │     ...
│  │     └─ 1920x1080 @ [30, 60]
│  ├─ 📷 Abham Image (HHWei Technology Co., Ltd.)
│  │  ├─ physical_path : /sys/devices/pci0000:00/0000:00:14.0/usb1/1-1/1-1:1.0
│  │  ├─ serial_number : HHW001
│  │  ├─ bcdDevice     : 0200           (USB device release number)
│  │  ├─ vid : pid     : 1c45:6200      (VendorID : ProductID)
│  │  ├─ video_id      : 10             (/dev/video10)
│  │  └─ modes (MJPG)  [width x height @ fps]:
│  │     ├─ 640x480 @ [5, 10, 15, 20, 25, 30]
│  │     ...
│  │     └─ 2688x1520 @ [5, 10, 15, 20, 25, 30]
│  └─ 📷 Cherry Dual Camera (DECXIN)
│     ├─ physical_path : /sys/devices/pci0000:00/0000:00:14.0/usb1/1-3/1-3.2/1-3.2:1.0
│     ├─ serial_number : 01.00.00
│     ├─ bcdDevice     : 0217           (USB device release number)
│     ├─ vid : pid     : 1bcf:2d4f      (VendorID : ProductID)
│     ├─ video_id      : 2              (/dev/video2)
│     └─ modes (MJPG)  [width x height @ fps]:
│        ├─ 320x232 @ [10, 15, 20, 25, 30, 60, 120]
│        ├─ 640x240 @ [10, 15, 20, 25, 30, 60, 120]
│        ├─ 800x592 @ [10, 15, 20, 25, 30, 60, 120]
│        ├─ 800x600 @ [10, 15, 20, 25, 30, 60, 120]
│        ├─ 1280x480 @ [10, 15, 20, 25, 30, 60, 120]
|        ...
│        └─ 3200x1296 @ [10, 15, 20, 25, 30, 60]
└─ V4L2Camera (4 found)   [driver_type: v4l2]
   ├─ 📷 USB HDR Camera (Generic)
   │  ├─ physical_path : /sys/devices/pci0000:00/0000:00:14.0/usb1/1-3/1-3.1/1-3.1:1.0
   │  ├─ serial_number : 200901010001
   │  ├─ bcdDevice     : 0200           (USB device release number)
   │  ├─ vid : pid     : 1e45:2050      (VendorID : ProductID)
   │  ├─ video_id      : 0              (/dev/video0)
   │  └─ modes  [width x height @ fps]:
   │     ├─ MJPG   1920x1080 @ [60.0, 30.0]
   ...
   │     └─ YUYV   640x480 @ [30.0]
   ├─ 📷 Abham Image (HHWei Technology Co., Ltd.)
   │  ├─ physical_path : /sys/devices/pci0000:00/0000:00:14.0/usb1/1-1/1-1:1.0
   │  ├─ serial_number : HHW001
   │  ├─ bcdDevice     : 0200           (USB device release number)
   │  ├─ vid : pid     : 1c45:6200      (VendorID : ProductID)
   │  ├─ video_id      : 10             (/dev/video10)
   │  └─ modes  [width x height @ fps]:
   │     ├─ MJPG   2688x1520 @ [30.0, 25.0, 20.0, 15.0, 10.0, 5.0]
   ...
   │     └─ YUYV   640x480 @ [30.0]
   ├─ 📷 Cherry Dual Camera (DECXIN)
   │  ├─ physical_path : /sys/devices/pci0000:00/0000:00:14.0/usb1/1-3/1-3.2/1-3.2:1.0
   │  ├─ serial_number : 01.00.00
   │  ├─ bcdDevice     : 0217           (USB device release number)
   │  ├─ vid : pid     : 1bcf:2d4f      (VendorID : ProductID)
   │  ├─ video_id      : 2              (/dev/video2)
   │  └─ modes  [width x height @ fps]:
   │     ├─ MJPG   3200x1296 @ [60.0, 30.0, 25.0, 20.0, 15.0, 10.0]
   ...
   │     ├─ YUYV   640x240 @ [60.0, 30.0, 25.0, 20.0, 15.0, 10.0]
   │     └─ YUYV   320x232 @ [120.0, 60.0, 30.0, 25.0, 20.0, 15.0, 10.0]
   └─ 📷 Intel(R) RealSense(TM) Depth Camera 435i (Intel(R) RealSense(TM) Depth Camera 435i)
      ├─ physical_path : /sys/devices/pci0000:00/0000:00:14.0/usb1/1-11/1-11.2/1-11.2:1.3
      ├─ serial_number : (none)
      ├─ bcdDevice     : 50d0           (USB device release number)
      ├─ vid : pid     : 8086:0b3a      (VendorID : ProductID)
      ├─ video_id      : 8              (/dev/video8)
      └─ modes  [width x height @ fps]:
         ├─ YUYV   424x240 @ [60.0, 30.0, 15.0, 6.0]
         ├─ YUYV   640x480 @ [30.0, 15.0, 6.0]
         ├─ YUYV   1280x720 @ [15.0, 10.0, 6.0]
         └─ YUYV   1920x1080 @ [8.0]
```

### 2.2 启动服务端

根据 2.1 节的相机搜索报告创建服务端 YAML，输出参数及字段注释见 [streaming_config.yaml](streaming_config.yaml)。例如，使用 RealSense 单目头部且无腕部时，将下面的配置与该文件放在同一目录，并替换设备序列号：

```yaml
streaming: streaming_config.yaml
camera:
  head_mono_camera:
    driver_type: realsense
    image_shape: [480, 640]
    binocular: false
    fps: 30
    serial_number: "YOUR_SERIAL_NUMBER"
    enable_depth_zmq: false
```

每路相机用 `driver_type` 选择采集驱动（`uvc`、`v4l2`、`realsense`、`gstreamer`、`rtp` 或 `isaacsim`，默认 `uvc`），用下表的键选择输出；只有某个输出或 App 推流需要时才打开这路相机：

| 键 | 含义 |
| --- | --- |
| `enable_color_zmq` / `color_zmq_port` | 在该端口以 JPEG 经 ZMQ 发布彩色流 |
| `enable_depth_zmq` / `depth_zmq_port` | 仅 RealSense：采集与彩色图对齐的深度图并在该端口发布；需要 `teleimager[depth]` |
| `enable_webrtc` / `webrtc_port` / `webrtc_codec` | 经 WebRTC 提供彩色流（`h264` 或 `vp8`） |

启用某个输出却没给端口会报错。使用推流文件时，由 `zmq.ports`、`zmq.depth_ports` 和 `webrtc.ports` 提供端口并启用所列话题，`enable_depth_zmq` 仍写在采集配置里。2.1 之前的键 `type`、`enable_zmq`、`zmq_port`、`enable_depth` 会被拒绝，报错信息给出新键名。各输出在相机交来新帧时发布，没有固定频率的定时器；相机停了就什么都不发，而不是重复最后一帧。

深度消息由 12 字节头（小端 `uint32` 彩色帧号、`uint16` 宽、`uint16` 高、`float32` 深度比例，单位米/刻度）和 zstd 压缩的行优先 `uint16` 深度图组成。深度与彩色共用 `fps`；若 RealSense 拒绝这一组合（"cannot stream together"），请降低 `fps`，报错会提示 45 fps 或更低。

启动时显式指定服务端 YAML：

```bash
teleimager-server --config /path/to/server.yaml
```

也可使用 `TELEIMAGER_CONFIG=/path/to/server.yaml teleimager-server`。`--config` 优先于环境变量；两者都未提供时明确报错，不自动创建或隐式读取服务端配置。相机发现（`--cf`）和权限设置（`--setup-permissions`）不要求配置文件。服务端启动时会记录版本（`TeleImager v2.1.0+robotree.2 on Linux x86_64`）。

`--streaming-config <路径>` 选择输出设置，`--no-affinity` 跳过 CPU 核心绑定，`--isaacsim` 从共享内存读取图像。TLS 证书仍默认读取 `~/.config/teleimager/cert.pem` 和 `key.pem`；客户端仍保留最近一次成功获取的配置缓存。

### 2.3 开机自启动

一切验证无误后，可将其安装为 systemd 服务，随机器启动：

```bash
sudo "$(which teleimager-server)" --setup-service --config /path/to/server.yaml
```

它会把 `teleimager.service` 写入 `/etc/systemd/system`，绑定执行该命令的解释器与你的账户（取自 `$SUDO_USER`），并立即启用。服务以该文件的绝对路径作为 `--config` 启动；若同时给出 `--streaming-config`、`--cert`、`--key`，也一并写入。`--config` 必填。改完服务端 YAML 后执行 `sudo systemctl restart teleimager.service`。若在 root shell（`sudo su -`）中执行，需额外加 `--user <名称>`，因为此时 `$SUDO_USER` 为空。

管理命令：`systemctl status / restart / stop / disable teleimager.service`；查看实时日志输出：`journalctl -u teleimager.service -f`。

---

## 3. 📺 运行客户端

### 3.1 ZMQ 方式

服务端运行后，在另一个终端（或另一台机器）启动客户端，指向服务端 IP：

```bash
teleimager-client --host 192.168.123.164        # 默认：192.168.123.164
```

每路摄像头会各自弹出一个 OpenCV 窗口（需要 `teleimager[viewer]`）。

若做无界面订阅，请参照 [`client.py`](src/teleimager/client.py) 中`TeleImageClient` 的 `# public api` 从自己的代码里订阅。传入 `depth_port` 即可同时接收深度图：

```python
from teleimager.client import TeleImageClient

head = TeleImageClient("head_mono_camera", server_host="192.168.123.164",
                       color_port=55560, depth_port=56001, request_bgr=True)
frame = head.get_frame()
if frame.depth is not None:
    meters = frame.depth.depth * frame.depth.depth_scale  # 与彩色图对齐的 uint16 [H, W]
    color_frame_no = frame.depth.frame_no
```

`frame.depth` 是 `DepthPacket`（也可从 `teleimager.client` 导入）或 `None`；其 `compressed` 属性保存收到的 zstd 数据，存盘时无需再次压缩。查看器会在第二个窗口按 0–5 m 着色显示深度图。

### 3.2 WebRTC 方式

在浏览器打开服务端的 WebRTC 页面，点击页面中央的 **start** 按钮：

```text
https://<host_ip>:<webrtc_port>        # 例如 https://192.168.123.164:60001
```

---

## 4. 🧠 设计原理

### 4.1 为什么需要五种摄像头识别方式？

当多个摄像头同时接入时，系统需要可靠的方式区分它们。TeleImager 支持五种标识，按优先级查找：

`physical_path > serial_number > bcd_device > vid_pid > video_id`

每种各有优劣，且厂商对这些字段的使用往往与 USB 规范不一致——字段在规范里的"标准含义"和厂商固件实际写入的值可能完全不同。下表基于实际场景。

| 标识 | 含义 | 🎯 优点 | ⚠️ 缺点 |
|------|------|--------|--------|
| **physical_path** | 内核分配的 USB 拓扑路径——插在哪个物理口 | 只要不换口就稳定；不依赖固件；适合固定部署（头部 + 左右腕） | 换 USB 口就要改配置 |
| **serial_number** | USB 描述符中的字符串，规范上每台唯一 | 换口不变，可移植；规范上就是用来区分单台设备的 | 低成本摄像头可能共用、留空或格式异常 |
| **bcd_device** | BCD 固件版本号；部分厂商按单台设备变化 | 固化在固件中，重启/换口不变；厂商有意时可当序列号用 | 很多厂商同型号所有设备取值相同 |
| **vid_pid** | 16 位 供应商:产品 ID | 极稳定；可一眼区分不同型号 | 同型号通常共享同一 vid_pid |
| **video_id** → `/dev/videoX` | 内核枚举的 V4L2 节点编号 | 最直接：填编号 `X` 即可 | 插拔顺序/重启/枚举顺序都会变——多摄像头下不可靠 |

> `bcd_device` 与 `vid_pid` 的粒度完全取决于厂商做法。请先运行 `teleimager-server --cf` 查看实际值再决定用哪个字段。

### 4.2 为什么需要两种传输方式？

服务端有两大用途，对延迟/带宽的要求不同：

- **ZeroMQ PUB–SUB** —— 通过局域网传输。高质量帧、低开销、高吞吐，在不牺牲画质的前提下保持低延迟。适合**录制训练数据**。
- **WebRTC** —— **实时预览**、VR 遥操作、UI 调试。自动码率控制下的低延迟，H.264（默认）/ VP8，兼容浏览器与 VR 设备。

### 4.3 三重环形缓冲区的好处

- **消除画面撕裂** —— 读写永远不落在同一槽位，因此读取者不会读到"写了一半"的帧。
- **始终最新** —— 与 FIFO 队列不同，旧帧会被覆盖，读取者永远拿到最新一帧。对实时性至关重要。

---

## 5. 🧐 FAQ

1. **`--cf` 输出中序列号等字段显示为 `unknown`？**
部分摄像头需要更高权限才能读取完整硬件信息，可尝试：

```bash
sudo $(which teleimager-server) --cf --uvc
```

**启动服务端时报 `No module named 'psutil'`？**
`psutil`（用于可选的 CPU 亲和性调优）已包含在 `teleimager[server]` 中。若你的环境是零散安装导致缺失，执行 `pip install psutil` 补上，或用 `pip install -e ".[server]"` 重装。现在即使缺失也只会打印警告并跳过该优化，不再导致崩溃。

---

## 6. 🙏 Acknowledgement

部分代码参考了 https://github.com/ARCLab-MIT/beavr-bot

## RoboTree 定制功能

版本 `2.1.0+robotree.2`，公版基线为 TeleImager 2.1.0，源码提交 `19fdf9329a82c13860da0a7dbfe3ce749f940456`。未采用其中的：双 RGB RealSense 相机（`sensors_config_mode`、RealSense 双目、`all_config_modes` 相机搜索报告）、移除 IsaacSim，以及自动创建默认服务端配置。

- `camera.<topic>.driver_type: rtp` 每个实例接收一条 RTP/JPEG 或 RTP/H264 流，用 `rtp_port` 选择端口并按 `source_host` 过滤。左右眼独立发布、独立恢复，不配对、不拼接；过期帧停止输出。
  - 可选配对：用 `rtp_ports: [左, 右]` 代替 `rtp_port`，一个实例同时收左右眼，只把接收时间相差不超过 `max_pair_delay` 秒（默认 0.02）的两帧拼成左右并排图发布（`image_shape` 是整幅尺寸，每眼占一半宽），现有按单个双目话题读图的客户端无需修改。
  - `jpeg_depay: python` 在进程内重组 RTP/JPEG，代替 GStreamer 解包（默认 `gstreamer`）；R1 固件每个 RTP 帧里是完整 JPEG 文件，`rtpjpegdepay` 会把它截断。
  - `rectify:` 用头部自带的标定做去畸变和极线矫正（`source` 默认 `http://192.168.123.161:9080/cam_param.txt`，缓存到 `cache`；可选的左右外参覆盖 `stereo`）。单路实例要写 `eye: left|right`。矫正后的针孔参数以 `rectified` 写进该相机的配置，由配置端口发布。外参覆盖用 `python -m teleimager.camera.r1_calib capture|fit` 标定，文件和头部绑定（`head_fingerprint`），换到别的头部会被忽略并警告。R1-D 的部署、端口、标定和切换步骤见 [configs/r1d/README.md](configs/r1d/README.md)。
- 顶层 `app_stream`（即推流文件的 `tcp` 段）支持 RoboTree App H264/TCP 直连、单画面、拼接及 `stereo_quad` 四宫格；控制端口默认 `13579`，图像连接使用 App 请求的地址和端口。使用 `stereo_quad` 时，App 还可以请求协议 v2，见下文“App 推流协议”。
- App 推流读取自己的 BGR 和显示缓冲；WebRTC 另有一个视频帧缓冲，只在浏览器连接时写入。四宫格对缺失、过期画面显示占位。`teleimager.client.TeleImageClient` 与公版客户端一致。
- 一路相机打不开、5 秒内没有首帧或运行中出错，只停这一路并记一条错误日志，其余相机照常输出，服务不退出；公版服务端遇到相机错误会整体停止。ZMQ（彩色和深度）和 WebRTC 在新帧到达时发布，停掉的相机什么都不发，客户端看到的是“没有帧”而不是冻结的画面；每个输出每次断流只警告一次。`GET_DATA` 仍返回完整配置。
- RealSense 深度：采集配置 `enable_depth_zmq: true` 的话题在其 `zmq.depth_ports` 端口发布与彩色图对齐的深度图；采集深度却没有该端口属于配置错误，`zmq.enabled: false` 同时关闭彩色和深度。`DepthPacket.decode()` 把收到的 zstd 数据保存在 `compressed`（本地构造的包为 `None`），录制端可直接存盘，无需再次压缩。
- 相机发现不再从设备取流，默认模式下不出流的相机不会卡住启动。UVC 发现（`driver_type: uvc` 和 `--cf --uvc`）用 `VIDIOC_ENUM_FMT` 读取每个节点的采集格式，列出 MJPG、YUYV、NV12、H264 等格式时视为彩色相机，红外、深度和元数据节点会被跳过。V4L2 发现保留 `v4l2-ctl` 输出中声明采集并列出格式的节点，不读取数据包。`UVCCamera._list_mjpeg_modes()` 用同样方式（`VIDIOC_ENUM_FRAMESIZES`、`VIDIOC_ENUM_FRAMEINTERVALS`）列出节点的 MJPEG 尺寸和整数帧率。`teleimager.camera_finder.find_usb_cameras()` 不取流，以 `UsbCamera` 记录返回本机 USB 相机：UVC 按 USB 接口每个一台、按 USB 口排序，带 `driver_type`、`name`、`serial_number`、`physical_path`、`video_path` 和本后端能打开的 `mjpeg_modes`；装有 `pyrealsense2` 时再列出 SDK 看到的 RealSense 及其型号。`read_brightness()` 经视频节点测量 UVC 相机画面亮度。RoboTree 的硬件信息向导使用这两个函数。

`stereo_quad` 的 `app_stream.camera_topics` 使用 `head_mono`、`head_stereo`、`head_left`、`head_right`、`wrist_left`、`wrist_right` 角色映射话题。单话题头部仅在采集配置声明 `binocular: true` 时拆分；单目完整复制到两个眼格，独立双眼分别读取。App 拼图只选择单目头部、拼接双目或独立双眼中的一种来源。所有声明话题使用独立的 ZMQ 和 WebRTC 端口；深度端口也不与任何彩色端口重复。只初始化已配置相机，无腕部时保留占位格且不启动腕部输出；所有组合共用 `streaming_config.yaml`。

统一输出参数及字段说明见 [streaming_config.yaml](streaming_config.yaml)。每份 RoboTree 实验在 `robot.drivers.teleimager.camera_streams[].camera_config` 中内联保存相机输入参数，不含发布设置；目录和流名不推导采集参数。实验通过 `streaming_config` 显式选择统一的 `streaming_config.yaml`。文件包含同级的 `zmq`、`tcp`、`webrtc` 区块，各自配置 `enabled`、`codec` 和输出参数；`zmq.ports` 设置 `enable_color_zmq` 与 `color_zmq_port`，`zmq.depth_ports` 设置 `depth_zmq_port`。Runtime 只将 ZMQ 设置用于连接校验与指纹；Docker 应用全部输出，预览参数变化不改变 Runtime 身份。统一启动参数为 `--streaming-config`，临时覆盖变量为 `TELEIMAGER_STREAMING_CONFIG`。TCP 的尺寸、FPS、码率与编码选项以推流文件为准，App 提供接收端点。v1 App 必须请求四宫格的 `width`、`height`；v2 的画面几何来自 `RTSI` 消息。`python -B docker/onboard/service.py print-config` 导出最终静态配置，不打开设备。

### App 推流协议

TCP `13579` 上的控制包先是 4 字节大端包长，再是小端长度前缀的命令和负载。App 端口上的视频消息是 4 字节大端长度加负载。

| 命令 | 版本 | 负载 | 服务端处理 |
| --- | --- | --- | --- |
| `OPEN_CAMERA` | 1 | 二进制：`0xCAFE`、版本字节 1、七个小端整数（宽、高、FPS、码率、两个不使用的字段、端口），再是流名和主机 | 按 `width` × `height` 推 `stereo_quad` 四宫格（请求尺寸必须一致），码率为 `bitrate`；直接开始发 H264 |
| `OPEN_STREAM` | 2 | JSON：`version: 2`、`request_id`、`stream_name`、`render_mode`（`immersive` 或 `window`）、`host`、`port`、`decoders`（`{编码器: {max_width, max_height}}`） | 为请求选定画面几何，新开一条视频连接，第一条消息是 `RTSI` |
| `REQUEST_KEYFRAME` | 2 | JSON：`request_id` | 为对应请求强制一个 IDR 帧，最快每 0.5 秒一次 |
| `CLOSE_CAMERA` | 1、2 | 空 | 停止推流 |

`OPEN_STREAM` 需要 `layout: stereo_quad` 提供头部角色映射，否则服务端记一条警告并忽略。RoboTree App 1.2.0 在 3 秒内收不到 `RTSI` 时退回 `OPEN_CAMERA`，并在本次运行中记住这台主机。

- **窗口视图与 v1：** `quad` 布局，尺寸为 `width` × `height`，`head_left`、`head_right`、`wrist_left`、`wrist_right` 按 2×2 排列，码率预算为 `bitrate`。
- **沉浸视图：** `stereo` 布局，`head_left`、`head_right` 左右并排。每眼尺寸取解码后的头部单眼、`eye_shape`、解码器 `max_width` 的一半与 `max_height` 三者中最小的，再向下取 16 的倍数；码率预算为 `max_bitrate`。H2 PC1 源图为 2064×1552 时，每眼 1280×960，整帧 2560×960。沉浸帧取自相机的显示缓冲：RTP 相机在自己的线程里把解码帧缩放到单眼尺寸，与 `image_shape` 无关；其他后端提供 `image_shape` 尺寸的 BGR 输出。ZMQ、WebRTC 和录制保持 `image_shape`。
- **`RTSI` 消息：** 每条 v2 视频连接的第一条消息，`RTSI` 后接 UTF-8 JSON，含 `version`、`request_id`、`codec`、`width`、`height`、`fps`、`bitrate`、`layout`（`stereo` 或 `quad`）和 `rects`（`{角色: [x, y, 宽, 高]}`，左上角为原点），之后是带长度前缀的 H264 包。v1 连接没有这个头。
- **出帧节奏：** 有头部角色时，所有头部相机都有新采集帧，或第一路新帧到达 10 毫秒后，才合成一帧；没有新帧时仍每 0.5 秒发一帧，超过 2 秒没有新帧的相机显示占位。
- **码率控制：** 上一帧还没发完或 socket 不可写时，编码前跳过这一帧。持续跳帧时目标码率乘 0.75，最快每 2 秒一次；连续 4 秒不跳帧后每 2 秒乘 1.1，范围为 [`min_bitrate`, 预算]。只有 5 秒没有任何发送进展才重连。`TCP_NOTSENT_LOWAT` 让内核里最多积压约一帧。
- **编码器：** libx264，使用 `preset`、`tune`、`threads`。`intra_refresh: true` 时没有周期性 IDR 帧，`gop_length` 是刷新周期。`maxrate` 为预算，VBV 缓冲为预算 × `vbv_seconds`。
- **统计日志：** 每 5 秒一行 `[App Stream] target=…Mbps sent=…Mbps unsent=…B encoded=… skipped=… encode_p95=…ms`；收到请求时记 `Requested immersive stream #N: stereo 2560x960@30fps up to 20.0 Mbps to <host>:<port>.`，视频连上时记 `H264 stream connected to <host>:<port> (stereo 2560x960).`

`tcp` 段的 v2 字段都是可选的，填写时会校验：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `eye_shape` | `[960, 1280]` | 沉浸视图单眼尺寸上限，`[高, 宽]`，16 的倍数 |
| `max_bitrate` | `20000000` | 沉浸视图码率预算，单位 bit/s |
| `min_bitrate` | `4000000` | 码率自适应下限，不能大于 `max_bitrate` |
| `vbv_seconds` | `0.04` | 码率控制缓冲，单位秒，取值 (0, 2] |
| `intra_refresh` | `true` | 用 intra refresh 代替周期性 IDR 帧 |
| `codecs` | `[h264]` | 按优先级提供给 App 的编码器，目前只有 `h264` |

`streaming_config.yaml` 现在设为 `preset: superfast`（原 `ultrafast`）、`threads: 4`（原 1）、`gop_length: 60`（原 30）；`bitrate` 只用于四宫格。`teleimager-server` 默认绑定 0–2 号 CPU 核，加 `--no-affinity` 关闭。

定制 RTP/App 路径需要 OpenCV（可安装已有的 `[viewer]` extra）。RTP/JPEG 还需 `gst-launch-1.0` 及 `rtpjitterbuffer`、`rtpjpegdepay`、`jpegparse` 插件，不使用通用 GStreamer 后端的 Python GI binding。

### 源码职责

`server.py` 提供 `TeleImageServer` 和服务端 CLI，`client.py` 提供 `TeleImageClient` 和客户端 CLI。`TeleImage`、`DepthPacket`、`TripleRingBuffer` 位于 `teleimager.frame`；`teleimager.client` 与公版客户端一样也导出 `TeleImage` 和 `DepthPacket`。通信类从 `teleimager.transport` 下各实现模块导入，服务端不转导出它们。现有命令名保持可用。

不再支持命名相机 `profiles`、`read_config(profile=...)` 参数及逐相机 `{config, profile}` 选择。原生相机使用完整参数字典或普通相对 YAML 引用；RoboTree 在 `camera_config` 中内联采集参数，再导出原生运行配置。客户端缓存、协议恢复和已支持的相机后端保持可用。

| 模块 | 职责 |
| --- | --- |
| `camera/camera.py` 及各后端文件 | 公共采集生命周期与逐后端实现 |
| `camera_finder.py` | 相机发现：`find_usb_cameras()` 返回数据，`--cf` 打印报告 |
| `transport/zmq_publisher.py` | ZMQ 发布线程与管理 |
| `transport/zmq_subscriber.py` | ZMQ 订阅、FPS 统计、JPEG 解码和深度包 |
| `transport/zmq_request.py` | 配置请求、响应和客户端缓存 |
| `transport/webrtc.py` | WebRTC 发布、编码设置及浏览器页面 |
| `transport/tcp_stream.py` | Robotree App TCP 控制、协议 v2 画面几何、H264 推流与码率控制及预览布局 |
| `frame.py` | 共享图像数据、深度包与最新帧缓冲 |
| `host.py` | 驱动重载、权限设置、开机服务和 CPU 亲和性 |

服务端直接引用共享通信模块，不再依赖客户端模块。WebRTC 编码器补丁在构造服务端时应用，不在模块导入时修改；页面 Logo 使用包资源定位。App 推流使用 `teleimager.transport.tcp_stream.TcpStreamServer`，不再提供原模块路径和类名。
