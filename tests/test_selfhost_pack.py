#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""自建包自检：python3 -m pytest tests/test_selfhost_pack.py -v
   env 样例、安装脚本、部署文档三件套都在，且覆盖升级/运维/桌宠声明。"""
import os
import subprocess

ROOT = os.path.join(os.path.dirname(__file__), "..")
ENV = os.path.join(ROOT, "tools", "selfhost", "selfhost.env.sample")
INSTALL = os.path.join(ROOT, "tools", "selfhost", "install-selfhost.sh")
COMPOSE = os.path.join(ROOT, "tools", "selfhost", "docker-compose.yml")
DOCS = os.path.join(ROOT, "docs", "selfhost-relay.md")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def test_env_sample_lists_dependencies():
    src = _read(ENV)
    for key in ("RELAY_SELFHOST=1", "RELAY_DASHSCOPE_KEY", "RELAY_SMTP_HOST",
                "RELAY_MAX_DEVICES", "RELAY_ADMIN_TOKEN",
                "RELAY_LLM_BASE", "RELAY_LLM_MODEL", "RELAY_TURN_URLS"):
        assert key in src, f"env 样例缺 {key}"


def test_install_script_parses():
    subprocess.run(["bash", "-n", INSTALL], check=True)
    src = _read(INSTALL)
    assert ".service" in src, "安装脚本没写 systemd unit"
    assert "EnvironmentFile" in src, "unit 没挂 EnvironmentFile，env 改了不生效"
    assert "aiohttp" in src, "没装 aiohttp（relay 的核心依赖）"
    assert "../../relay/relay.py" in src, "relay.py 相对路径错（一级向上= tools/relay/，脚本永远死在守卫）"


def test_compose_and_docs_cover_upgrade_and_admin():
    assert os.path.exists(COMPOSE), "缺 docker-compose.yml"
    assert "env_file" in _read(COMPOSE), "compose 没挂 env_file"
    assert "../../relay/relay.py" in _read(COMPOSE), "compose 挂的 ./relay.py 不存在（Docker 会建成目录）"
    docs = _read(DOCS)
    assert "/r/update" in docs, "文档没写升级通道 /r/update"
    assert "/r/admin_api/devices" in docs, "文档没写运维端点 /r/admin_api/devices"
    assert "未验证" in docs, "文档没写桌宠「未验证不承诺」声明"
    assert "GitHub" in docs, "文档没写 host 升级 GitHub 回退"
    assert "$RELAY_ADMIN_TOKEN" in docs, "文档命令示例没用 $RELAY_ADMIN_TOKEN 占位（$TOKEN 未定义，照抄 401）"


def test_sync_dist_produces_runnable_pack(tmp_path):
    # 分发仓同步：5 个文件齐、compose 挂载改平、README 去 tools/selfhost/ 前缀
    import subprocess
    sync = os.path.join(ROOT, "tools", "selfhost", "sync-dist.sh")
    subprocess.run(["bash", "-n", sync], check=True)
    env = dict(os.environ, CB_DIST_NOPUSH="1")
    subprocess.run(["bash", sync, str(tmp_path / "relay-dist")],
                   check=True, env=env, cwd=ROOT)
    d = tmp_path / "relay-dist"
    for f in ("relay.py", "install-selfhost.sh", "docker-compose.yml",
              "selfhost.env.sample", "README.md"):
        assert (d / f).exists(), f"分发仓缺 {f}"
    assert (d / "relay.py").read_text(encoding="utf-8") == _read(
        os.path.join(ROOT, "relay", "relay.py")), "relay.py 与主仓源码不一致"
    assert "./relay.py:/app/relay.py:ro" in (d / "docker-compose.yml").read_text(encoding="utf-8")
    readme = (d / "README.md").read_text(encoding="utf-8")
    assert "tools/selfhost/" not in readme, "README 残留主仓路径（分发仓是平铺布局）"
    # 分发仓布局下安装脚本必须自足：同级就有 relay.py（双路径解析）
    inst = (d / "install-selfhost.sh").read_text(encoding="utf-8")
    assert '"$HERE/relay.py"' in inst and '"$HERE/../../relay/relay.py"' in inst


def test_sync_dist_secret_scan_blocks_leak(tmp_path):
    # 公开仓闸门：源码里出现密钥样字面量时 sync 必须拒推，且镜像不留残影
    import subprocess
    sync = os.path.join(ROOT, "tools", "selfhost", "sync-dist.sh")
    mirror = str(tmp_path / "relay-dist")
    env = dict(os.environ, CB_DIST_NOPUSH="1")
    # 干净替身先同步一份基线
    clean = tmp_path / "clean_relay.py"
    clean.write_text(_read(os.path.join(ROOT, "relay", "relay.py")), encoding="utf-8")
    subprocess.run(["bash", sync, mirror], check=True, cwd=ROOT,
                   env=dict(env, CB_DIST_SRC=str(clean)))
    good = _read(os.path.join(mirror, "relay.py"))
    # 注入一枚假密钥 → 必须拒推，镜像保持基线
    # （拼接构造：本文件也是 OSS 导出+扫描对象，不能落密钥样字面量）
    fake_key = "LTAI" + "hackFakeKey12345678"
    leaky = tmp_path / "leaky_relay.py"
    leaky.write_text(good + "\nFAKE = %r\n" % fake_key, encoding="utf-8")
    r = subprocess.run(["bash", sync, mirror], cwd=ROOT,
                       env=dict(env, CB_DIST_SRC=str(leaky)),
                       capture_output=True, text=True)
    assert r.returncode != 0, "泄漏样本没被拦下"
    assert "秘密扫描命中" in r.stdout + r.stderr
    assert _read(os.path.join(mirror, "relay.py")) == good, "拒推时镜像不该被半更新"
    assert fake_key not in "".join(
        _read(os.path.join(mirror, f)) for f in os.listdir(mirror) if f.endswith((".py", ".sh", ".md", ".yml")))
