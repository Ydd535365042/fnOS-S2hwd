# 安全修复与前端审查（2026-11-10）

本次修复针对 `repo/app` 源码。测试中的硬件操作、脚本启动均由记录调用的替身接管，
没有在真实 NAS 上执行重启、关机、服务停止或固件写入。

## 安全修复

| 问题 | 修复 |
| --- | --- |
| 同主机其他端口可利用 Cookie 发起跨来源写请求 | 所有写请求检查 Origin / Fetch Metadata，API 写请求要求 `X-S2-Request: 1`，JSON 接口校验媒体类型；上传也经过同一校验 |
| 口令直接进入一年期 Cookie，向同主机其他端口发送 | 登录签发随机短期会话，服务端仅保存哈希；前端使用按来源隔离的 sessionStorage，通过 Bearer 头发送；不再接受 Cookie / URL 口令，并清理旧 Cookie |
| 未设口令时定时关机、服务管理等可远程调用 | `/api/hw` 改为只读操作白名单，其他操作统一要求已认证或本机来源；兼容路径 `/api/v1/` 同样生效 |
| 可选 root 代理允许其他本机用户匿名调用 | 所有代理路由要求随机密钥；密钥为 root 所有、应用组只读，缺失或不安全时拒绝调用；升级迁移旧代理，失败时保持停止 |
| 配置文件原子替换后可能泄漏口令 | 使用随机、私有临时文件；写 settings.conf 时明确 0600，保留其他已有文件的所有者和权限，拒绝符号链接目标 |

仓库外辅助脚本 `../get_dtb.py` 的硬编码 SSH 凭据已移除，改用环境变量/交互输入，
并要求已有可信 SSH 主机密钥。此次无法轮换设备上的旧密码；已暴露的旧密码仍需在设备端更换。

## 前端修复与审查

- API 请求有超时和响应结构校验，异常响应/超时会显示中文错误并恢复控件；写请求不自动重试。
- 增加退出登录，401 清除会话并跳到登录页；认证数据不进入 URL、localStorage 或 Cookie。
- 定时计划保存期间防止重复提交及轮询覆盖，失败后回滚并恢复后台同步。
- 定时关机倒计时使用设备时区，未配置关机时间时不再显示默认关机时间。
- 改端口跳转保持当前协议，正确处理 IPv6 地址；无口令远程模式的定时计划与端口按钮禁用。
- 检查动态 HTML、脚本预览、文件名和模块显示：已审查路径使用转义或 textContent，未确认新的 XSS；浏览器注入标记验证了脚本名转义。

## 接口迁移

1. `POST /login` 发送 `application/x-www-form-urlencoded` 的 `token` 字段，返回 JSON 中的 `session`。
2. 后续 API 请求携带 `Authorization: Bearer <session>`；API 写请求另加 `X-S2-Request: 1`。
3. JSON 写接口使用 `Content-Type: application/json`；原始文件上传使用 `application/octet-stream`。
4. `POST /api/logout` 撤销当前会话。会话最长 12 小时，服务重启后失效。
5. 受信任的原生自动化客户端仍可用 `X-S2-Token` 显式头，但写请求也需安全校验头和正确媒体类型。

反向代理需保留浏览器访问的 Host（包含端口），否则 Origin 检查会拒绝写操作。
HTTP 部署仍不提供传输加密；本次修复没有配置 TLS。
页面仍支持 fnOS 桌面 iframe，没有新增限制嵌入来源的策略。

## 验证

接口与安全回归：16 项测试，15 项通过、1 项因 Windows 权限语义不适用而跳过。
Chrome 浏览器回归：24 项通过。Python 语法、JavaScript 语法和向导 JSON 检查通过。

```text
python -I -B tests/test_security.py
node tests/browser_security.mjs
node --check app/server/public/app.js
node --check app/server/public/login.js
```

浏览器回归使用 Chrome、两个本机随机端口和模拟硬件，覆盖实际登录、旧 Cookie 清理、
退出撤销、跨端口凭据隔离、CSRF 拦截、XSS 转义、写请求头、保存回滚、异常响应和超时；
检查 1440 / 390 / 360 像素下概览与控制页无整页横向溢出。

当前 Windows 环境不能验证 Linux/POSIX 的真实文件权限，相关测试明确跳过。
systemd 安装/升级及实际风扇、RTC、设备重启还需要在目标 fnOS 设备验证。
本次未部署到设备，也未发布 release。
