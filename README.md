# NUC 软路由 + WiFi 热点 + 透明代理

在 Intel NUC 上搭建全后台服务化的软路由：有线网卡作 WAN，无线网卡作 AP，全流量透明代理走 Xray 节点，WebUI 管理面板。所有组件 systemd 服务化，一键部署，配置可备份/回滚，带断网看门狗自愈。

## 架构

```
[上级路由/光猫] ──网线──> eno1 (WAN, DHCP)
                         NUC (NUC8i5BEH, Ubuntu 24.04)
                         ├─ hostapd  : wlp0s20f3 开 5GHz AP (ch 149)
                         ├─ dnsmasq  : DHCP + DNS (192.168.50.1/24)
                         ├─ nftables : NAT + TPROXY 引流
                         ├─ xray     : 客户端 TPROXY 透明代理 (:12345)
                         └─ FastAPI  : WebUI 管理面板 (:8080)
[下游设备] ──WiFi──> 连热点 → 全流量自动走代理
```

## 网段规划

| 角色 | 接口 | 地址 | 说明 |
|---|---|---|---|
| WAN | eno1 | DHCP | 上级网络 DHCP (10.111.x.x) |
| LAN/AP | wlp0s20f3 | 192.168.50.1/24 | DHCP 池 192.168.50.100-200 |
| Xray TPROXY | lo | 127.0.0.1:12345 | 透明代理入站 |
| WebUI | wlp0s20f3 | 192.168.50.1:8080 | 管理面板 |

## 部署

### 前置条件

- Intel NUC (NUC8i5BEH) 或类似硬件
- Ubuntu 24.04 LTS
- 有线网卡 (eno1) 已插网线，无线网卡 (wlp0s20f3) 可用
- root 权限

### 一键部署

```bash
# 基础部署（使用默认 SSID 和密码）
sudo ./install.sh

# 指定 SSID 和 WiFi 密码
sudo ./install.sh --ssid "MyRouter-5G" --password "MySecurePass123"

# 预览模式（不实际执行）
sudo ./install.sh --dry-run --ssid "MyRouter-5G" --password "MySecurePass123"
```

部署流程：
1. 检测 Ubuntu 24.04 环境
2. 安装 hostapd、dnsmasq、nftables、xray、Python 依赖
3. 渲染配置模板到 `/etc/`
4. 安装 systemd 服务和看门狗定时器
5. 首次配置备份
6. 依序启动服务链：firewall → hostapd → dnsmasq → xray → webui
7. 自检验证

幂等：重复运行只更新配置不重复安装，所有变更前自动备份。

## WebUI 管理面板

默认监听 `http://192.168.50.1:8080`，需在连接热点的设备上访问。

### 认证

- Token 认证：Header `X-Auth-Token: <token>`
- Token 存储在 `/etc/router-webui/secret` (权限 0600)
- 首次部署时自动生成随机 token

### 页面功能

**状态页**：
- WAN IP、AP 状态、Xray 状态、代理开关状态
- 连接客户端列表（MAC、IP、信号强度）
- 备份/回滚按钮

**代理设置页**：
- 代理总开关（nftables 原子切换，不重启任何服务）
- 订阅导入：输入订阅 URL 或粘贴 Clash YAML → 预览节点 → 选中导入
- 节点列表：显示主/备节点，一键切换、一键测速
- 节点编辑表单：协议/地址/端口/UUID/TLS/WS 等字段
- 保存流程：JSON 渲染 → xray run -test 校验 → 备份 → reload

**热点设置页**：
- SSID / 密码 / 信道 修改（保存后自动重启 hostapd）
- 各服务日志查看

### API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/status | 系统状态总览 |
| GET | /api/clients | 客户端列表 |
| GET/POST | /api/config/hotspot | 热点配置查看/修改 |
| GET | /api/proxy | 代理总览（状态、节点列表） |
| POST | /api/proxy/toggle | 代理开关 on/off |
| PUT | /api/proxy/config | 保存 Xray 节点配置 |
| POST | /api/proxy/node/{id}/activate | 切换激活节点 |
| POST | /api/proxy/test | 测试节点连通性 |
| POST | /api/subscription | 添加订阅 URL |
| GET | /api/subscription | 列出订阅 |
| POST | /api/subscription/{id}/refresh | 刷新订阅 |
| DELETE | /api/subscription/{id} | 删除订阅 |
| POST | /api/proxy/import | 导入（URL 或粘贴 YAML），返回节点预览 |
| POST | /api/proxy/import/confirm | 确认导入选中节点 |
| POST | /api/service/{name}/restart | 重启指定服务 |
| GET | /api/logs/{service} | 查看服务日志 |
| POST | /api/backup | 配置备份 |
| POST | /api/rollback | 回滚到上次备份 |

## 运维

### 服务管理

```bash
systemctl status router-firewall  # 防火墙/NAT
systemctl status router-ap         # WiFi 热点
systemctl status router-dns        # DHCP/DNS
systemctl status router-xray       # 透明代理
systemctl status router-webui      # WebUI
systemctl status router-watchdog.timer  # 看门狗
```

### 代理开关

```bash
# 通过 WebUI POST /api/proxy/toggle
# 或命令行：
/usr/local/bin/router-proxy-toggle.sh on
/usr/local/bin/router-proxy-toggle.sh off
```

状态持久化到 `/etc/router-webui/state/proxy`。

### 配置备份与回滚

```bash
/usr/local/bin/router-backup.sh              # 备份当前配置
/usr/local/bin/router-rollback.sh            # 回滚到上次备份
```

备份目录：`/etc/router-backups/<timestamp>/`

### 断网看门狗

- `router-watchdog.timer` 每 5 分钟执行健康检查
- 检查 eno1 IP + 外网连通性
- 连续 2 次失败 → 自动回滚到上次备份

## 订阅导入

支持的协议转换（Clash YAML → Xray outbound）：

| Clash 类型 | Xray 出站 | 说明 |
|---|---|---|
| vless | vless | 完整支持 |
| vmess | vmess | 完整支持 |
| ss | shadowsocks | 完整支持 |
| trojan | trojan | 完整支持 |
| hysteria2 | hysteria2 | Xray >= 1.8 支持 |
| ssr / tuic | 不支持 | 导入时标注并跳过 |

安全：仅允许 http/https URL，SSRF 防护拒绝内网/保留地址，响应大小限制 2MB。

## 故障排查

```bash
# 检查服务状态
systemctl status router-firewall router-ap router-dns router-xray router-webui

# 查看日志
journalctl -u router-firewall -f
journalctl -u router-xray -f

# 检查 AP 状态
iw dev wlp0s20f3 info
iw dev wlp0s20f3 station dump

# 检查 nftables 规则
nft list ruleset

# 检查 xray 配置
xray run -test -config /etc/xray/client.json

# 手动回滚配置
/usr/local/bin/router-rollback.sh
```

## 文件结构

```
nuc-router/
├── README.md
├── docs/design.md
├── install.sh
├── configs/
│   ├── hostapd.conf
│   ├── dnsmasq.conf
│   ├── nftables-router.nft
│   └── xray-client.json
├── systemd/
│   ├── router-firewall.service
│   ├── router-ap.service
│   ├── router-dns.service
│   ├── router-xray.service
│   ├── router-webui.service
│   ├── router-watchdog.service
│   └── router-watchdog.timer
├── scripts/
│   ├── router-proxy-toggle.sh
│   ├── router-backup.sh
│   ├── router-rollback.sh
│   └── router-watchdog.sh
└── webui/
    ├── main.py
    ├── subscription.py
    ├── clash2xray.py
    ├── requirements.txt
    └── static/index.html
```
