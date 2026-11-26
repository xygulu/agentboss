# SPDX-License-Identifier: AGPL-3.0-or-later

"""中继边缘 gzip 自检：假 host 经隧道推 SSE，浏览器侧必须增量收到压缩流。

跑法：.venv/bin/python test_relay_gzip.py
只动 /tmp 下的临时 relay 库，不碰生产/本地真服务。
"""
import asyncio, base64, json, os, pathlib, subprocess, sys, tempfile, time
import aiohttp

RELAY = str(pathlib.Path(__file__).resolve().parent.parent / "relay" / "relay.py")
PORT = 18994
DB = "/tmp/cbt_gzcheck.db"
BASE = "http://127.0.0.1:%d" % PORT


async def fake_host(dev, sec):
    """冒充开发机：连上 host-link，按隧道协议回 5 帧 SSE。"""
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("%s/link?dev=%s&sec=%s" % (BASE, dev, sec)) as ws:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                d = json.loads(msg.data)
                if d.get("t") != "req":
                    continue
                rid = d["id"]
                await ws.send_json({"t": "res", "id": rid, "st": 1, "code": 200,
                                    "ct": "text/event-stream", "headers": {}, "body": ""})
                for i in range(5):
                    ch = ("data: %s\n\n" % ("x" * 400)).encode()
                    await ws.send_json({"t": "chunk", "id": rid, "n": i,
                                        "b64": base64.b64encode(ch).decode()})
                    await asyncio.sleep(0.3)
                await ws.send_json({"t": "res_end", "id": rid})


async def main():
    if os.path.exists(DB):
        os.unlink(DB)
    srv = subprocess.Popen([sys.executable, RELAY, "--port", str(PORT), "--db", DB],
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(BASE + "/r/") as r:
                        if r.status == 200:
                            break
            except Exception:
                pass
            await asyncio.sleep(0.2)

        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            e = await (await s.post(BASE + "/r/enroll", json={"name": "gz"})).json()
            dev, sec = e["device_id"], e["device_secret"]
            subprocess.run([sys.executable, RELAY, "--db", DB, "--admin", "adduser",
                            "gztest", "gztest123"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            async with s.post(BASE + "/r/login",
                              json={"ident": "gztest", "pass": "gztest123"}) as r:
                assert r.status == 200 and (await r.json()).get("ok"), "登录失败"
            async with s.post(BASE + "/r/pair", json={"code": e["pair_code"]}) as r:
                assert (await r.json()).get("ok"), "配对失败: " + await r.text()
            s.cookie_jar.update_cookies({"cb_dev": dev})

            host_task = asyncio.create_task(fake_host(dev, sec))
            await asyncio.sleep(0.8)

            t0, chunks = time.time(), []
            async with s.get(BASE + "/api/ext_push/x",
                             headers={"Accept-Encoding": "gzip"},
                             auto_decompress=False) as r:
                assert r.headers.get("Content-Encoding") == "gzip", \
                    "边缘没压：%s" % r.headers.get("Content-Encoding")
                # 自定义 gzip 解压流：验证增量
                import zlib
                dz = zlib.decompressobj(16 + zlib.MAX_WBITS)
                async for c in r.content.iter_any():
                    chunks.append((time.time() - t0, len(dz.decompress(c))))
            host_task.cancel()
            assert len(chunks) >= 3, "只收到 %d 块 → 压缩攒批了，实时性坏了: %r" % (len(chunks), chunks)
            assert sum(n for _, n in chunks) >= 5 * 405, "解压后内容不足: %r" % (chunks,)
            print("✓ 增量压缩流：%d 块，首块 %.2fs，总解压 %d 字节"
                  % (len(chunks), chunks[0][0], sum(n for _, n in chunks)))

            # 不压的对照：老 WebView 路径（XHR 流式）必须原样可读
            host_task = asyncio.create_task(fake_host(dev, sec))
            await asyncio.sleep(0.8)
            async with s.get(BASE + "/api/ext_push/x",
                             headers={"Accept-Encoding": "identity"}) as r:
                assert r.headers.get("Content-Encoding") in (None, "identity"), "identity 被压了"
                body = await r.read()
                assert b"data: " in body, "对照路径内容坏了"
            host_task.cancel()
            print("✓ identity 对照路径正常（%d 字节明文）" % len(body))
    finally:
        srv.terminate()
        srv.wait(timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
