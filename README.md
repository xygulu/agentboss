# agentboss —— 手机上的 agent 控制端

把 Claude Code / Codex / OpenCode 等 coding agent 装进手机：会话流、审批、语音、
产出物，一个 App 全收。**手机是遥控器，开发机是引擎**——agent 跑在你自己的电脑上，
手机只负责看、批、说、收。

三件套架构：

```
手机 App ──▶ 中继（relay.py，单文件）◀── host（开发机守护进程）
             能力协商 /r/caps：官方托管版全量，自建版核心链路
```

- **host**：装在你跑 agent 的那台电脑（macOS / Linux / Windows），管理 PTY 会话、
  屏幕/镜像、工具结果与审批；一个 agent 内核多端共享。
- **中继**：一个 `relay.py` 单文件（依赖仅 aiohttp），负责手机与 host 的配对、
  转发与离线消息；官方托管版与自建版是同一份代码，用 `/r/caps` 下发能力表，
  缺运营端的端点自动隐藏。
- **App**：Android（`clients/android`）与 iOS（`clients/ios`），登录页可一键切自建服务器。

能力协商细节见 [docs/selfhost-relay.md](docs/selfhost-relay.md)。

## 三十分钟跑起来

前提：一台 1C1G+ 的 VPS（或家里常开的机器）、一个域名、Python 3.10+。

### ① 起自建中继

**systemd（推荐）**

```bash
git clone <本仓> && cd agentboss
sudo bash tools/selfhost/install-selfhost.sh          # 端口 8791
sudo $EDITOR /etc/codeboss/selfhost.env               # 至少填 RELAY_ADMIN_TOKEN
sudo systemctl restart codeboss-relay
curl -s http://127.0.0.1:8791/r/caps                  # {"selfhost":true,...} 即成功
```

**或 docker compose**

```bash
cp tools/selfhost/selfhost.env.sample selfhost.env && $EDITOR selfhost.env
docker compose -f tools/selfhost/docker-compose.yml up -d
```

不想装任何依赖也可以直跑：`pip install aiohttp && RELAY_SELFHOST=1 python3 relay/relay.py`。

对外暴露前面加一层 nginx/caddy 做 TLS 反代到 8791 即可；语音转写（可选）配
`RELAY_DASHSCOPE_KEY`。详文与变量说明见 [docs/selfhost-relay.md](docs/selfhost-relay.md)。

### ② 开发机装 host 并接入

```bash
# macOS / Linux
bash host/install.sh            # Windows: host/install.ps1；Linux 服务器: host/install-linux.sh
python3 host/server.py --enroll https://你的域名 我的开发机
```

`--enroll` 把 host 登记到你的中继；配对信息落在本机配置里，重跑即换中继。

### ③ 手机接入

手机装 agentboss App（官方渠道下载，或自行构建：`bash clients/android/build.sh`；
自行构建需自备签名 keystore——build.sh 按 `products/agentboss/product.json` 里的
keystore 路径找，缺了会直接报错退出，调试可用 `keytool` 自建一个）。
第一次进登录页，点底部**「使用自建服务器」**，填 `https://你的域名`（或扫 host 打出的
二维码）→ 确认后注册、配对全部从零开始，和任何其他中继的数据互不相通。

## 它能干什么

- **会话流**：手机上看 agent 的实时终端流（全彩镜像、滚动历史持久化），断链自动重连续流。
- **发消息**：手机打字/语音 → host 注入当前 agent 会话，逐字流回显。
- **审批**：agent 的敏感操作在手机上弹出「在确认什么/选什么/后果」，一键放行或驳回；
  不确定的操作由风险二审把关。
- **产出物**：预览网页/装 App/拍屏，diff 编辑回流会话。
- **多 agent**：claude / codex / opencode / hermes 一套界面，ACP 适配层接入新 agent。

## License

[AGPL-3.0](LICENSE)。用它在自己的机器上遥控自己的 agent，随便改、随便自部署；
做成对外服务时，请按 AGPL 条款开源你的修改版。

官方托管版（免部署、我们运维中继与更新通道）请从 App 内引导页了解。
