# SPDX-License-Identifier: AGPL-3.0-or-later

# tests/test_relay_apk_slot.py  —— 跑法：.venv/bin/python -m pytest tests/test_relay_apk_slot.py -q
import sys, json, os
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import relay

def _dev_db(product):
    db = relay.db_init(":memory:")
    uid = db.execute("INSERT INTO users(name,pass_hash,salt) VALUES('u','h','s')").lastrowid
    db.execute("INSERT INTO devices(id,name,secret,owner,product) VALUES('d1','dev','sec',?,?)",
               (uid, product))
    return db

def test_store_agentboss_leaves_codeboss_slot_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "APK_FILE", str(tmp_path / "codeboss-apk.bin"))
    monkeypatch.setattr(relay, "APK_META", str(tmp_path / "codeboss-apk.json"))
    monkeypatch.setattr(relay, "APK_DIR", str(tmp_path / "apkpkgs"))
    (tmp_path / "codeboss-apk.bin").write_bytes(b"C" * 20480)   # codeboss 存量包
    app = {"db": _dev_db("agentboss")}
    relay._apk_store(app, "d1", b"A" * 20480, {"code": 2, "ver": "0.2.0"})
    assert (tmp_path / "agentboss-apk.bin").read_bytes() == b"A" * 20480
    assert (tmp_path / "codeboss-apk.bin").read_bytes() == b"C" * 20480   # 一字节不动
    assert json.loads((tmp_path / "agentboss-apk.json").read_text())["code"] == 2
    assert (tmp_path / "apkpkgs" / "agentboss-2.apk").exists()            # 归档带产品前缀
    assert relay._apk_meta("agentboss")["code"] == 2
    assert relay._apk_meta("codeboss") == {}                              # 各槽 meta 独立

def test_code_meta_reads_legacy_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "APK_DIR", str(tmp_path))
    (tmp_path / "189.json").write_text('{"code": 189, "ver": "0.11.24"}')   # 老命名
    assert relay._apk_code_meta("codeboss", 189)["ver"] == "0.11.24"
    assert relay._apk_code_meta("agentboss", 189) == {}                     # 不跨产品读老名

class _Req:   # 最小 request 替身：h_apk_ver/灰度只吃 query/headers/cookies
    def __init__(self, query=None):
        self.query = query or {}
        self.headers = {}
        self.cookies = {}

def test_apk_ver_answers_per_product(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "APK_FILE", str(tmp_path / "cb.bin"))
    monkeypatch.setattr(relay, "APK_META", str(tmp_path / "cb.json"))
    monkeypatch.setattr(relay, "APK_DIR", str(tmp_path / "apkpkgs"))
    os.makedirs(tmp_path / "apkpkgs")
    for prod, code, ver in (("codeboss", 190, "0.11.25"), ("agentboss", 2, "0.2.0")):
        binp, metap = relay._apk_slot(prod)
        (Path(binp)).write_bytes(b"A" * 20480)
        (Path(metap)).write_text(json.dumps({"code": code, "ver": ver, "product": prod}))
    out = relay._apk_ver_answer({}, _Req({"product": "agentboss"}), "agentboss")
    assert out["code"] == 2 and out["url"] == "/agentboss.apk"
    out = relay._apk_ver_answer({}, _Req({}), "codeboss")
    assert out["code"] == 190 and out["url"] == "/codeboss.apk"

def test_apk_ver_bad_product_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "APK_FILE", str(tmp_path / "cb.bin"))
    monkeypatch.setattr(relay, "APK_META", str(tmp_path / "cb.json"))
    (Path(relay.APK_FILE)).write_bytes(b"A" * 20480)
    (Path(relay.APK_META)).write_text('{"code": 190, "ver": "0.11.25"}')
    out = relay._apk_ver_answer({}, _Req({"product": "evil;drop"}), None)
    assert out["code"] == 190 and out["url"] == "/codeboss.apk"

# ---- I-2：codeboss 老归档是裸 <code>.apk 命名——灰度/回退要能双读，否则 pin 静默失效 ----

def test_apk_ver_canary_uses_legacy_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "APK_FILE", str(tmp_path / "cb.bin"))
    monkeypatch.setattr(relay, "APK_META", str(tmp_path / "cb.json"))
    monkeypatch.setattr(relay, "APK_DIR", str(tmp_path / "apkpkgs"))
    os.makedirs(tmp_path / "apkpkgs")
    binp, metap = relay._apk_slot("codeboss")
    Path(binp).write_bytes(b"A" * 20480)
    Path(metap).write_text(json.dumps({"code": 190, "ver": "0.11.25"}))
    (tmp_path / "apkpkgs" / "187.apk").write_bytes(b"B" * 20480)          # 老命名归档
    (tmp_path / "apkpkgs" / "187.json").write_text('{"code": 187, "ver": "0.11.24"}')
    monkeypatch.setattr(relay, "_canary_cfg",
                        lambda: {"default": {"ver": "0.11.24", "apk": 187}})
    out = relay._apk_ver_answer({}, _Req({}), "codeboss")
    assert out["code"] == 187 and out["url"] == "/codeboss-187.apk"

def test_apk_code_path_reads_legacy_name(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "APK_DIR", str(tmp_path))
    (tmp_path / "187.apk").write_bytes(b"B" * 20480)                      # 只有老命名
    p = relay._apk_code_path("codeboss", "187")
    assert os.path.exists(p) and p.endswith("187.apk")
    assert not os.path.exists(relay._apk_code_path("agentboss", "187"))   # agentboss 不读裸名

# ---- I-3：「全量发布」要同时放开各产品线自己的灰度基线，否则 agentboss 被钉旧版 ----

def test_promote_clears_per_product_baseline():
    c = {"default": {"ver": "0.1.0", "apk": 1},
         "default_by_product": {"agentboss": {"apk": 2}},
         "rules": [{"id": "r1"}]}
    relay._canary_promote(c, "0.2.0", 3)
    assert c["default"] == {"ver": "0.2.0", "apk": 3}
    assert c["rules"] == []
    assert "default_by_product" not in c


# ---- 单 host 双产物：尾帧 product 优先于设备产品线落槽（2026-11-18 发版）----

def test_store_follows_payload_product_not_device(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "APK_FILE", str(tmp_path / "codeboss-apk.bin"))
    monkeypatch.setattr(relay, "APK_META", str(tmp_path / "codeboss-apk.json"))
    monkeypatch.setattr(relay, "APK_DIR", str(tmp_path / "apkpkgs"))
    app = {"db": _dev_db("agentboss")}     # 设备登记的是 agentboss 线
    relay._apk_store(app, "d1", b"B" * 20480, {"code": 190, "ver": "0.11.25",
                                               "product": "codeboss"})
    assert (tmp_path / "codeboss-apk.bin").read_bytes() == b"B" * 20480
    assert json.loads((tmp_path / "codeboss-apk.json").read_text())["code"] == 190
    assert not (tmp_path / "agentboss-apk.bin").exists()   # 不落设备自己的槽

def test_store_bad_payload_product_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "APK_FILE", str(tmp_path / "codeboss-apk.bin"))
    monkeypatch.setattr(relay, "APK_META", str(tmp_path / "codeboss-apk.json"))
    monkeypatch.setattr(relay, "APK_DIR", str(tmp_path / "apkpkgs"))
    app = {"db": _dev_db("agentboss")}
    relay._apk_store(app, "d1", b"A" * 20480, {"code": 3, "ver": "0.1.1",
                                               "product": "evil;drop"})
    assert (tmp_path / "agentboss-apk.bin").read_bytes() == b"A" * 20480   # 清洗后回落设备线


# ---- 单条产品线全量发布：只放开这条线的 APK 基线，别碰别的线（2026-11-19）----

def test_release_product_unpins_only_that_line():
    c = {"default": {"ver": "0.9.37", "apk": 194},
         "default_by_product": {"agentboss": {"apk": 6}},
         "rules": [{"id": "r1"}]}
    relay._canary_release_product(c, "agentboss")
    assert "agentboss" not in (c.get("default_by_product") or {})
    assert c["default"] == {"ver": "0.9.37", "apk": 194}      # codeboss 基线不动
    assert c["rules"] == [{"id": "r1"}]                        # 灰度规则不动


def test_release_codeboss_zeroes_default_apk():
    c = {"default": {"ver": "0.9.37", "apk": 194},
         "default_by_product": {"agentboss": {"apk": 6}}}
    relay._canary_release_product(c, "codeboss")
    assert c["default"]["apk"] == 0                            # 跟最新
    assert c["default"]["ver"] == "0.9.37"
    assert c["default_by_product"]["agentboss"] == {"apk": 6}  # 别的线不动


def test_release_product_rejects_bad_name():
    c = {"default": {"ver": "0.9.37", "apk": 194}}
    relay._canary_release_product(c, "evil;drop")
    assert c == {"default": {"ver": "0.9.37", "apk": 194}}
