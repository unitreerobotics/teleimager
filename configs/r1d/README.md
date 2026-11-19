# R1-D（R1-A-D）相机服务说明

本目录是 TeleImager 在 Unitree **R1-D** 上的配置。服务运行在开发计算单元 **PC2（192.168.123.164）**，把头部双目和两个手腕相机的画面整理好，通过 ZMQ 发给下游程序（unitree_deploy 的 `ImageClient`、r1d-sandwich 技能服务器等）和 RoboTree App。

```
PC1 头部 192.168.123.161                      PC2 开发计算单元 192.168.123.164                 下游（本机等）
┌─────────────────────────────┐  RTP/JPEG   ┌──────────────────────────────────────┐  ZMQ  ┌──────────────────┐
│ 左目 1280x1088 → UDP 5004   │ ──────────▶ │ teleimager-server                     │ ────▶ │ ImageClient       │
│ 右目 1280x1088 → UDP 5005   │             │  head_camera：配对 + 去畸变 + 极线矫正 │       │ 技能服务器 / 策略 │
│ 9080 网页：cam_param.txt    │ ◀── HTTP ── │  （启动时读一次标定，带缓存）          │       └──────────────────┘
└─────────────────────────────┘             │  left/right_wrist_camera：USB v4l2    │  TCP  ┌──────────────────┐
                                            │  60000 配置端口（含矫正后内参）        │ ────▶ │ RoboTree App      │
                                            └──────────────────────────────────────┘ 13579 └──────────────────┘
```

## 1. 快速开始

.164 上已有系统服务 `unitree-r1-teleimager-robotree` 开机自启（见第 7 节），一般不用手动启动。手动调试时：

```bash
# 在 .164 上。5004/5005 同一时间只能有一个进程接收，先停服务
sudo systemctl stop unitree-r1-teleimager-robotree
cd ~/teleimager_robotree && export PYTHONPATH=$PWD/src
PY=/home/unitree/teleimager-conda/bin/python
$PY -m teleimager.camera.r1_calib show                   # 确认能读到头部标定（见第 4 节）
$PY -m teleimager.server --config configs/r1d/teleimager_r1d.yaml
```

**.164 上不要直接用 `teleimager-server` 命令**：conda 环境里装的 teleimager 包不是本仓库的代码。本文其余命令都假定已按上面设好 `PYTHONPATH` 和 `$PY`。

**前提：** 头部 PC1 要发送**左右目原图**。在 `http://192.168.123.161:9080` 选"左右目原图"：MJPG，1280x1088，30 FPS，左目 RTP 发到 .164 的 UDP 5004，右目发到 5005。机器人重启后要在 App 里重新打开这个推流。

## 2. 文件

| 文件 | 作用 |
|---|---|
| `teleimager_r1d.yaml` | **主配置**。头部左右眼配对成一路并排图；两个手腕相机按 USB 物理路径识别 |
| `streaming_r1d.yaml` | 主配置引用的输出设置：ZMQ 端口、App 推流（`stereo_quad`），WebRTC 关闭 |
| `r1d_164_r1_stereo.yaml` | **这台 .164 头部专用**的左右眼外参（R/T）。拷到 `~/.config/teleimager/r1_stereo.yaml` 才生效，换机器不能用（见第 4 节） |
| `unitree-r1-teleimager-robotree.service` | .164 上的 systemd 服务文件（见第 7 节） |
| `teleimager_r1d_independent.yaml`、`streaming_r1d_independent.yaml` | 对比用：左右眼不配对，各发一路（55559 左 / 55558 右）。现有下游不认这种输出，不要用于任务 |

## 3. 输出

| 端口 | 话题 | 内容 |
|---|---|---|
| ZMQ 55555 | `head_camera` | 头部，**矫正后**的左右并排图，1088x448（每眼 544x448，左眼在左），30 fps |
| ZMQ 55556 | `left_wrist_camera` | 左手腕，640x480，30 fps |
| ZMQ 55557 | `right_wrist_camera` | 右手腕，640x480，30 fps |
| 60000 | 配置查询 | 整份配置。`head_camera.rectified` 是矫正后的针孔参数（见下） |
| 13579 | App 推流 | RoboTree App 的 H.264 四宫格预览 |

ZMQ 消息是纯 JPEG。

**`legacy_client_keys: true`：** 下游的 `ImageClient` 只认 `enable_zmq` / `zmq_port` / `type`，而 2.1 的相机配置写这些字段会拒绝启动（要写 `enable_color_zmq` / `color_zmq_port` / `driver_type`）。所以配置里只写 2.1 的字段，顶层加 `legacy_client_keys: true`，60000 端口回复时补上 `ImageClient` 认的字段。话题名和端口保持下游现有的约定，下游不用改代码。

**`rectified` 字段：** `fx`、`fy`、`cx`、`cy`（像素，按单眼 544x448 计）、`baseline_m`（基线，米）、`R_left_raw_to_rect`（左眼原始相机到矫正相机的旋转）、`source`（参数来源，是否用了外参覆盖）、`origin`（从哪读到的）。下游计算双目深度或做射线投影时直接读这个字段，不要自己再存一份内参。

## 4. 标定

| 内容 | 是否自动 | 说明 |
|---|---|---|
| 头部内参和畸变 | ✅ 自动 | 启动时读**本机头部**的 `http://192.168.123.161:9080/cam_param.txt`（EEPROM 数据，不走代理），缓存到 `~/.config/teleimager/r1_cam_param.txt`。.161 常比 .164 开机晚，所以每秒重试一次，最多等 `wait_s`（20 秒）；等不到就用缓存，缓存也没有时头部不启动，手腕相机照常运行。只在启动时读一次，换头或重新标定后要重启服务 |
| 左右眼外参 R/T | ❌ 每台机器标一次 | EEPROM 里的 R/T 可能偏好几度。这台 .164 偏了约 3°：矫正后左右行错开 3.3 px，1 m 处深度会算错好几倍。所以用棋盘格标一次，存成 `~/.config/teleimager/r1_stereo.yaml` 覆盖。没有这个文件就用 EEPROM 的 R/T，日志里会提示 |
| 手腕相机路径 | ❌ 写死在配置里 | `physical_path` 是 USB 物理路径，同型号、接线相同的机器一般一样。两个相机的序列号都是出厂统一的 `JR0001` / `JR0002`，不能用来区分左右 |

### 外参文件和头部绑定

`r1_stereo.yaml` 里有 `head_fingerprint`，是头部 EEPROM 标定参数（左右内参、畸变、R/T）的哈希值，每个头部都不同。启动时拿本机头部算出的指纹和文件里的比对：

- **一致**：用文件里的 R/T，日志中参数来源显示 `stereo override`。
- **不一致**（文件是从别的机器拷来的）：**忽略这个文件**，改用 EEPROM 的 R/T，并打印带 `IGNORED` 的警告，避免拷错文件后深度悄悄算错。
- **文件里没有指纹**：照常使用，但打印警告，提示补上指纹或重新标定。

查看本机头部指纹：`$PY -m teleimager.camera.r1_calib show`，输出里的 `head_fingerprint` 就是。这台 .164 的指纹是 `b5395095593b24e5`。

### 标定左右外参（每台机器一次，约 5 分钟）

需要一块棋盘格（默认内角点 8x11，格边长 3 cm；其他规格用 `--pattern` 和 `--square` 指定）。在 .164 上运行：

```bash
sudo systemctl stop unitree-r1-teleimager-robotree       # 让出 5004/5005
$PY -m teleimager.camera.r1_calib capture --tag d060      # 棋盘格放在约 0.6 m 处，两只眼都要看到整块板
$PY -m teleimager.camera.r1_calib capture --tag d100      # 约 1.0 m 处
$PY -m teleimager.camera.r1_calib fit --write ~/.config/teleimager/r1_stereo.yaml
sudo systemctl start unitree-r1-teleimager-robotree
```

`capture` 会打印两只眼是否找到棋盘格，找不到就调整位置重拍。`fit` 打印结果供检查，正常情况下：
- stereo rms 小于 0.5 px；
- 基线约 60 mm；
- 每组照片的 row offset 接近 0 px；
- stereo depth 和实际距离一致。

写出的文件自带本机头部的 `head_fingerprint`。抓拍的照片在 `~/.config/teleimager/r1_stereo_capture/`。

### 本服务不管的外参

把像素换算成机器人坐标还需要两种外参，由使用画面的程序（如 r1d-sandwich）自己标定和保存：
- 头部相机相对头部连杆的安装外参；
- 手腕相机的手眼外参。

只看画面、遥操作、App 预览时不需要；要根据图像去抓东西时才需要。

## 5. 配对模式和独立模式

| | 配对（`rtp_ports: [5004, 5005]`，正式使用） | 独立（每只眼一个 `rtp_port`） |
|---|---|---|
| 输出 | 一路并排图 55555 | 左右各一路 55559 / 55558 |
| 头部帧率（实测） | 30.0 fps | 29.9 / 30.1 fps |
| 现有下游 | ✅ 直接可用 | ❌ 下游要自己配对，要改代码 |

**配对规则：** 两只眼的接收时间相差不超过 `max_pair_delay`（默认 0.02 s）才拼成一帧；超过时丢掉较旧的那只眼，等它的配对帧。

**实现：** 每只眼一个后台线程，负责收包、解 JPEG、矫正，只保留最新一帧；主线程只负责配对、拼图和编码。

只用一只眼的程序也用配对模式，从并排图里切出左半边即可。

## 6. 头部相机的配置项（`camera.head_camera`）

| 键 | 默认 | 说明 |
|---|---|---|
| `rtp_ports` | — | `[左, 右]`，开启配对模式。不写则用 `rtp_port` 单路 |
| `rtp_encoding` | `H264` | R1 头部用 `JPEG` |
| `jpeg_depay` | `gstreamer` | R1 必须用 `python`。R1 固件每个 RTP 帧里放的是完整 JPEG 文件，GStreamer 的 `rtpjpegdepay` 会截断它，只能解出顶部几行 |
| `source_host` | `192.168.123.161` | 只接收这个 IP 发来的包 |
| `image_shape` | — | 输出的 `[高, 宽]`。配对时是整幅并排图的尺寸，每眼占一半宽 |
| `receive_timeout` | 2.0 | 超过这么久（秒）没收到帧就视为断流 |
| `max_pair_delay` | 0.02 | 配对时左右眼允许的最大时差（秒） |
| `rectify.source` | 9080 地址 | 标定从哪读：URL 或本地文件（离线调试时可指向一个 `cam_param.txt`） |
| `rectify.cache` | `~/.config/teleimager/r1_cam_param.txt` | 标定缓存 |
| `rectify.wait_s` | 20 | 启动时最多等 `source` 这么久（秒） |
| `rectify.stereo` | — | 左右外参覆盖文件（见第 4 节） |
| `rectify.alpha` | 0.0 | 0 表示裁掉黑边，1 表示保留全部原始视野 |
| `rectify.eye` | — | 仅单路模式需要：`left` 或 `right` |

## 7. .164 上的部署

| | |
|---|---|
| 服务 | `unitree-r1-teleimager-robotree`，开机自启 |
| 代码 | `/home/unitree/teleimager_robotree`（`DEPLOYED_COMMIT` 记录提交号） |
| 配置 | `~/teleimager_robotree/configs/r1d/teleimager_r1d.yaml` |
| Python | conda 环境 `/home/unitree/teleimager-conda` |

服务用 `PYTHONPATH=~/teleimager_robotree/src python -m teleimager.server` 直接运行代码目录，不依赖 conda 环境里装的 teleimager 包（见 `unitree-r1-teleimager-robotree.service`）。

**更新代码：**
```bash
# 在本机仓库
git archive --format=tar.gz -o /tmp/ti.tgz HEAD && scp /tmp/ti.tgz unitree@192.168.123.164:/tmp/
# 在 .164
mv ~/teleimager_robotree ~/teleimager_robotree.bak-$(date +%s) && mkdir ~/teleimager_robotree
tar xzf /tmp/ti.tgz -C ~/teleimager_robotree       # .164 时钟落后，tar 会提示时间戳在未来，可忽略
sudo systemctl restart unitree-r1-teleimager-robotree
```

**检查：**
- `journalctl -u unitree-r1-teleimager-robotree -f`：应看到 `TeleImager v2.1.0+robotree.2`、`rectifying (eeprom intrinsics + stereo override ...)` 和两个手腕相机启动。`Failed to reload driver ... sudo: a password is required` 可以忽略：服务想用 sudo 重载 uvcvideo 驱动，而手腕相机走 v4l2，不受影响。
- 55555/6/7 都是 30 fps；60000 端口 `head_camera.rectified` 中 f 约 133、基线约 60.2 mm。
- 实测：三路 30.0–30.2 fps，进程 CPU 约 174%；`ImageClient` 不改代码就能拿到头部左右眼（544x448）和两个手腕（640x480）。

**换一台 R1-D：** 先按第 4 节标定左右外参（不要拷贝这台的 `r1d_164_r1_stereo.yaml`，指纹对不上会被忽略），再按上面方式拷代码，然后安装服务文件（按那台机器改里面的路径）：
```bash
sudo cp ~/teleimager_robotree/configs/r1d/unitree-r1-teleimager-robotree.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now unitree-r1-teleimager-robotree
```

## 8. 常见问题

| 现象 | 原因和处理 |
|---|---|
| 头部没画面，手腕正常 | ① .161 没在发左右目原图（检查 9080 页面或 App）；② 5004/5005 被别的进程占用（`sudo ss -ulpn \| grep 500[45]`）；③ 读不到标定且没有缓存，日志里有 `unreachable ... and no cache` |
| 日志里有 `stereo override ... IGNORED` | 外参文件是别的头部标定的，现在用的是 EEPROM 的 R/T，双目深度可能不准。按第 4 节在这台机器上重新标定 |
| 日志里有 `has no head_fingerprint` | 外参文件缺指纹。确认是在本机标定的，就按日志提示补上 `head_fingerprint`；不确定就重新标定 |
| 双目深度明显偏大或偏小 | 左右外参不准。看 `fit` 输出的 row offset，或者重新标定 |
| 头部帧率低于 30 | 用 `top` 看 CPU 占用。服务默认绑定 0–2 号核，`--no-affinity` 可以关掉绑定 |
| 下游报找不到 `enable_zmq` / `zmq_port` | 配置里缺了 `legacy_client_keys: true` |
| 手腕相机打不开 | `physical_path` 和实际 USB 口不一致。用 `$PY -m teleimager.server --cf` 列出设备后修改配置 |

## 9. 代码和测试

- `src/teleimager/camera/rtp.py`：`RtpCamera`（单路或配对，每只眼一个线程）和 `_RtpStream`（一路 RTP 的收包和解码）。
- `src/teleimager/camera/r1_calib.py`：解析 `cam_param.txt`、读取和缓存标定、头部指纹、外参覆盖、矫正映射、RTP/JPEG 拼帧，以及 `show` / `capture` / `fit` 命令行。
- `src/teleimager/transport/zmq_request.py`：`legacy_client_keys` 的兼容回复。
- 测试在 `tests/test_r1_calib.py` 和 `tests/test_rtp_camera_r1.py`，在 .164 上运行：`$PY -m unittest tests.test_r1_calib tests.test_rtp_camera_r1`。本仓库的 `.gitignore` 忽略了 `/tests/`，这两个测试文件不随仓库提交。
