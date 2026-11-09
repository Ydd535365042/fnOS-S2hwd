# fnOS-S2 硬件控制台

由 NIX 独立开发，飞牛 fnOS 上的硬件控制台：电源灯、硬盘灯、风扇、蜂鸣器、定时开关机，网页上直接调，不用 SSH。

![icon](docs/icon.png)

## 界面

![总览](docs/screenshots/overview-desktop.png)

![控制](docs/screenshots/control-desktop.png)

![风扇](docs/screenshots/fan-desktop.png)

![存储](docs/screenshots/storage-desktop.png)

![脚本](docs/screenshots/terminal-desktop.png)

手机端：

![手机总览](docs/screenshots/overview-mobile.png) ![手机控制](docs/screenshots/control-mobile.png) ![手机脚本库](docs/screenshots/scripts-mobile.png)

## 安装

从 [Releases](../../releases) 下载 `fnOS-S2-v1.0.0.fpk`，在飞牛 fnOS 应用中心选择「手动安装」上传。

设备上如果装过同类的硬件控制应用，请先卸载再装本应用，避免抢占 18899 端口与同一套风扇 / 灯光硬件。

默认端口 `18899`，可在向导与设置中改；建议设个访问口令。

## 构建

```
python -I build_fpk.py <app目录> <fpk目录> <输出.fpk> <版本号> <changelog文本>
```

## 兼容性

- 飞牛 fnOS ≥ 0.9.0（arm64）
- RK3568 NAS 主机（双 SATA 盘位 + eMMC）

## 许可

MIT，见 [LICENSE](fpk/LICENSE)。
