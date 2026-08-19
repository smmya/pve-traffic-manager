# PVE 流量控制管理器

基于 Python 开发的 Proxmox VE 虚拟机流量控制管理器。以组为单位批量管理虚拟机流量，超出限额自动关机。

- 兼容 PVE 8 / PVE 9
- 同时支持 KVM (qemu) 和 LXC 容器
- CLI 菜单式交互，Telegram 功能使用锁定版本的 PTB 22.8
- 前台管理 + 后台 crontab 监控
- Telegram 80% 预警、超限关机通知与双向 Bot 控制

---

## 一键下载并运行

在 PVE 节点上执行以下命令：

```bash
# 下载项目
wget -O pve-traffic-manager.zip https://github.com/smmya/pve-traffic-manager/archive/refs/heads/main.zip

# 解压
unzip pve-traffic-manager.zip && mv pve-traffic-manager-main pve-traffic-manager

# 进入目录
cd pve-traffic-manager

# 运行前台管理程序
python3 manager.py
```

或者一行命令完成下载、解压、启动：

```bash
wget -O /tmp/pve-tm.zip https://github.com/smmya/pve-traffic-manager/archive/refs/heads/main.zip && unzip -qo /tmp/pve-tm.zip && mv pve-traffic-manager-main pve-traffic-manager && rm /tmp/pve-tm.zip && cd pve-traffic-manager && python3 manager.py
```

---

## 快速开始

### 1. 前台交互

```bash
cd pve-traffic-manager
python3 manager.py        # 首次运行
```

安装快捷指令后，在任意目录直接输入 `ptm` 即可启动：

```bash
ptm                       # 全局快捷启动
```

CLI 菜单结构：

```
=== PVE 流量控制管理器 ===

[主菜单]
1. 组管理        → 创建/修改/删除管理组，设定流量限额
2. 虚拟机管理    → 扫描 VM，批量加入组（支持 100-110 范围写法）
3. 流量监控      → 查看流量概览/详情/历史，手动重置
4. 系统设置      → 查看配置、管理 crontab、Telegram、升级和操作日志
5. 退出
```

### 2. 后台监控（一键安装）

在 `manager.py` 中进入 **系统设置 → 后台监控管理**，选择「安装后台监控」即可自动配置 crontab。

```bash
=== 系统设置 ===
  1. 查看当前配置
  2. 后台监控管理 [已安装]  ← 这里自动显示安装状态
  3. 查看操作日志

=== 后台监控管理 ===
  1. 安装后台监控          ← 自动写入 crontab，可选择间隔
  2. 卸载后台监控          ← 一键移除
  3. 手动执行一次监控      ← 前台测试运行
```

建议始终通过菜单安装或更新后台任务；程序会保留已有的 crontab 条目，并防止多个监控进程重叠运行。

```bash
python3 manager.py
# 系统设置 → 后台监控管理 → 安装后台监控
```

### 3. 升级

在 `manager.py` 中进入 **系统设置 → 检查并升级程序**，会自动调用 `upgrade.py` 完成升级。

也可以独立运行：

```bash
python3 upgrade.py              # 检查并升级
python3 upgrade.py --check      # 仅检查新版本
python3 upgrade.py --force      # 强制升级
```

### 4. Telegram 接入

进入 **系统设置 → Telegram 接入**，按以下顺序完成首次配置：

1. 从 BotFather 获取 Bot Token，并在 PTM 中设置（界面只显示脱敏值）。
2. 选择「安装/更新 Telegram Python 依赖」。依赖会装入项目私有目录 `.ptm-packages/`，不会修改 PVE 的系统 Python。
3. 选择「安装/重启 Bot 后台服务」，然后在 Telegram 中向机器人发送 `/id`。
4. 将机器人返回的会话 ID 填入 PTM；群组 ID 通常是负数。
5. 运行连接诊断和测试消息，最后启用 Telegram 推送。

每次启动 `ptm` 都会显示 Telegram HTTPS 网络、Bot Token 有效性和授权会话可访问性结果。Bot 以独立 systemd 服务长轮询，不依赖流量监控的 crontab。

流量通知按“机器 + 管理组 + 本次流量周期”去重，不会按时间反复发送：达到配置的预警比例时通知一次；达到 100% 且 PVE 接受关机请求后再通知一次。只有该机器的流量被手动重置，或在 PTM 超限关机后被用户重新启动并自动重置，才会开启下一周期的预警和关机通知；如果 Telegram 发送失败，则保留到下一轮重试。

当一个管理组内已无继续运行的虚拟机，并且其中至少一台原本处于运行状态、由 PTM 成功提交了超限关机请求时，Telegram 会额外发送一次“管理组已全部关机”通知。其他机器可以是此前已手动停止或维护停机的状态，不要求每台都具备 PTM 关机记录，也不等待关机请求被下一轮标记为 `stopped`。该组同一流量周期只通知一次；组内任一机器重置流量后重新开放，发送失败会在下一轮重试。

后台监控每轮都会从 PVE 同步受管 KVM/LXC 的当前名称，自动修复旧版本留下的空名称。LXC 同时兼容列表中的 `name`、`hostname` 和容器配置里的 `hostname`，且临时空结果不会覆盖已保存名称。

机器人只接受 PTM 中配置的单一授权会话；未授权会话除 `/id` 外不能使用任何管理功能。日常管理采用“按钮 + 文字反馈”，用户打开 `/menu` 后即可完成全部操作，无需输入组 ID、VMID 或机器类型等参数：

- 「状态」「流量」「预警」「日志」分别显示对应信息，并提供返回或上下文操作按钮。
- 「重置数据」可选择「按组重置」或「按机器重置」，继续点击目标对象并完成二次确认；执行结果会以文字明确说明影响范围。
- 「网络检测」可查看状态或点击按钮手动运行一次。

Bot 使用分层按钮导航：主菜单不显示“立即采集”，各状态页、流量页、日志页、重置页和网络检测页使用各自操作按钮并提供返回键。Bot 命令列表仅保留 `/menu`、`/help` 和首次配置所需的 `/id`；旧版参数命令只作升级兼容，不在界面或帮助中展示。

### 5. LXC 网络状态检测

进入 **系统设置 → Telegram 接入 → LXC 网络状态检测管理**：

- 可单独启用/停用，默认每 6 小时启动一轮。
- 检测 IP 支持 IPv4/IPv6；多个地址使用 `;` 分隔。
- 每轮扫描 PVE 上全部正在运行的 LXC，不限于已加入流量组的容器。
- 每个容器随机选择一个检测 IP，通过 `pct exec <CTID> -- ping` 在容器自身网络环境中发送 3 个包。
- 只要有一个 ping 成功即判定正常；3 个全部失败才通过 Telegram 通知。
- 容器之间固定间隔 30 秒；正常时不推送消息。

网络检测运行在 Telegram Bot systemd 服务中。升级本功能后需再次执行“安装/更新 Telegram Python 依赖”并重启 Bot 服务，以安装 PTB `job-queue` 组件。

---

## 核心逻辑

```
组内每台 VM 独立判断：
    单台 VM 累计流量 (入站+出站) >= 组设定的限额
    → 1. 达到预警比例（默认 80%）时 Telegram 提醒一次
    → 2. 若配置了 notify_cmd，执行兼容通知命令
    → 3. 关闭该 VM（不影响组内其他 VM）
    → 4. PVE 接受关机请求后发送 Telegram 关机通知
```

- **组限额 = 每台 VM 的上限**，不是组内共享额度
- VM-A 超了只关 VM-A，VM-B 照常运行
- 不做周期清零；支持手动重置
- VM 被 PTM 超限关机后，只有确认其已停止并再次启动，才会自动重置该 VM 在各组中的流量
- 自动重置按 `(VMID, 类型)` 执行；同组其他机器及其通知状态不受影响
- Telegram 预警在每次流量重置周期中只发送一次；发送失败会在下次监控重试
- 预留 `notify_cmd` 通知接口

---

## 项目结构

```
pve-traffic-manager/
├── manager.py          # CLI 前台交互主程序
├── monitor.py          # 后台监控脚本（crontab 调用）
├── upgrade.py          # 一键升级脚本
├── db.py               # SQLite 数据库操作层
├── pve.py              # PVE API 交互封装（pvesh/qm/pct）
├── config.py           # 配置文件
├── telegram_service.py # Telegram 通知、诊断和 systemd 管理
├── telegram_bot.py     # Telegram 双向交互 Bot
├── requirements.txt    # 锁定 python-telegram-bot 版本
├── VERSION             # 版本号
└── data/               # 运行时数据目录
    └── traffic.db      # SQLite 数据库（自动创建）
```

---

## 功能清单

| 功能 | 说明 |
|------|------|
| 组管理 | 创建/修改/删除管理组，自定义单 VM 流量限额，可选通知命令 |
| 批量选 VM | 支持 `100` / `100-110` / `100,102,105-110` 范围写法 |
| 自动识别 | 自动扫描 PVE 中所有 KVM 和 LXC，显示名称和运行状态 |
| 流量采集 | 通过 pvesh API 获取 netin/netout 累计字节，计算增量 |
| 防重运行 | 使用进程锁避免 cron 与手动监控重叠导致重复计费/重复关机 |
| 超限关机 | 每台 VM 独立判断，超限自动 qm shutdown / pct shutdown |
| 通知接口 | `notify_cmd` 字段可配自定义脚本，支持变量替换 |
| Telegram 通知 | 默认 80% 单次预警、超限关机通知、全组关机汇总、失败自动重试 |
| Telegram Bot | 分层按钮导航；授权会话内查询/日志/按组或按机器确认式重置 |
| LXC 网络检测 | 通过 `pct exec` 使用容器网络执行 3 次 ping，异常才推送，支持多 IP 随机选择 |
| 历史数据 | SQLite 存储流量日志（每次采集的增量）和操作日志 |
| 流量重置 | 前台手动触发整组重置，数据不丢失 |
| 升级支持 | 系统设置菜单调用 upgrade.py，一键检查并升级，自动备份数据 |
| 快捷指令 | 一键注册 `ptm` 全局命令，任意目录直接启动 |

---

## 数据安全

- `data/traffic.db` 存放所有配置和流量数据
- 升级时自动备份 `data/` 到 `data.bak.YYYYMMDD_HHMMSS/`
- 保留最近 5 个备份，旧备份自动清理

---

## 依赖

核心流量管理仍仅使用 Python 3 标准库。Telegram 功能锁定：

```text
python-telegram-bot[job-queue]==22.8
```

推荐通过 **系统设置 → Telegram 接入 → 安装/更新 Telegram Python 依赖** 安装；也可以手动执行：

```bash
python3 -m pip install --upgrade --target .ptm-packages -r requirements.txt
```

从不含 Telegram 新文件的旧升级器跨版本升级时，如果界面显示“Telegram 组件缺失”，再执行一次 `python3 upgrade.py --force` 即可让新版升级器补齐新增文件。

---

## 许可证

MIT License
