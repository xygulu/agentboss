# SPDX-License-Identifier: AGPL-3.0-or-later

"""台账验收交互：不许用原生 confirm()/prompt()（WebView 里弹带网址的系统框/静默失败）。

2026-11-19 用户实报：点「打回」弹出一个带 url 的对话框，页面像卡死。
agentboss 线（05_cmd.js）的交互一律走页面内浮层 #dlg（askConfirm）。

跑法： .venv/bin/python -m pytest tests/test_ledger_dialogs.py -q
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def test_no_native_dialogs_in_agentboss_cmds():
    src = (HERE / "host" / "static" / "src" / "js" / "05_cmd.js").read_text(encoding="utf-8")
    for i, line in enumerate(src.splitlines(), 1):
        t = line.strip()
        if t.startswith("/*") or t.startswith("*") or t.startswith("//"):
            continue                      # 注释里提到 confirm() 不算
        code = t.split("/*")[0]
        m = re.search(r'(?<![.\w])(confirm|prompt|alert)\s*\(', code)
        assert not m, "05_cmd.js:%d 用了原生对话框：%s" % (i, line)


def test_ledger_reject_collects_note_inpage():
    src = (HERE / "host" / "static" / "src" / "js" / "05_cmd.js").read_text(encoding="utf-8")
    assert "function ledgerTaskReject(" in src and "askConfirm(" in src
    assert 'id="dlgNote"' in src, "打回意见要在页面内输入，不能靠 prompt()"
