"""冒烟测试：baidu_pan_download 的纯函数（不联网、不需要 Cookie）。

运行: python test_baidu_pan_download.py
"""

from __future__ import annotations

import baidu_pan_download as b


def main() -> None:
    # 1. surl 解析（含 ?pwd= 与 share/init 两种链接形态、前导 1 的处理）
    print("=== extract_surl ===")
    cases = {
        "https://pan.baidu.com/s/1lpUp14K-1CXccXny5RlYmg": "lpUp14K-1CXccXny5RlYmg",
        "https://pan.baidu.com/s/1nvBwS25lENYceUu3OMH4tg?pwd=6img": "nvBwS25lENYceUu3OMH4tg",
        "https://pan.baidu.com/share/init?surl=7M-O0-SskRPdoZ0emZrd5w": "7M-O0-SskRPdoZ0emZrd5w",
        "链接: https://pan.baidu.com/s/182A8FJ02gCq1MWYyrm_emA 提取码: fm9k": "82A8FJ02gCq1MWYyrm_emA",
    }
    for url, want in cases.items():
        got = b.extract_surl(url)
        assert got == want, f"{url} -> {got} != {want}"
        print(f"  {got} -> {b.share_page_url(got)}")

    # 2. Cookie 解析与归一化（兼容多种粘贴形态）
    print("=== normalize_cookie / parse_cookie_string ===")
    cookie_cases = {
        "abc-DEF": "BDUSS=abc-DEF",  # 裸 BDUSS 值
        "BDUSS=x;STOKEN=y": "BDUSS=x; STOKEN=y",
        "'BDUSS=x; STOKEN=y'": "BDUSS=x; STOKEN=y",
        'Cookie: BDUSS=aaa; STOKEN=bbb': "BDUSS=aaa; STOKEN=bbb",  # 带请求头前缀
        "cookie:BDUSS=aaa;STOKEN=bbb": "BDUSS=aaa; STOKEN=bbb",
        '"BDUSS=aaa; STOKEN=bbb"': "BDUSS=aaa; STOKEN=bbb",
        "BDUSS\t=\taaa\nSTOKEN\t=\tbbb": "BDUSS=aaa; STOKEN=bbb",  # DevTools 表格粘贴
        'BDUSS=aaa; \\\nSTOKEN=bbb': "BDUSS=aaa; STOKEN=bbb",  # 行尾续行符
        '[{"name":"BDUSS","value":"v1"},{"name":"STOKEN","value":"v2"}]': "BDUSS=v1; STOKEN=v2",
        '{"BDUSS":"v1","STOKEN":"v2"}': "BDUSS=v1; STOKEN=v2",
    }
    for raw, want in cookie_cases.items():
        got = b.normalize_cookie(raw)
        assert got == want, f"{raw!r} -> {got!r} != {want!r}"

    # BDUSS_BFESS 回填：新版百度只下发 BFESS 字段时也要能登录
    bfess = b.normalize_cookie("BDUSS_BFESS=bf1; STOKEN_BFESS=st1; BAIDUID=q")
    assert "BDUSS=bf1" in bfess and "STOKEN=st1" in bfess, bfess

    # 整段请求头多行粘贴：只保留 Cookie 行
    headers = "Host: pan.baidu.com\nCookie: BDUSS=aaa; STOKEN=bbb\nAccept: */*"
    assert b.normalize_cookie(headers) == "BDUSS=aaa; STOKEN=bbb"
    assert b.parse_cookie_string("BDUSS=a=b=c; X=y") == {"BDUSS": "a=b=c", "X": "y"}
    assert b.cookie_value("BDUSS=x; STOKEN=y", "STOKEN") == "y"
    assert b.cookie_value("BDUSS=x; STOKEN=y", "BDCLND") == ""
    assert b.cookie_field_names("BDUSS=a; STOKEN=b") == ["BDUSS", "STOKEN"]
    assert b.normalize_cookie("") == "" and b.normalize_cookie(";;;") == ""
    print("  ok")

    # 3. 分享页配置解析：标准 JSON（locals.mset）与 JS 对象字面量（yunData）
    print("=== extract_page_json ===")
    mset_html = (
        '<script>locals.mset({"csrf":"x","uk":0,"loginstate":1,"bdstoken":"tok",'
        '"file_list":{"list":[{"fs_id":123456,"isdir":0,'
        '"server_filename":"\\u7814\\u62a5.pdf","size":4096,"path":"/研报/研报.pdf"}],"total":1}});</script>'
    )
    data = b.extract_page_json(mset_html)
    assert data and data["bdstoken"] == "tok"
    assert data["file_list"]["list"][0]["server_filename"] == "研报.pdf"
    print("  locals.mset(JSON) ok")

    js_html = "<script>window.yunData={skinName:'white', neglect:1, share_uk:\"1103665489460\", shareid:\"8621787266\"};</script>"
    data2 = b.extract_page_json(js_html)
    assert data2 and data2["shareid"] == "8621787266", data2
    print("  window.yunData(JS 字面量) ok")

    # 4. 整页兜底抓取条目
    print("=== extract_entries_fallback ===")
    quoted = (
        '{"fs_id":11,"isdir":0,"server_filename":"a\\u7814b.pdf","size":12,"path":"/x/a.pdf"},'
        '{"fs_id":22,"isdir":1,"server_filename":"folder","size":0,"path":"/x/folder"}'
    )
    ents = b.extract_entries_fallback(quoted)
    assert [e.fs_id for e in ents] == ["11", "22"]
    assert ents[0].name == "a研b.pdf" and ents[0].size == 12 and not ents[0].isdir
    assert ents[1].isdir and ents[1].path == "/x/folder"

    js = "var file_list={list:[{fs_id:33,isdir:0,server_filename:'报告 A.pdf',size:999,path:'/r/报告 A.pdf'}],total:1};"
    ents2 = b.extract_entries_fallback(js)
    assert len(ents2) == 1 and ents2[0].fs_id == "33" and ents2[0].size == 999
    print("  ok")

    # 5. 已登录后的分享页：file_list 直接是数组，需能正确解析成素材
    print("=== parse_share_page（file_list 为数组）===")

    class _Resp:
        def __init__(self, text):
            self.text, self.status_code, self.cookies = text, 200, {}

    spa_page = (
        '<script>locals.mset({"bdstoken":"","shareid":"8621787266","share_uk":"1103665489460",'
        '"file_list":[{"fs_id":28692584753369,"isdir":1,"server_filename":"261004",'
        '"size":0,"path":"/sharelink1103665489460-28692584753369/261004"}]});</script>'
    )
    pan = b.BaiduPan("BDUSS=x; STOKEN=y")
    pan._request = lambda *a, **k: _Resp(spa_page)  # type: ignore[method-assign]
    meta = pan.parse_share_page("lpUp14K-1CXccXny5RlYmg")
    assert meta.shareid == "8621787266" and meta.uk == "1103665489460"
    assert len(meta.entries) == 1 and meta.entries[0].isdir and meta.entries[0].name == "261004"
    assert meta.entries[0].fs_id == "28692584753369"
    print("  ok:", meta.shareid, meta.entries[0].name)

    # 6. 落盘路径安全化
    print("=== _safe_rel_path ===")
    assert b._safe_rel_path("../../etc/pa:ss?.pdf") == "etc/pa_ss_.pdf"
    assert b._safe_rel_path("/研报/2026/foo.pdf") == "研报/2026/foo.pdf"
    assert b._safe_rel_path("..") == "unnamed"
    print("  ok")

    # 7. sign 算法可执行且输出在预期字符集内
    print("=== _sign2 / _sign_base64 ===")
    sig = b._sign_base64("".join(b._sign2("abcdef", "123456")))
    assert sig and set(sig) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    print(f"  sign={sig}")

    # 8. 其它小工具
    assert b.human_size(0) == "0B" and b.human_size(1536) == "1.5KB"
    assert b._first_nonzero(0, "", "5") == "5"
    assert b._first_nonzero("0", None, "") == ""
    print("=== human_size / _first_nonzero ok ===")

    # 9. session_hint 会把缺失的会话字段指出来（排查 errno=-6）
    hint = b.BaiduPan("BDUSS=x").session_hint()
    assert "STOKEN" in hint and "errno=-6" in hint
    print("=== session_hint ok ===")

    # 10. 下载后处理：入库 → 分类 → 上传 → 发邮件（含失败隔离与幂等）
    print("=== PostProcessor ===")
    _test_post_processor()

    # 11. _load_known_locations 同样只认 7 天内的记录
    print("=== _load_known_locations（7 天过滤）===")
    _test_known_locations()

    print("\n全部通过 ✅")


# ══════════════════════════════════════════════════════════════════════
# 下载后处理（用假模块替掉 check_reports / classifier，不联网、不碰真实 DB）
# ══════════════════════════════════════════════════════════════════════

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS reports (media_id TEXT PRIMARY KEY, title TEXT NOT NULL,"
    " downloaded_ts INTEGER DEFAULT 0, sendmail_ts INTEGER DEFAULT 0, created_ts INTEGER DEFAULT 0,"
    " path TEXT DEFAULT '', level1 TEXT DEFAULT '', level2 TEXT DEFAULT '', level3 TEXT DEFAULT '',"
    " author TEXT DEFAULT '', report_date TEXT DEFAULT '', priority TEXT DEFAULT 'Medium',"
    " sharepoint_ts INTEGER DEFAULT 0)"
)


def _test_known_locations() -> None:
    """_load_known_locations 与后处理保持一致：只认 RECENT_DAYS 天内的记录。"""
    import sqlite3
    import tempfile
    import time
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    cat = tmp / "categorized_reports" / "Macro"
    cat.mkdir(parents=True)
    now = int(time.time())
    rows = [
        ("bdpan_old", "old.pdf", now - 8 * 86400),  # 旧记录
        ("bdpan_new", "new.pdf", now - 1 * 86400),  # 7 天内
    ]
    for _, name, _ in rows:
        (cat / name).write_bytes(b"%PDF fake")
    with sqlite3.connect(str(tmp / "reports.db")) as conn:
        conn.execute(
            "CREATE TABLE reports (media_id TEXT PRIMARY KEY, title TEXT, created_ts INT, path TEXT)"
        )
        for media_id, name, ts in rows:
            conn.execute(
                "INSERT INTO reports VALUES (?, ?, ?, ?)", (media_id, name, ts, str(cat))
            )
        conn.commit()

    saved = (b.PROJECT_DIR, b._known_paths_cache, b._known_paths_loaded)
    b.PROJECT_DIR = tmp
    b._known_paths_cache, b._known_paths_loaded = {}, False
    try:
        known = b._load_known_locations()
        assert set(known) == {"new.pdf"}, known  # 旧记录被过滤掉

        out_dir = tmp / "downloaded_reports"
        size = (cat / "old.pdf").stat().st_size
        assert b._existing_local(out_dir, "d/old.pdf", size) is None, "旧记录不应被识别为已存在"
        assert b._existing_local(out_dir, "d/new.pdf", size) == cat / "new.pdf"
        assert b._is_recent(0) is False
        print("  ok：旧记录被过滤，7 天内的仍能避免重复下载")

        # 路径过长（OSError ENAMETOOLONG）时应跳过该条记录，不抛异常、继续处理后面的记录
        (cat / "good2.pdf").write_bytes(b"%PDF fake")
        with sqlite3.connect(str(tmp / "reports.db")) as conn:
            conn.execute(
                "INSERT INTO reports VALUES (?, ?, ?, ?)",
                ("bdpan_long", "TOO_LONG-名称.pdf", now - 60, str(cat)),
            )
            conn.execute(
                "INSERT INTO reports VALUES (?, ?, ?, ?)",
                ("bdpan_good2", "good2.pdf", now - 120, str(cat)),
            )
            conn.commit()
        real_exists = Path.exists

        def fake_exists(self):
            if "TOO_LONG" in str(self):
                raise OSError(36, "File name too long")
            return real_exists(self)

        Path.exists = fake_exists  # type: ignore[method-assign]
        try:
            b._known_paths_cache, b._known_paths_loaded = {}, False
            known = b._load_known_locations()  # 不应抛异常
            assert set(known) == {"new.pdf", "good2.pdf"}, known  # 长的被跳过，其余照常
            assert b._size_matches(cat / "TOO_LONG-x.pdf", 1) is False
        finally:
            Path.exists = real_exists  # type: ignore[method-assign]
        print("  ok：ENAMETOOLONG 只跳过该条记录，后续记录照常处理")
    finally:
        b.PROJECT_DIR, b._known_paths_cache, b._known_paths_loaded = saved


def _test_post_processor() -> None:
    import argparse
    import sqlite3
    import sys
    import tempfile
    import time
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    db_path, cat_root = tmp / "reports.db", tmp / "categorized_reports"

    class FakeCR:
        """替身：check_reports"""

        DB_PATH, CATEGORIZED_ROOT, PROJECT_DIR = db_path, cat_root, tmp
        SEND_OK, SEND_CLIENT_ERROR, SEND_FAILED = "ok", "client_error", "failed"
        mails: list[str] = []
        uploads: list[str] = []
        fail_mail: set[str] = set()
        fail_upload: set[str] = set()

        @staticmethod
        def init_db() -> None:
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(_SCHEMA)
                conn.commit()

        @staticmethod
        def insert_report(media_id: str, title: str) -> bool:
            FakeCR.init_db()
            with sqlite3.connect(str(db_path)) as conn:
                try:
                    conn.execute(
                        "INSERT INTO reports (media_id, title, created_ts) VALUES (?, ?, ?)",
                        (media_id, title, int(time.time())),
                    )
                    conn.commit()
                    return True
                except sqlite3.IntegrityError:
                    return False

        @staticmethod
        def mark_downloaded(media_id: str) -> None:
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("UPDATE reports SET downloaded_ts = 1 WHERE media_id = ?", (media_id,))
                conn.commit()

        @staticmethod
        def mark_sent(media_id: str) -> None:
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("UPDATE reports SET sendmail_ts = 1 WHERE media_id = ?", (media_id,))
                conn.commit()

        @staticmethod
        def _resolve_path(value: str) -> Path:
            path = Path(value)
            return path if path.is_absolute() else tmp / path

        @staticmethod
        def upload_report_to_sharepoint(media_id: str, title: str, filepath: Path) -> bool:
            if title in FakeCR.fail_upload:
                raise RuntimeError("sharepoint boom")
            FakeCR.uploads.append(title)
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("UPDATE reports SET sharepoint_ts = 1 WHERE media_id = ?", (media_id,))
                conn.commit()
            return True

        @staticmethod
        def send_email(title: str, filepath: Path) -> str:
            if title in FakeCR.fail_mail:
                return "failed"
            FakeCR.mails.append(title)
            return "ok"

    class FakeCL:
        """替身：classifier（把文件移到分类目录并写元数据）"""

        calls: list[str] = []

        @staticmethod
        def classify_one_report(db, root, media_id, title, src_dir, dry_run=False):
            FakeCL.calls.append(title)
            dest = Path(root) / "Equity Research" / "Autos"
            dest.mkdir(parents=True, exist_ok=True)
            src = Path(src_dir) / title
            if src.resolve() != (dest / title).resolve():
                src.rename(dest / title)
            with sqlite3.connect(str(db)) as conn:
                conn.execute(
                    "UPDATE reports SET level1 = 'Equity Research', level2 = 'Autos', path = ? "
                    "WHERE media_id = ?",
                    (str(dest), media_id),
                )
                conn.commit()
            return True, str(dest)

    saved = {name: sys.modules.get(name) for name in ("check_reports", "classifier")}
    sys.modules["check_reports"], sys.modules["classifier"] = FakeCR, FakeCL
    try:
        args = argparse.Namespace(classify=True, sharepoint=True, mail=True)
        names = ["a-261001.pdf", "b-261002.pdf", "c-261003.pdf"]
        files = []
        for name in names:
            path = tmp / "downloaded_reports" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.4 fake")
            files.append(path)

        # a: 发信失败；b: 上传失败；c: 全部成功
        FakeCR.fail_mail, FakeCR.fail_upload = {"a-261001.pdf"}, {"b-261002.pdf"}

        post = b.PostProcessor(args)
        assert post.available, "PostProcessor 应可用（已注入假模块）"
        post.start()
        for i, path in enumerate(files):
            post.submit(b.PendingFile(fs_id=str(100 + i), title=path.name, filepath=path))
        post.close()

        # 三份都入库并标记已下载；分类成功；文件已移到分类目录
        with sqlite3.connect(str(db_path)) as conn:
            rows = {
                row[0]: row
                for row in conn.execute(
                    "SELECT title, downloaded_ts, sendmail_ts, level1, path FROM reports"
                )
            }
        assert set(rows) == set(names), rows
        assert all(row[1] > 0 for row in rows.values()), rows
        assert all(row[3] == "Equity Research" for row in rows.values()), rows
        for path in files:
            assert not path.exists()
            assert (cat_root / "Equity Research" / "Autos" / path.name).exists()

        # 失败隔离：a 未标记已发送；b 上传失败但仍发了邮件；c 全部完成
        assert rows["a-261001.pdf"][2] == 0, rows
        assert rows["b-261002.pdf"][2] > 0, rows
        assert rows["c-261003.pdf"][2] > 0, rows
        assert FakeCR.mails == ["b-261002.pdf", "c-261003.pdf"], FakeCR.mails
        assert FakeCR.uploads == ["a-261001.pdf", "c-261003.pdf"], FakeCR.uploads
        assert post.stats["failed"] == 2 and post.stats["classify"] == 3, post.stats
        print("  失败隔离 ok：", post.summary())

        # 幂等 + 重试：邮件服务恢复后只重试未发送的 a，b/c 跳过
        FakeCR.fail_mail = set()
        post2 = b.PostProcessor(args)
        post2.start()
        for i, name in enumerate(names):
            post2.submit(
                b.PendingFile(
                    fs_id=str(100 + i),
                    title=name,
                    filepath=cat_root / "Equity Research" / "Autos" / name,
                )
            )
        post2.close()
        assert post2.stats["skipped"] == 2, post2.stats
        assert FakeCR.mails[-1] == "a-261001.pdf", FakeCR.mails
        # 已分类（level1 非空）的研报不再重复调 LLM：三次后仍只有最初的 3 次调用
        assert FakeCL.calls == names, FakeCL.calls
        assert post2.stats["classify"] == 0 and post2.stats["classify_skipped"] == 1, post2.stats
        print("  幂等 + 失败重试 + 已分类不重复分类 ok：", post2.summary())

        # 文件不存在：只计失败，不抛异常
        post3 = b.PostProcessor(args)
        post3.start()
        post3.submit(b.PendingFile(fs_id="9", title="gone.pdf", filepath=tmp / "gone.pdf"))
        post3.close()
        assert post3.stats["failed"] == 1 and post3.stats["db"] == 0, post3.stats
        print("  缺失文件容错 ok")

        # 规则一：created_ts 超过 7 天的旧记录一律不处理
        old_file = tmp / "downloaded_reports" / "old-261001.pdf"
        old_file.write_bytes(b"%PDF-1.4 old")
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO reports (media_id, title, created_ts, path) VALUES (?, ?, ?, ?)",
                ("bdpan_old", old_file.name, int(time.time()) - 8 * 86400, str(old_file.parent)),
            )
            conn.commit()
        before_calls = len(FakeCL.calls)
        post4 = b.PostProcessor(args)
        post4.start()
        post4.submit(b.PendingFile(fs_id="old", title=old_file.name, filepath=old_file))
        post4.close()
        assert post4.stats["skipped"] == 1, post4.stats
        assert post4.stats["db"] == 0 and post4.stats["mail"] == 0, post4.stats
        assert len(FakeCL.calls) == before_calls, "旧记录不应触发分类"
        assert old_file.exists(), "旧记录不应被移动"
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT downloaded_ts, sendmail_ts FROM reports WHERE media_id = 'bdpan_old'"
            ).fetchone()
        assert row == (0, 0), row
        print("  7 天外旧记录不处理 ok")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


if __name__ == "__main__":
    main()
