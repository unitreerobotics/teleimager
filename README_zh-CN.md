<div align="center">
  <img src="teleimager_logo.png" alt="Teleimager Logo" width="100%">
  <a href="https://www.unitree.com/" target="_blank">
    <img src="https://www.unitree.com/images/0079f8938336436e955ea3a98c4e1e59.svg" alt="Unitree LOGO" width="15%">
  </a>
  <p align="center">
    <a href="README.md"> English</a> | <a>中文</a>
  </p>
  <p align="center">
  <p align="center">
    <a href="https://github.com/unitreerobotics/xr_teleoperate/wiki" target="_blank"> <img src="https://img.shields.io/badge/GitHub-Wiki-181717?logo=github" alt="Unitree LOGO"></a> <a href="https://discord.gg/ZwcVwxv5rq" target="_blank"><img src="https://img.shields.io/badge/-Discord-5865F2?style=flat&logo=Discord&logoColor=white" alt="Unitree LOGO"> <a href="https://deepwiki.com/unitreerobotics/teleimager"><img src="https://deepwiki.com/badge.svg" alt="Ask DeepWiki"></a> </a>
  </p>
</div>

## 1. 图像服务器（Image Server）

[**TeleImager**](https://github.com/unitreerobotics/teleimager) 是宇树（Unitree）的图像服务器，用于从多路摄像头（UVC、V4L2、GStreamer 和 RealSense）采集视频流，并使用 ZeroMQ 或 WebRTC 方式进行网络发布。

目前 Tele Imager 用于 [xr_teleoperate](https://github.com/unitreerobotics/xr_teleoperate) 项目中提供遥操作视频流。

> 所有可供用户调用的 API 都在代码中的 `# public api` 注释下面。



### 1.0 ✨ 特性

- 📸 支持多路 UVC、V4L2、GStreamer 和 Intel RealSense 摄像头
- 📢 使用 **ZeroMQ PUB-SUB** 方式发布视频帧
- 📢 使用 **WebRTC** 方式发布视频帧
- 💬 通过 **ZeroMQ REQ-REP** 方式响应图像配置指令
- 🆔 多种摄像头识别方式：物理路径、序列号、bcd_device、vid_pid、video 设备路径
- ⚙️ 可配置分辨率和帧率
- 🚀 使用三重环形缓冲区实现高效帧处理



### 1.1 📥 环境配置

1. 安装 miniconda3

```
# for jetson orin nx (ARM architecture)
unitree@ubuntu:~$ mkdir -p ~/miniconda3
unitree@ubuntu:~$ wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-aarch64.sh -O ~/miniconda3/miniconda.sh
unitree@ubuntu:~$ bash ~/miniconda3/miniconda.sh -b -u -p ~/miniconda3
unitree@ubuntu:~$ rm ~/miniconda3/miniconda.sh
unitree@ubuntu:~$ source ~/miniconda3/bin/activate
(base) unitree@ubuntu:~$ conda init --all
```

2. 创建并激活 conda 环境：

```
(base) unitree@ubuntu:~$ conda create -n teleimager python=3.10 -y
(base) unitree@ubuntu:~$ conda activate teleimager
```

3. 安装项目与依赖：

```
(teleimager) unitree@ubuntu:~$ sudo apt install -y libusb-1.0-0-dev libturbojpeg-dev
(teleimager) unitree@ubuntu:~$ git clone https://github.com/unitreerobotics/teleimager.git
(teleimager) unitree@ubuntu:~$ cd teleimager
# 假如您只使用客户端
(teleimager) unitree@ubuntu:~/teleimager$ pip install -e .
# 假如您还使用服务端
(teleimager) unitree@ubuntu:~/teleimager$ pip install -e ".[server]"
```

4. 添加 video 权限（非 root 用户运行）：

```
bash setup_uvc.sh
```

5. 配置证书路径（WebRTC 模式需要）
    证书通常由 [televuer](https://github.com/unitreerobotics/televuer) 仓库生成。

   你可以通过 **用户配置目录** 或 **环境变量** 两种方式指定证书路径。

   方法 1：用户配置目录（推荐）

   ```bash
   mkdir -p ~/.config/xr_teleoperate/
   cp cert.pem key.pem ~/.config/xr_teleoperate/
   ```

   方法 2：环境变量方式

   ```bash
   echo 'export XR_TELEOP_CERT="your_file_path/cert.pem"' >> ~/.bashrc
   echo 'export XR_TELEOP_KEY="your_file_path/key.pem"' >> ~/.bashrc
   source ~/.bashrc
   ```

   方法 3：默认行为
    若不配置，Tele Imager 会从默认模块路径查找证书。



### 1.2 🔍 查找已连接的摄像头

运行以下命令可以自动发现已连接摄像头：

```bash
python -m teleimager.server --cf
# 或
teleimager-server --cf
```

你将看到类似下面的输出：
 ```bash
 (teleimager) unitree@ubuntu:~$ teleimager-server --cf
 ============================ Camera Discovery ============================
 Found video devices: ['/dev/video0', '/dev/video1', '/dev/video2']
 Found RGB video devices: ['/dev/video0', '/dev/video2']

 ══════ Camera 1/2: Cherry Dual Camera (DECXIN) ══════
   physical_path : /sys/devices/pci.../usb1/1-3/1-3.1/1-3.1:1.0
   serial_number : 01.00.00
   bcdDevice     : 0217 (v2.17)   (USB device release number)
   vid : pid     : 1bcf:2d4f        (VendorID : ProductID)
   video_id      : 0
   uid           : 1:71           (USB bus:device address)
   Supported modes (MJPG)  [width x height @ fps]:
     640x480 @ [10, 15, 20, 25, 30, 60, 120] fps
     1280x720 @ [10, 15, 20, 25, 30, 60] fps
     ...

 ══════ Camera 2/2: Abham Image (HHWei Technology Co., Ltd.) ══════
   physical_path : /sys/devices/pci.../usb1/1-1/1-1:1.0
   serial_number : HHW001
   bcdDevice     : 0200 (v2.00)   (USB device release number)
   vid : pid     : 1c45:6200        (VendorID : ProductID)
   video_id      : 2
   uid           : 1:79           (USB bus:device address)
   Supported modes (MJPG)  [width x height @ fps]:
     640x480 @ [5, 10, 15, 20, 25, 30] fps
     1920x1080 @ [5, 10, 15, 20, 25, 30] fps
     ...
 ================================================================
 ```

如果存在 RealSense 设备并加上 `--rs` 参数，也会看到 RealSense 摄像头的搜索结果。

------

### 1.3 📡 启动图像服务器

根据摄像头搜索结果配置 `cam_config_server.yaml`。
 （示例配置见原文，此处不重复）

启动服务器：

```
python -m teleimager.server
python -m teleimager.server --rs   # 若使用 RealSense

# 或
teleimager-server
teleimager-server --rs
```



## 2. 图像客户端（Image Client）

该模块提供图像客户端，用于连接图像服务器并接收显示多路视频流。
 专为远程操作场景设计，与图像服务器配合使用。

所有可调用 API 都在 `# public api` 注释下。

------

### 2.1 🌀  ZMQ 使用方式

服务器运行后，在另一个终端启动客户端：

```
python -m teleimager.client
# 或
teleimager-client --host 127.0.0.1
```

若服务器运行在例如 `192.168.123.164` 的 G1 Jetson 上，则：

```
teleimager-client --host 192.168.123.164
```

然后你将看到各路 ZMQ 摄像头的视频窗口。

> 需要确保环境中安装了 opencv-python

### 2.2  🌀 WebRTC 使用方式

若使用 WebRTC，可通过浏览器访问：

```
https://<host_ip>:<webrtc_port>
# 例如
https://192.168.123.164:60001
```

点击页面中间的 `start` 按钮



## 3. 🚀🚀🚀 自动启动服务

完成上述配置并测试成功后，可以通过以下脚本配置系统自动启动：

```
bash setup_autostart.sh
```

根据提示完成配置即可。



## 4. 🧠 设计原理



### 4.1 为什么需要多种摄像头识别方式？

当多个摄像头同时接入时，系统需要可靠的方式区分它们。Tele Imager 支持五种标识，
按优先级查找：`physical_path > serial_number > bcd_device > vid_pid > video_id`。

每种方式各有优劣。更重要的是，厂商对这些字段的实际使用方式往往与 USB 规范不一致
——某一字段在规范里的”标准含义”和厂商固件中实际写入的值可能完全不同。以下描述基于
实际场景，而非单纯的规范定义。

------

#### 1. 物理路径（physical_path）—— 最高优先级

Linux 内核分配的 USB 拓扑路径，能唯一确定摄像头插在哪个物理口上。

🎯 优点

- 只要不换口，始终稳定
- 不依赖厂商固件写了什么
- 非常适合固定部署（如机器人头部 + 左腕 + 右腕）

⚠️ 缺点

- 换 USB 口就必须修改配置

------

#### 2. 序列号（serial_number）

存储在摄像头 USB 设备描述符中的字符串，规范上应是每台设备唯一的。

🎯 优点

- 换口也不变，可移植性好
- 规范上就是用来区分单台设备的

⚠️ 缺点

- 部分低成本摄像头所有设备共用同一个序列号，或直接留空
- 有些摄像头序列号格式异常或不稳定

------

#### 3. USB 设备版本号（bcd_device）

USB 设备描述符中的 BCD 编码数字，规范上是固件版本号。但实际中，有些厂商会故意给
同一型号的不同设备分配不同的 bcd_device——例如用来区分左右手腕的一对相机。

🎯 优点

- 固化在固件中，不受重启或换口影响
- 当厂商有意用它区分设备时，可以充当序列号的作用

⚠️ 缺点

- 很多厂商仅把它当固件版本号使用，所有设备相同
- 能否用于区分设备，取决于具体硬件，无法一概而论

------

#### 4. 供应商:产品 ID（vid_pid）

USB-IF 分配的 vid 和厂商自定的 pid，规范上标识芯片/产品型号。但有些厂商会给不同
设备分配不同的 pid，实际上把它当序列号使用。

🎯 优点

- 极其稳定——同一芯片 vid 永远不变
- 可以一眼区分不同型号的摄像头

⚠️ 缺点

- 同型号通常共享相同 vid_pid
- 它标识的是型号还是单台设备，完全取决于厂商

------

#### 5. video 设备路径（video_id: /dev/videoX）

内核枚举时分配的 V4L2 设备节点编号。

🎯 优点

- 最直接：看到 `/dev/video2` 就填 `video_id: 2`

⚠️ 缺点

- 插拔顺序、重启、内核枚举顺序都会改变它
- 多摄像头场景下不可靠

------

#### 五种标识定位

> **注意：** bcd_device 和 vid_pid 的粒度完全取决于厂商的做法。运行
> `teleimager-server --cf` 查看实际值后再决定用哪个字段。



### 4.2 为什么需要两种图像传输方式？

本图像服务有两大用途：

1. **录制高质量数据 → 用于模型训练**
2. **实时可视化（XR / UI） → 用于调试、状态监控、远程操作界面**

不同传输场景（本地 / 局域网 / 远程网络）对延迟和带宽的要求不同，因此系统提供两种图像传输方式。

------

#### 1. ZeroMQ PUB–SUB

**适用于：服务器与客户端**不在同一台机器**，需要通过局域网传输时使用**。ZeroMQ 模式主要用于 **跨机器** 的图像传输，例如图像服务器在 A 电脑，数据记录程序在 B 电脑。

🎯 优点

- 在局域网（LAN）内传输高质量图像
- 尽可能减少包开销，提高吞吐
- 不牺牲图像质量的前提下保持低延迟

------

#### 2. WebRTC

**适用于：实时监控预览、VR 遥操作、UI 调试**。WebRTC 不是为训练数据设计的，而是为 **实时可视化流** 设计的：

🎯 优点

- 自动码率控制的前提下低延迟
- H.264(默认) / VP8
- 适用于浏览器、VR 设备等



### 4.3 Triple Ring Buffer 有什么好处？

- **非阻塞读写 (Non-blocking):**

  **写入者（Writer）** 不需要等待读取者读完。只要有空闲缓冲区，它就一直写。即便读取者卡住了，写入者也可以跳过被占用的槽位，继续在另外两个槽位间轮转。

  **读取者（Reader）** 不需要等待写入者写完。它总是能立即拿到最近一次**完整**写入的一帧数据。

- **消除“画面撕裂” (No Tearing):**

  由于读取和写入永远不会发生在同一个索引（`write` 函数中有专门的 `if write_index == read_index` 避让逻辑），读取者永远不会读到一张“写了一半”的图片。

- **始终最新 (Always Fresh):**

  与标准的队列（Queue）不同，队列是先进先出（FIFO），如果处理慢，队列会积压，导致读取者看到的画面有延迟。

  三重缓冲允许**丢帧**。如果写入太快，旧的帧会被覆盖，读取者永远拿到的是 `latest_index` 指向的那一帧。这对实时性至关重要。


## 5. 🧐 FAQ

1. 为什么 teleimager-server --cf 输出的信息中序列号等内容为 unknown？

    您可以尝试添加 `sudo` 权限运行该命令，某些摄像头需要更高权限才能读取完整信息。
    例如：

    ```bash
    sudo $(which teleimager-server) --cf
    ```

## 6. 🙏 Acknowledgement



部分代码参考了 https://github.com/ARCLab-MIT/beavr-bot