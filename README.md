# PVE 流量控制管理器

基于 Python 开发的 Proxmox VE 虚拟机流量控制管理器。以组为单位批量管理虚拟机流量，超出限额自动关机。

- 兼容 PVE 8 / PVE 9
- 同时支持 KVM (qemu) 和 LXC 容器
- CLI 菜单式交互，零外部依赖
- 前台管理 + 后台 crontab 监控

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
wget -qO- https://github.com/smmya/pve-traffic-manager/archive/refs/heads/main.zip | bsdtar -xvf- && mv pve-traffic-manager-main pve-traffic-manager && cd pve-traffic-manager && python3 manager.py
```

---

## 快速开始

### 1. 前台交互

```bash
cd pve-traffic-manager
python3 manager.py
```

CLI 菜单结构：

```
=== PVE 流量控制管理器 ===

[主菜单]
1. 组管理        → 创建/修改/删除管理组，设定流量限额
2. 虚拟机管理    → 扫描 VM，批量加入组（支持 100-110 范围写法）
3. 流量监控      → 查看流量概览/详情/历史，手动重置
4. 系统设置      → 查看配置、安装/卸载 crontab、操作日志
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

也可以直接用命令行：

```bash
# 手动安装 crontab（5分钟间隔，默认）
python3 -c "
import subprocess, sys, os
d = os.path.dirname(os.path.abspath('monitor.py'))
line = f'*/5 * * * * {sys.executable} {d}/monitor.py # pve-traffic-manager monitor'
subprocess.run(['crontab', '-'], input=f'{line}\n', text=True)
print('已安装')
"

### 3. 升级

```bash
cd pve-traffic-manager
python3 upgrade.py              # 检查并升级到最新版
python3 upgrade.py --check      # 仅检查是否有新版本
python3 upgrade.py --force      # 强制升级（跳过版本比较）
```

---

## 核心逻辑

```
组内每台 VM 独立判断：
    单台 VM 累计流量 (入站+出站) >= 组设定的限额
    → 1. 若配置了 notify_cmd，先执行通知命令
    → 2. 记录操作日志
    → 3. 关闭该 VM（不影响组内其他 VM）
```

- **组限额 = 每台 VM 的上限**，不是组内共享额度
- VM-A 超了只关 VM-A，VM-B 照常运行
- 流量完全手动重置，不做自动周期清零
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
| 超限关机 | 每台 VM 独立判断，超限自动 qm shutdown / pct shutdown |
| 通知接口 | `notify_cmd` 字段可配自定义脚本，支持变量替换 |
| 历史数据 | SQLite 存储流量日志（每次采集的增量）和操作日志 |
| 流量重置 | 前台手动触发整组重置，数据不丢失 |
| 升级支持 | `upgrade.py` 从 GitHub 拉取最新版，自动备份数据 |

---

## 数据安全

- `data/traffic.db` 存放所有配置和流量数据
- 升级时自动备份 `data/` 到 `data.bak.YYYYMMDD_HHMMSS/`
- 保留最近 5 个备份，旧备份自动清理

---

## 依赖

**零外部依赖。** 仅使用 Python 3 标准库：`sqlite3`, `subprocess`, `json`, `os`, `re`, `urllib`, `hashlib`

PVE 8/9 自带 Python 3，无需 `pip install` 任何包。

---

## 许可证

MIT License
