# fnOS-S2 硬件控制台

由 **NIX** 独立开发，专为飞牛 fnOS 定制的硬件控制台：把主板上的电源灯、硬盘灯、风扇、蜂鸣器与定时开关机等底层硬件能力，全部收进一个可视化控制台，无需 SSH 即可随手调节。

![icon](docs/icon.png)

## 功能一览

![总览](docs/screenshots/overview-desktop.png)

- **硬件总览**：处理器 / 内存 / 存储 / 网络一屏看完，CPU 逐核频率与占用、内存占用、每块硬盘的温度 / 健康 / 读写速率 / 通电时长，右上角时钟实时走秒。
- **风扇调速**：CPU / 硬盘双路四锚点温控曲线取高挡，静音 / 性能 / 自定义三档一键切换（不重启服务，0.5 秒内生效），界面直接显示真实 PWM 占空比，曲线可逐点微调；风扇图标按转速绿 → 橙 → 红渐变。
- **指示灯**：电源灯（网络状态指示：网线插上 → 绿灯常亮，未插网 → 红灯闪烁）、硬盘灯（无盘全灭、有盘绿灯常亮），均支持「全灭」手动覆盖，优先级最高，系统升级与自愈均不会改变。
- **定时开关机**：每日定时关机与定时开机，默认关闭、按需开启。
- **蜂鸣器**：鸣叫时长毫秒级可调，支持一键试听。
- **脚本库**：拖入 `.sh` / `.run` 一键执行，实时查看日志与退出码（控制台中途重启也不丢结果）；内置重装组件 / 恢复 DTB / 重启服务 / 一键部署全部硬件配置等出厂脚本；补丁模块热插拔，可启用 / 停用 / 卸载。
- **告警通知**：CPU / 硬盘温度、硬盘容量、内存、服务异常、风扇异常全程盯守，右上角铃铛查看，可开启浏览器系统通知。
- **三端并重**：手机 / 平板 / 桌面响应式（底部拇指导航、触控目标不小于 40px、安全区适配）。

![控制](docs/screenshots/control-desktop.png)

![风扇](docs/screenshots/fan-desktop.png)

![存储](docs/screenshots/storage-desktop.png)

![脚本](docs/screenshots/terminal-desktop.png)

手机端：

![手机总览](docs/screenshots/overview-mobile.png) ![手机控制](docs/screenshots/control-mobile.png) ![手机脚本库](docs/screenshots/scripts-mobile.png)

## 安装

从 [Releases](../../releases) 下载 `fnOS-S2-v1.0.0.fpk`，在飞牛 fnOS 应用中心选择「手动安装」上传即可。设备上如果装过同类的硬件控制应用，请先卸载（可选「保留数据」）再安装本应用，避免抢占 18899 端口与同一套风扇 / 灯光硬件。

默认端口 `18899`，可在安装向导与应用设置中自定义；建议在设置里配置访问口令后再从局域网访问。

## 仓库结构

```
app/                    应用主体（fpk 内 app.tgz 的内容）
  server/               Python 后端（纯标准库）+ 前端静态页
    server.py           HTTP 服务：/api/* 接口、脚本任务、告警轮询
    hwctl.py            硬件读写：风扇曲线、指示灯、蜂鸣器、systemd 服务
    hwinfo.py           硬件信息采集：磁盘 / 盘位 / SMART / 文件系统
    alerts.py           后台告警：温度 / 容量 / 服务 / 风扇规则引擎
    public/             前端（零依赖单页，桌面 + 手机）
    default_scripts/    出厂默认脚本（单一事实来源）
  firmware/             硬件定制脚本与 systemd 单元（fan-control 等）
  agent/                可选 root 代理 s2hw-agent（写操作需要）
  bin/                  s2hwctl-cli 命令行
  config/  ui/          fpk 权限 / 桌面入口配置
fpk/                    安装包骨架（cmd/ 回调脚本、wizard/ 向导、manifest、图标）
releases/               已发布的 .fpk 安装包与更新说明
docs/screenshots/       界面截图
```

## 构建

```
python -I build_fpk.py <app目录> <fpk目录> <输出.fpk> <版本号> <changelog文本>
```

构建脚本要求 Python 3 标准库，无需第三方依赖。

## 兼容性

- 飞牛 fnOS ≥ 0.9.0（arm64）
- RK3568 NAS 主机（双 SATA 盘位 + eMMC）

## 许可

MIT，见 [LICENSE](fpk/LICENSE)。
