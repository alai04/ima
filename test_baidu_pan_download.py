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

    print("\n全部通过 ✅")


if __name__ == "__main__":
    main()
