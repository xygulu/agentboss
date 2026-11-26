# 自建中继（私有化部署）

半小时把官方中继换成自己的：一台 1C1G 以上的 VPS、一个域名、Python 3.10+。
自建模式（`RELAY_SELFHOST=1`）只保留核心链路——手机控开发机、语音转写、审批、
会话流——关停所有需要我们运营的端点，并按 `/r/caps` 把能力表下发给手机和 host，
页面自动降级（套餐、TestFlight、语音播报等入口隐藏，不是变灰，是没了）。

对外下载源是公开分发仓 **https://github.com/example/codeboss-relay**（只有中继单文件
和部署脚本）；官网部署说明见 https://example.com/selfhost.html 。
本仓开发者不用 clone：`tools/selfhost/` 就是源，`tools/selfhost/sync-dist.sh`
负责把发布物同步推到分发仓。

## 部署（二选一）

### A. systemd（推荐）

```bash
git clone https://github.com/example/codeboss-relay && cd codeboss-relay
sudo bash install-selfhost.sh                           # 端口 8791
sudo $EDITOR /etc/codeboss/selfhost.env                 # 至少填 RELAY_ADMIN_TOKEN、RELAY_DASHSCOPE_KEY
sudo systemctl restart codeboss-relay
curl -s http://127.0.0.1:8791/r/caps                    # {"selfhost":true,...} 即成功
```

### B. docker compose

```bash
git clone https://github.com/example/codeboss-relay && cd codeboss-relay
cp selfhost.env.sample selfhost.env && $EDITOR selfhost.env
docker compose up -d
```

对外暴露：前面加一层 nginx/caddy 做 TLS 反代到 8791 即可（手机壳校验 http(s)，
正式使用建议配域名 + 证书）。

## 手机接入

手机上第一次进登录页，点底部「使用自建服务器」，填 `https://你的域名`（或扫二维码）
→ 壳弹原生确认框 → 确认后整页切到你的服务器。注册、账号、配对全部从零开始，
和官方中继的数据互不相通。

## 升级

**升级中继**（两种，等价）：

```bash
# 1. 替换文件重启（分发仓里就是 relay.py，先 git pull 再拷）
git -C codeboss-relay pull && sudo cp codeboss-relay/relay.py /opt/codeboss-relay/relay.py \
  && sudo systemctl restart codeboss-relay

# 2. 远程推（RELAY_ADMIN_TOKEN 配了才行）
source /etc/codeboss/selfhost.env
curl -X POST -H "Authorization: Bearer $RELAY_ADMIN_TOKEN" --data-binary @relay.py https://你的域名/r/update
```

**升级 host（开发机端）**：自建中继不发放 host 更新包。两条路：

- host 走 GitHub Releases 自动回退下载（默认行为，无需配置）；
- 或我们定向发的更新包手动覆盖。

## 日常运维

查/解绑已接入的开发机（token 从 env 读，别写死在命令历史里）：

```bash
source /etc/codeboss/selfhost.env
curl -s -H "Authorization: Bearer $RELAY_ADMIN_TOKEN" https://你的域名/r/admin_api/devices
curl -s -X POST -H "Authorization: Bearer $RELAY_ADMIN_TOKEN" -H "Content-Type: application/json" \
     -d '{"device":"要解绑的设备id"}' https://你的域名/r/admin_api/device
```

备份就是备份 `relay.db`（systemd 版默认每 6 小时自动快照，见 RELAY_BACKUP_H）。

## 能力缺失表（自建 vs 官方）

| 能力 | 自建 | 说明 |
|------|------|------|
| 手机控开发机、会话流、审批 | ✅ | 核心链路，齐全 |
| 语音转写 ASR | ✅ 需 key | `RELAY_DASHSCOPE_KEY`，不配则手机上按住说话入口隐藏 |
| 工头大脑 LLM | ✅ 需 key | 同一个 key；可换 OpenAI 兼容地址（RELAY_LLM_BASE/MODEL） |
| 语音播报 TTS | ❌ | 官方 Cherry 品牌服务，自建不提供（手机端开关自动隐藏） |
| 注册 | 看通道 | 配了 SMTP 邮件或阿里短信才开；都不配 = 只能迁移已有号 |
| 套餐/支付/促销 | ❌ | 自建不限量：日额度（消息/转写/智能调用）全放开，台数由 RELAY_MAX_DEVICES 管 |
| 开发机 APK 分发 / iOS | ❌ | 用官方域名下载，或自己分发 |
| Web 管理台 | ❌ | 只保留查/解绑设备的两个 API（见上） |
| 桌宠（deskboss） | ⚠️ 未验证 | 相关端点保留但没在自建环境验过，不承诺能用 |

## 环境变量

逐项「不配会怎样」见 `tools/selfhost/selfhost.env.sample`，注释即文档。
