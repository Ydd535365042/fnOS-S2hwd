# fnOS-S2（s2hwctl）

面向 RK3568 / NanoPi R5S 飞牛定制固件的 **fpk 应用**：把电源灯、硬盘灯、风扇挡位、
蜂鸣器时长做成可视化调节，并提供「脚本拖放修复窗口」。应用访问端口可自定义。

**v1.1.0 起，fpk 内嵌全部硬件定制脚本**（`app/firmware/`：灯/风扇/电源键/蜂鸣/随机 MAC/
主机名/自愈/DTB）。**安装即自动部署**到系统并启用服务，因此本 fpk 可作为单一交付物，
装到任意 fnOS 固件（无需事先刷定制镜像）即可获得完整 S2 硬件行为；卸载时保留已部署的
硬件行为（只移除应用自身文件）。

## 界面（v1.1.7 HUD 科技风）

12 栅格控制台：顶部 HUD 状态栏（权限 / 设备名 / 端口 / 实时时钟 + 底部扫描流光），全宽
「运行总览」含四枚发光环形仪表（CPU 温度、硬盘温度、风扇挡位、硬盘数，带刻度环与
温度分级变色）、CPU / 硬盘实时温度趋势曲线、服务状态矩阵；灯光阵列（电源灯 / 硬盘灯
带虚拟状态灯泡、蜂鸣器带波形指示）；风扇控制带按实际挡位旋转的可视化扇叶；终端风
脚本修复窗。卡片为玻璃拟态 + 双层背景渐变描边 + 鼠标跟光，背景为粒子星座画布。
纯本地资源，无外链字体与 CDN，适配离线环境与手机；file:// 离线打开自动渲染演示数据。
功能与后端接口与 v1.1.5 起保持一致。

## 功能

| 模块 | 可调节项 | 落地点 |
|---|---|---|
| 电源灯 | 自动=网络状态指示（网线插上/链路通 → 绿灯常亮；没插网线 → 红灯闪烁）；可一键「全灭」手动覆盖（优先级最高） | `power-led-net.service`；覆盖配置 `/etc/s2hwctl/led-override.conf`，节奏 `/etc/power-led-net.conf` |
| 硬盘灯 | 自动=有盘绿灯常亮 / 无盘全灭；可一键「全灭」手动覆盖（优先级最高） | `sata-led-manager`；覆盖配置 `/etc/s2hwctl/led-override.conf`，规则 `/etc/sata-led.conf` |
| 硬盘灯 | 有盘灯色（全灭 / 绿 / 红 / 琥珀 / 交回系统）、检测节拍 | `/etc/sata-led.conf` + 重启 `sata-led-manager` |
| 风扇 | 静音 / 性能 / 自定义三档；CPU 与硬盘四锚点温控曲线取高挡，界面显示真实 PWM 占空比 | `/etc/fan-control.conf` + `fanctl` 立即生效 + 重启 `fan-control` |
| 蜂鸣器 | 鸣叫时长 30-3000ms，可立即试听 | `/etc/s2hwctl/beep.conf`，作用于 gpio_ctrl / GPIO18 |
| 脚本修复 | 拖入 `.sh` 上传并执行，实时输出日志；内置 5 个一键修复脚本 | `${TRIM_PKGVAR}/scripts` |
| 访问端口 | 安装向导 / 应用设置 / 页面内均可改 | `${TRIM_PKGETC}/settings.conf` |

内置一键脚本：重装定制功能、恢复定制 DTB、重启硬件服务、自检并修复、收集诊断信息。

## 目录结构（fpk 标准）

```
s2hwctl-fpk/
├── manifest                 # 应用身份（platform=arm / service_port=18899）
├── ICON.PNG ICON_256.PNG    # 应用中心图标 64 / 256
├── config/privilege         # run-as: root（硬件操作必需）
├── config/resource          # data-share（脚本共享目录）
├── wizard/                  # install / config / upgrade / uninstall 向导
├── cmd/                     # 9 个生命周期脚本（main 负责启停与状态）
└── app/                     # → 安装后 target (TRIM_APPDEST)
    ├── ui/config            # 桌面入口（port 用 ${wizard_port}）
    ├── ui/images/           # icon-64.png / icon-256.png
    ├── bin/s2hwctl-cli      # 命令行入口
    ├── server/server.py     # HTTP 服务（Python 标准库，无第三方依赖）
    ├── server/hwctl.py      # 硬件控制层（op 分发表）
    ├── firmware/            # 内嵌全部硬件定制脚本（38 个文件 + deploy.sh）
    └── agent/               # s2-power-led 开机应用 + 可选 root 代理 s2hw-agent
```

## 打包

需要 Linux（x86_64）或 fnOS 环境：

```bash
bash build.sh          # 自检 + 生成图标 + fnpack 打包 → s2hwctl.fpk
bash build.sh check    # 只做语法与结构自检
```

`build.sh` 会自动下载官方 `fnpack 1.2.1`（static2.fnnas.com），依次尝试 `fnpack pack` /
`fnpack build`。若无法联网，可把整个目录拷到设备上：

```bash
appcenter-cli install-local     # 开发模式安装，无需 .fpk
```

## 安装

应用中心 → 设置 → **手动安装应用** → 选择 `s2hwctl.fpk`；或在设备上：

```bash
appcenter-cli install-fpk s2hwctl.fpk
appcenter-cli start s2hwctl
```

安装向导第二步可设置端口（默认 18899）、访问口令、是否安装开机灯光服务。

> **装完想在手机 / 别的电脑上传 `.run` 模块，安装时必须启用访问口令。**
> 未启用口令时，脚本上传 / 执行与改端口这三条高危接口只允许本机（loopback）调用。
> 忘了设也不用重装：进「应用设置 → 配置」，在「设置新口令」填一个口令并保存
> （应用自动重启），再用 `http://设备地址:端口/?token=你的口令` 打开一次绑定 Cookie 即可。
> 配置页口令框**留空 = 保持现有口令不变**，填新值 = 启用 / 更新，
> 勾选「清除访问口令」才关闭；只改端口不会误清口令。

> **ARM 说明**：manifest 里 `platform=arm`（R5S 是 arm64）。若应用中心提示架构不匹配，
> 把 manifest 改成 `platform=all` 重新打包即可。

## 端口为什么用三级优先级

`service_port` 只在打包时固定，`TRIM_SERVICE_PORT` 首次安装后就不再变化，用户在向导里
改的 `wizard_port` 不会自动回灌。因此：

1. `install_callback` / `config_callback` 把端口写进 `${TRIM_PKGETC}/settings.conf`
2. `cmd/main` 启动时按 `settings.conf → TRIM_SERVICE_PORT → 18899` 取端口
3. 桌面入口 `app/ui/config` 用 `"port": "${wizard_port}"` 跟随变化

页面里改端口会写 `settings.conf` 并自重启（后台执行 `cmd/main stop → start`）。

## 权限：root 与非 root 两条路

- **root 模式**（默认声明 `"run-as": "root"`）：直接写 sysfs / conf、重启服务。
  第三方 root 应用只能在应用中心「手动安装」，不能上架应用商店 —— 自用无影响。
- **应用用户模式**：页面只读。把 `app/agent/s2hw-agent.py` 以 root 部署为服务后，
  页面会把 `{op, params}` 转发到 `http://127.0.0.1:8899` 代为执行：

```bash
install -m 755 app/agent/s2hw-agent.py /usr/local/sbin/s2hw-agent.py
install -m 644 app/agent/s2hw-agent.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now s2hw-agent
```

页面右上角徽章会显示当前是「root 直控」还是「应用用户（需代理）」。

## 与既有定制组件的关系

**v1.1.0 起，安装即部署**：`cmd/install_callback` 会调用 `app/firmware/deploy.sh`，把
`app/firmware/` 下的全部硬件脚本落到系统（`/usr/local/sbin`、`/etc/systemd/system`、`/etc`、
`/boot/dtb` 等）并启用服务。这意味着 —— 哪怕目标固件是**从未刷过定制镜像的干净 fnOS**，
只要装上本 fpk，就会自动获得：

- 硬盘灯（无盘全灭 / 有盘绿灯常亮）、电源灯网络状态指示（有链路绿灯常亮 / 无链路红灯闪）
- 风扇自动温控 + 双曲线、开机蜂鸣、电源键长按关机 + 蜂鸣
- 随机 MAC、主机名 `fnOS-S2`、升级后 DTB/关键文件自愈

`deploy.sh` 刻意**不使用 `set -e`、不主动重启、DTB 仅在板子匹配时安装**，因此即使个别
步骤失败也不会让 fpk 安装被判失败。

| 内嵌组件 | 部署落点 |
|---|---|
| `sata-led-manager.*` / `sata-led.conf` | `/usr/local/sbin`、`/etc/systemd/system`、`/etc/sata-led.conf` |
| `fan-control.*` / `fanctl` / `fan-api.*` | `/usr/local/bin`、`/etc/systemd/system`、`/etc/fan-control.conf` |
| `power-led-solid.*` | `/usr/local/sbin`、`/etc/systemd/system` |
| `beep-short.sh` / `beep-boot.service` | `/usr/local/sbin`、`/etc/systemd/system` |
| `power-key-daemon.*` / `power-key.*` | `/usr/local/sbin`、`/etc/systemd/system`、`/etc/triggerhappy`、`/etc/systemd/logind.conf.d` |
| `random-mac.*` | `/usr/local/sbin`、`/etc/systemd/system` |
| `set-hostname.*` | `/usr/local/sbin`、`/etc/systemd/system` |
| `selfheal-*` / `boot-version-guard.*` | `/usr/local/sbin`、`/etc/systemd/system` + 备份到 `/usr/local/share/install-1014` |
| `rk3568-nanopi-r5s-new.dtb` | `/boot/dtb/rockchip/rk3568-nanopi-r5s.dtb`（板子匹配才装） |

卸载时**保留**上述已部署的硬件行为（只移除 fpk 自身文件），因此硬件控制不会因卸载而消失。

页面「脚本修复」里新增 `00-部署全部硬件配置`（一键重跑 `deploy.sh`），以及原有的
重装定制功能 / 恢复 DTB / 重启硬件服务 / 自检修复 / 诊断脚本，它们会优先到 fpk 内嵌目录
（`${TRIM_APPDEST}/firmware`）找源，其次才是 `/usr/local/share/install-1014` 备份目录。
