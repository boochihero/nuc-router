# NUC 软路由 + WiFi 热点 + 透明代理自动化方案

> 状态: 设计稿 v0.1 (2026-08-11)
> 目标机器: sakana-NUC8i5BEH (Ubuntu 24.04.4 LTS)
> 关联: `memory/xray-proxy.md` (VPS 端 Xray 服务), `TOOLS.md` (NUC = HAKO worker)

## 1. 项目目标

在一台 NUC (NUC8i5BEH) 上搭建**全后台服务化**的软路由：

- 有线网卡 `eno1` 作 WAN 接入上级网络（DHCP）
- 无线网卡 `wlp0s20f3` 作 AP 发射 5GHz 热点，下游设备连上即上网
- **透明代理**：下游设备全流量自动走 Xray 节点（日本 VLESS / 新加坡 REALITY），设备端零配置
- 所有组件以 **systemd 服务**常驻，开机自启、崩溃自愈
- 提供 **WebUI** 管理：状态总览、节点切换、热点配置、客户端列表、日志查看
- **一键部署脚本** + 配置备份/回滚 + 断网看门狗，实现"自动化、可恢复"

## 2. 硬件与网络环境（已实测确认）

| 项 | 值 |
|---|---|
| 机器 | Intel NUC8i5BEH |
| 有线网卡 | Intel I219-V 千兆 (`eno1` / enp0s31f6), MAC 1c:69:7a:62:a1:88 |
| 无线网卡 | Intel Wireless-AC 9560 CNVi (`wlp0s20f3`), 2.4/5GHz 2x2, MAC 5c:80:b6:93:1d:a4 |
| 系统 | Ubuntu 24.04.4 LTS (Noble) |
| 当前联网 | wlp0s20f3 连上级 WiFi, 10.111.14.160/22, gw 10.111.15.254 (DHCP) |
| 额外 | 机器上跑着 Docker (docker0 / br-959e1a1820ab) 和 HAKO worker |

### ⚠️ 关键硬件结论：为什么用「网线 WAN + WiFi AP」而不是「纯 WiFi 中继」

Intel 9560 是 **CNVi 单射频卡**。iwlwifi 支持多虚拟接口，但 STA（连上级）+ AP（热点）并发时：

- 所有虚拟接口必须**共享同一信道**（热点只能开在上级 WiFi 同频段同信道）
- 吞吐减半、不稳定，AP 客户端体验差

因此**最优架构是**：

```
[上级路由/光猫] ──网线──> eno1 (WAN, DHCP)
                         NUC
                         ├─ hostapd  : wlp0s20f3 开 5GHz AP
                         ├─ dnsmasq  : DHCP + DNS (192.168.50.1/24)
                         ├─ nftables : NAT + TPROXY 引流
                         └─ xray     : 客户端 TPROXY 透明代理
[下游设备] ──WiFi──> 连热点 → 全流量自动走代理
```

`eno1` 专职 WAN，`wlp0s20f3` 专职 AP，射频无并发冲突。**"网线+WiFi"不是备选，是最优解。**

## 3. 服务栈设计（全部 systemd）

| 服务 | 组件 | 端口/监听 | 作用 |
|---|---|---|---|
| `router-firewall.service` | nftables | - | 加载 NAT + TPROXY 规则，开机最先 |
| `hostapd.service` | hostapd | wlp0s20f3 | 5GHz AP 热点 |
| `dnsmasq.service` | dnsmasq | 53/67 | LAN DHCP + DNS（上游 DoH/DoT 防污染） |
| `xray-proxy.service` | xray | 127.0.0.1:10086 (出站) / TPROXY 12345 (入站) | 透明代理 |
| `router-webui.service` | FastAPI + uvicorn | 0.0.0.0:8080 | Web 管理面板 |
| `router-watchdog.timer` | 脚本 | - | 周期健康检查 + 断网自愈回滚 |

依赖链：`firewall → hostapd/dnsmasq/xray → webui`

### 3.1 网段规划

- WAN: eno1 DHCP（上级 10.111.x.x 或其他）
- LAN/AP: `192.168.50.1/24`（沿用 NUC 历史网段，避免与上级冲突）
- DHCP 池: 192.168.50.100 - 192.168.50.200
- 注意: 若上级网络也是 192.168.50.x 需调整

## 4. 核心配置模板

### 4.1 hostapd (`/etc/hostapd/hostapd.conf`)

```
interface=wlp0s20f3
driver=nl80211
ssid=<SSID_PLACEHOLDER>
hw_mode=a
channel=149
ieee80211n=1
ieee80211ac=1
wmm_enabled=1
country_code=CN
wpa=2
wpa_passphrase=<PASSWORD_PLACEHOLDER>
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
# 5GHz 用 149 信道避开 DFS，稳定第一
```

> Intel 9560 的 5GHz AP 需确保 reg domain 允许信道（`iw reg set CN` 或配置 country_code）。

### 4.2 dnsmasq (`/etc/dnsmasq.d/router.conf`)

```
interface=wlp0s20f3
bind-interfaces
dhcp-range=192.168.50.100,192.168.50.200,12h
dhcp-option=option:router,192.168.50.1
dhcp-option=option:dns-server,192.168.50.1
# DNS 上游用 DoH/DoT（防上级 DNS 污染）
server=1.1.1.1
server=8.8.8.8
```

### 4.3 nftables TPROXY 引流 (`/etc/nftables.d/router.nft`)

透明代理核心：**只劫持 LAN 侧流量，放行本机/代理自身**：

```nft
table inet router {
  chain prerouting {
    type filter hook prerouting priority mangle; policy accept;
    # 本机发出的代理流量不劫持
    ip daddr { 127.0.0.0/8, 192.168.50.0/24, 10.111.0.0/16 } return
    # 非 LAN 来源跳过
    iifname != "wlp0s20f3" return
    # TCP 全量 TPROXY 到 12345
    tcp dport != { 22, 8080 } meta l4proto tcp tproxy to 127.0.0.1:12345
  }
}
```

实际规则以部署时的完整版为准（含 UDP、保留端口、VPS 直连白名单等）。

### 4.4 Xray 客户端 (`/etc/xray/client.json`)

- 入站: dokodemo-door, port 12345, `"network": "tcp,udp"`, `"followRedirect": true`
- 出站: 主节点日本 (VLESS+WS+TLS) / 备用新加坡 (REALITY)，配置从 VPS 端提取（脱敏，密钥不入库）
- 注意: REALITY 节点**不填** pinnedPeerCertSha256（LE 证书指纹会变，教训见 xray-proxy.md）

### 4.5 路由规则

```
ip rule add fwmark 1 table 100
ip route add local 0.0.0.0/0 dev lo table 100
```

### 4.6 代理开关机制（WebUI 实时切换）

“是否启用代理”通过 **nftables 规则原子切换**实现，不重启防火墙：

- **启用**: `nft -f` 加载 TPROXY 劫持规则（lan 流量 → xray tproxy 12345）
- **禁用**: `nft -f` 移除劫持规则（lan 流量 → 直连 NAT 出网），xray 服务可保持运行或随机关闭
- 切换脚本: `/usr/local/bin/router-proxy-toggle.sh [on|off]`，写入状态到 `/etc/router-webui/state/proxy`（WebUI 读取显示）
- 安全: 切换前备份当前 nft 规则集，失败自动还原

### 4.7 Xray 客户端配置由 WebUI 管理

- Xray 出站配置模板 `/etc/xray/client.json` 由 WebUI 表单生成（不再手改）
- 保存流程: 表单 → JSON 模板渲染 → `xray run -test -config` 校验 → 备份旧配置 → 写入 + `systemctl reload xray-proxy`
- 支持多节点（主/备），WebUI 一键切换当前激活节点

### 4.8 订阅导入（Clash YAML 解析）

WebUI 支持从**订阅链接**导入节点（如 `https://bocchi.japaneast.cloudapp.azure.com/sub/clash.yaml`），也支持直接粘贴 YAML 文本。

**流程**: URL/文本 → 拉取 → 解析 YAML → 提取 `proxies:` → 转换成 Xray outbound JSON → 预览（可选节点）→ 确认导入 → 校验 + 备份 + reload

**Clash → Xray 映射表**:

| Clash type | Xray outbound | 说明 |
|---|---|---|
| `vless` | vless | 需 settings.vnext + streamSettings |
| `vmess` | vmess | 同上 |
| `ss` | shadowsocks | 支持 |
| `trojan` | trojan | 支持 |
| `hysteria2` | hysteria2 | Xray ≥1.8 支持 |
| `ssr` / `tuic` | ❌ 不支持 | Xray 无此协议，跳过并在预览中标注提示 |

**关键字段映射**（以 vless 为例）: `name→tag`, `server→address`, `port→port`, `uuid→id`, `network(ws/grpc/tcp)→streamSettings.network`, `ws-opts.path→wsSettings.path`, `ws-opts.headers.Host→wsSettings.headers.Host`, `tls:true→security=tls + servername`, `reality(publicKey/shortId/fingerprint)→REALITY 参数`

**订阅管理**: 订阅 URL 存 `/etc/router-webui/subscriptions.json`，支持手动刷新；可加 cron 定时刷新节点列表。

**安全**: 仅允许 http/https URL（SSRF 防护：拒绝内网/保留地址），响应大小限制（如 2MB），解析失败返回具体错误行；导入前必须预览确认，不自动覆盖当前节点。

## 5. WebUI 设计

### 5.1 技术选型

- **后端**: Python FastAPI + uvicorn（systemd 托管，Python 3.12 自带）
- **前端**: 原生 HTML + JS 单页（无构建步骤，避免 npm 依赖；样式内联或单 CSS）
- **权限**: webui 用户加入 `sudo` 组，仅允许白名单 systemctl 命令（`systemctl restart hostapd` 等）；或用 polkit 精确授权

### 5.2 API 端点

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/status` | 总览: WAN IP、AP 状态、xray 状态、流量 |
| GET | `/api/clients` | 当前连接客户端列表（`iw dev wlp0s20f3 station dump`） |
| GET/POST | `/api/config/hotspot` | 查看/修改 SSID、密码、信道（改后自动重启 hostapd） |
| **GET** | **`/api/proxy`** | **代理总览: 启用状态、当前节点、节点列表** |
| **POST** | **`/api/proxy/toggle`** | **代理总开关 on/off（nftables 原子切换）** |
| **PUT** | **`/api/proxy/config`** | **保存 Xray 节点配置（校验→备份→reload）** |
| **POST** | **`/api/proxy/node/{id}/activate`** | **切换主/备节点** |
| **POST** | **`/api/proxy/test`** | **测试当前节点连通性（curl 经代理出口 IP）** |
| **POST** | **`/api/subscription`** | **添加订阅 URL** |
| **GET** | **`/api/subscription`** | **列出订阅 + 各订阅节点数** |
| **POST** | **`/api/subscription/{id}/refresh`** | **重新拉取订阅** |
| **DELETE** | **`/api/subscription/{id}`** | **删除订阅** |
| **POST** | **`/api/proxy/import`** | **导入（URL 或粘贴 YAML），返回节点预览** |
| **POST** | **`/api/proxy/import/confirm`** | **确认导入选中节点 → 写入 xray 配置** |
| POST | `/api/node/switch` | 切换代理节点（日本/新加坡，重载 xray） |
| POST | `/api/service/{name}/restart` | 重启指定服务 |
| GET | `/api/logs/{service}` | 查看服务日志（journalctl 尾部） |
| POST | `/api/backup` | 一键备份配置（tar 到 /etc/router-backups/） |
| POST | `/api/rollback` | 回滚到上次备份 |

### 5.3 页面

单页 `index.html`，左侧导航三个标签页：

1. **状态**：状态卡片（WAN/AP/代理/客户端数）、客户端列表、备份/回滚按钮
2. **代理设置**：
   - 总开关（是否启用代理，switch 控件，调 `/api/proxy/toggle`）
   - **订阅导入区块**：订阅 URL 输入框 + 「拉取」→ 节点预览列表（checkbox 勾选 + 协议标注，不支持的协议灰色提示）→ 「导入选中」；已存订阅列表带刷新/删除按钮
   - 节点列表（主/备），显示当前激活节点、一键切换、一键测速
   - 节点编辑表单：协议（VLESS/VMess/REALITY）、地址、端口、UUID、TLS/SNI、WS 路径、伪装 host、REALITY publicKey/shortId 等；保存时后端校验 JSON + 测试 + reload
3. **热点设置**：SSID/密码/信道表单 + 日志滚动查看

深色主题，手机可访问（WebUI 自己暴露在 WAN 或仅 LAN——默认仅 LAN + 可选 token 认证）。

## 6. 一键部署 (`install.sh`)

```
install.sh [--dry-run] [--ssid X] [--password Y]
1. 环境准备: 安装 hostapd dnsmasq nftables xray fastapi/uvicorn
2. 生成配置: 模板渲染 → /etc/{hostapd,dnsmasq.d,nftables.d,xray}
3. 安装 systemd units + watchdog timer
4. 首次备份配置 (router-backups/)
5. 启动服务链并自检: firewall → hostapd → dnsmasq → xray → webui
6. 验证: 本机 curl 代理出口 IP / 检查 TPROXY 规则加载
```

幂等：重复运行只更新配置不重复安装；所有变更前自动备份。

## 7. 自动化与运维

### 7.1 断网自救看门狗（关键！）

这台 NUC 是 HAKO worker，我（Bocchi）通过它的网络远程操作。**配置失误 = 断网 = 失去控制**。因此：

- 任何网络变更前：自动备份 `/etc/router-backups/` + 记录变更时间戳
- `router-watchdog.timer`（每 5 分钟）：检查 eno1 是否拿到 IP + 外网连通性（ping 网关 + curl VPS）
- 连续 2 次失败 → 自动执行上次备份回滚 → 重启网络服务
- 手工操作时：`systemctl start router-watchdog-manual.service` 可临时启用"10 分钟无确认自动回滚"

### 7.2 日志与监控

- journald 集中日志：`journalctl -u hostapd -u xray-proxy -u dnsmasq`
- WebUI 日志页直接读 journalctl
- 可选: 接 Prometheus node_exporter + 现有监控（见 prometheus-monitoring-setup-guide.md）

### 7.3 备份

- 配置目录: `/etc/router-backups/<timestamp>/`
- 定期: cron 每日备份 + 保留 7 份
- WebUI 一键备份/回滚

## 8. 安全要点

- WebUI **默认只监听 LAN 侧**（bind 192.168.50.1），需要远程管理时经 SSH 隧道
- 可选 token 认证（`X-Auth-Token` header），token 存 `/etc/router-webui/secret`（0600）
- nftables 放行白名单: SSH(22) 管理、VPS 节点 IP 直连、局域网
- Xray 配置不含明文密钥入库；UUID/密码用 placeholder + 部署时注入
- hostapd 用 WPA2-PSK；5GHz 信道选非 DFS 规避雷达检测断连
- 下游设备互访默认隔离（可选 `ap_isolate=1`）

## 9. 路线图

- [ ] **Phase 1**: 手动搭通最小链路（网线 WAN + AP + NAT），验证上网
- [ ] **Phase 2**: Xray TPROXY 透明代理接入，下游设备无感翻墙
- [ ] **Phase 3**: systemd 服务化 + watchdog + 一键部署脚本
- [ ] **Phase 4**: WebUI（状态/切节点/改热点/日志/备份回滚）
- [ ] **Phase 5**: 打磨（流量统计、客户端限速、多 SSID、访客网络隔离）
- [ ] **Phase 6**: 订阅自动刷新（cron 定时拉取订阅更新节点列表）

## 10. 待确认事项

1. ~~eno1 上级网络形态~~ → **已确认 (2026-08-11): DHCP，无静态 IP**（上级路由器 DHCP 池 10.111.12.0/22，eno1 插线自动拿地址）
2. 当前 10.111.x.x WiFi 切走后是否有影响？（NUC 现靠它联网，切 AP 前必须 eno1 就绪）
3. 代理节点主/备：日本 VLESS 主 + 新加坡 REALITY 备？
4. 热点 SSID / 密码偏好？
5. WebUI 是否仅内网访问 + token 认证即可？
