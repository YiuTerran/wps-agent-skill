#!/usr/bin/env python3
"""把本地文件写回 WPS 365 —— request_upload 三步路线（**字节透传，格式无关**）。

这是编辑 .docx / .xlsx / .pptx 的**首选且唯一推荐**方式：

    file_fetch.py 下载原件 → 本地用 Office 技能改 → 本脚本原地传回

因为 `.docx` / `.xlsx` 都没有块级正文 API，而 sheets 接口连字面值都写不了
（只能写公式）。本地编辑再整文件回传既没有这些限制，又能保留公式、格式、
合并、多工作表。

  1) POST /v7/drives/{drive_id}/files/{parent_id}/request_upload
         {"file_id"?, "name", "size", "hashes":[md5, sha256]}
       → data.upload_id + data.store_request.{method,url}
  2) PUT 实体字节到 store_request.url（必须带 delegated token）
  3) POST /v7/drives/{drive_id}/files/{parent_id}/commit_upload  {"upload_id":...}

**带 `file_id` = 原地替换**：同一个 file_id、分享链接不变、版本历史保留（可 revert）。
**不带 `file_id` = 在 `parent_id` 下新建文件**（`0` 为盘根目录）。

`.docx` 没有块级正文 API（`/v7/documents/{file_id}/` 整组只有附件/评论/导出/点赞，
`/files/{file_id}/update` 只能改 `ext_attr`，`/files/{file_id}/content` 只有 GET），
所以语义上是整文件替换 —— 要逐段原地编辑请用 .otl（见 otl_write.py）。

最容易踩的坑（实测 v0.3.6）：
  * `hashes` **必填**，且只能用 **md5 / sha256**；**sha1 会被拒**，哪怕
    `enum_hash_type` 里列着它。报错是 `400000004 请求参数不支持`。
  * 别把下载接口给的哈希喂进来：`drive file download --with-hash` 只给 **sha1**。
  * 这个 400 **与权限无关**，不要误判成"没权限 / 接口不可用"。
  * `upload_scene` 可选，不是必需。
  * 上传与当前版本**完全相同的字节不产生新版本**（按内容哈希去重）。
  * `--dry-run` 只做第 1 步，**不产生任何文件**，可当无副作用的连通性测试。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import time

import _wps

_wps.use_utf8_stdio()


def api(method: str, path: str, timeout: int, data: dict | None = None) -> dict:
    return _wps.client.api(method, path, timeout=timeout, body=data, operation="上传 Office 文件")


# ---------------------------------------------------------------- 并发冲突检查

def current_state(drive_id: str, file_id: str, timeout: int) -> dict:
    """取云端当前状态，供更新前比对。

    `drive file get` 的元数据里直接带 `hash`（sha1），**不用下载**就能拿到当前
    内容哈希；另有 version / mtime / modified_by 可用来描述"被谁改的"。
    """
    data = _wps.file_meta(drive_id, file_id, timeout)
    return {"version": data.get("version"),
            "hash": (data.get("hash") or {}).get("sum"),
            "parent_id": data.get("parent_id"),
            "name": data.get("name"),
            "size": data.get("size"),
            "mtime": data.get("mtime"),
            "modified_by": (data.get("modified_by") or {}).get("name")}


def check_conflict(drive_id: str, file_id: str, timeout: int,
                   expect_hash: str | None = None, expect_version: int | None = None,
                   force: bool = False) -> dict:
    """更新前比对：若云端已被他人改动则放弃更新并提示冲突。

    不传任何期望值时不检查（调用方没提供基线，无从判断）。
    """
    if not expect_hash and expect_version is None:
        return {}
    cur = current_state(drive_id, file_id, timeout)
    problems = []
    if expect_version is not None and cur["version"] != expect_version:
        problems.append(f"版本 {expect_version} → {cur['version']}")
    if expect_hash and (cur["hash"] or "").lower() != expect_hash.lower():
        actual = (cur["hash"] or "缺失")[:12]
        problems.append(f"哈希 {expect_hash[:12]}… → {actual}")
    if problems and not force:
        who = cur.get("modified_by") or "未知用户"
        sys.exit("[file_write] ⚠️  冲突：云端文件已被改动，已放弃更新。\n"
                 f"   文件：{file_id}\n"
                 f"   差异：{'；'.join(problems)}\n"
                 f"   最后修改：{who}\n"
                 "   处理：先 file_fetch.py 重新下载、把你的改动合并进去再上传；"
                 "确认要覆盖请加 --force。")
    if problems:
        print(f"⚠️  已忽略冲突（--force）：{'；'.join(problems)}")
    return cur


# ---------------------------------------------------------------- Markdown → docx

def _style_run(run_obj, ascii_font: str = "Arial", ea_font: str = "微软雅黑"):
    """同时设置西文与东亚字体 —— 只设 font.name 时中文可能回落到不可控字体。"""
    from docx.oxml.ns import qn
    run_obj.font.name = ascii_font
    rpr = run_obj._element.get_or_add_rPr()
    rpr.get_or_add_rFonts().set(qn("w:eastAsia"), ea_font)


def _inline(paragraph, text: str) -> None:
    """还原 `**加粗**` 与 `` `代码` ``，其余当普通文本。"""
    import re
    for piece in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            r = paragraph.add_run(piece[2:-2]); r.bold = True
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            r = paragraph.add_run(piece[1:-1]); r.font.name = "Menlo"
        else:
            r = paragraph.add_run(piece)
        _style_run(r)


def _bullet(doc, text: str):
    """优先用列表样式；精简文档没有该样式时降级为手写项目符号。"""
    try:
        doc.styles["List Bullet"]
        para = doc.add_paragraph(style="List Bullet")
        _inline(para, text)
    except KeyError:
        para = doc.add_paragraph()
        _inline(para, "• " + text)
    return para


def build_docx_from_md(md: str, dest: str) -> None:
    """把 Markdown 子集转成 docx。

    支持：`#`~`######` 标题、`-`/`*` 无序列表、`1.` 有序列表、`>` 引用、
    `|` 表格、`**加粗**`、`` `代码` ``、`---` 分隔线、空行分段。
    不支持：图片、嵌套列表、脚注 —— 复杂排版请直接给 --docx。
    """
    try:
        from docx import Document
    except ImportError:
        sys.exit("[file_write] 生成 docx 需要 python-docx；请用 --docx 直接给文件，"
                 "或改用它自带的 Python 运行本脚本")

    import re
    doc = Document()
    if doc.styles["Normal"].font.name is None:
        pass  # 沿用模板默认

    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 表格：当前行以 | 开头且下一行是分隔行
        if stripped.startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            header = [c.strip() for c in stripped.strip("|").split("|")]
            rows: list[list[str]] = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            table = doc.add_table(rows=1, cols=len(header))
            try:
                table.style = "Table Grid"
            except KeyError:
                pass
            for idx, cell_text in enumerate(header):
                cell = table.cell(0, idx); cell.text = ""
                _inline(cell.paragraphs[0], cell_text)
                for r in cell.paragraphs[0].runs:
                    r.bold = True
            for row in rows:
                cells = table.add_row().cells
                for idx, cell_text in enumerate(row[:len(header)]):
                    cells[idx].text = ""
                    _inline(cells[idx].paragraphs[0], cell_text)
            doc.add_paragraph()
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = min(len(m.group(1)), 4)
            heading = doc.add_heading("", level=level)
            _inline(heading, m.group(2))
            i += 1
            continue

        if re.match(r"^[-*+]\s+", stripped):
            _bullet(doc, re.sub(r"^[-*+]\s+", "", stripped))
            i += 1
            continue

        m = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if m:
            para = doc.add_paragraph()
            _inline(para, f"{m.group(1)}. {m.group(2)}")
            i += 1
            continue

        if stripped.startswith(">"):
            para = doc.add_paragraph()
            _inline(para, stripped.lstrip("> ").strip())
            para.paragraph_format.left_indent = 254000  # 0.25 英寸
            i += 1
            continue

        if re.match(r"^-{3,}$|^\*{3,}$", stripped):
            i += 1
            continue

        para = doc.add_paragraph()
        _inline(para, stripped)
        i += 1

    doc.save(dest)


# ---------------------------------------------------------------- 上传

def backup_current(drive_id: str, file_id: str, backup_dir: str, timeout: int,
                   name: str = "") -> str:
    os.makedirs(backup_dir, exist_ok=True)
    ext = os.path.splitext(name or "")[1] or ".bin"
    dest = os.path.join(backup_dir, f"backup-{_wps.segment(file_id)}{ext}")
    _wps.download_file(drive_id, file_id, dest, timeout)
    return dest


def verify_upload(drive_id, file_id, previous_version, expected_sha1, timeout):
    for delay in (0, 1, 2):
        if delay:
            time.sleep(delay)
        state = current_state(drive_id, file_id, timeout)
        if previous_version is not None:
            version = state["version"]
            if type(version) is not int or version <= previous_version:
                continue
        with tempfile.TemporaryDirectory(prefix="wps-agent-verify-") as directory:
            _, actual, _ = _wps.download_file(drive_id, file_id, os.path.join(directory, "readback"), timeout)
        if actual == expected_sha1:
            return state["version"]
    raise _wps.WpsError("上传校验", "云端版本或下载内容与预期不一致，请回查文件")


def check_name_available(route, name, timeout):
    query = {"page_size": 100}
    seen = set()
    while True:
        data = _wps.client.api("GET", f"{route}/children", timeout=timeout, query=query, operation="核对上传目录")
        if not isinstance(data.get("items"), list):
            raise _wps.WpsError("核对上传目录", "响应缺少文件列表")
        if any((item.get("name") or item.get("fname") or "").casefold() == name.casefold() for item in data["items"]):
            raise ValueError("目标目录存在同名文件。如需覆盖，请指定该文件 ID 和基线")
        page = data.get("next_page_token")
        if not page:
            return
        if page in seen:
            raise _wps.WpsError("核对上传目录", "分页标记重复，已停止上传")
        seen.add(page)
        query["page_token"] = page


def upload(drive_id: str, parent_id: str, file_id: str | None, name: str,
           path: str, on_name_conflict: str | None, timeout: int,
           dry_run: bool, baseline: dict | None = None) -> None:
    with open(path, "rb") as fh:
        blob = fh.read()
    size = len(blob)
    md5 = hashlib.md5(blob).hexdigest()
    sha256 = hashlib.sha256(blob).hexdigest()

    body: dict = {
        "name": name,
        "size": size,
        # 只能用 md5/sha256 —— sha1 会被 400 拒掉
        "hashes": [{"type": "md5", "sum": md5}, {"type": "sha256", "sum": sha256}],
    }
    if file_id:
        body["file_id"] = file_id
    if not file_id:
        # WPS rejects an explicit `fail` on this upload endpoint. Guard the name
        # locally and request rename on the server to prevent a racing overwrite.
        body["on_name_conflict"] = "rename"

    target = f"原地替换 {file_id}" if file_id else f"新建到 parent={parent_id}"
    print(f"上传：{name}  {size} 字节  ({target})")

    if dry_run:
        print("预演完成，未申请或提交上传")
        return

    route = f"/v7/drives/{_wps.segment(drive_id)}/files/{_wps.segment(parent_id)}"
    if not file_id and on_name_conflict != "rename":
        check_name_available(route, name, timeout)
    data = api("post", f"{route}/request_upload", timeout, body)
    upload_id = data.get("upload_id")
    store = data.get("store_request") or {}
    if not upload_id or not store.get("url"):
        sys.exit("[file_write] 申请上传的响应缺少上传 ID 或地址")
    method = (store.get("method") or "PUT").upper()
    if method != "PUT" or store.get("body_params"):
        sys.exit("[file_write] WPS 未返回可用的原件 PUT 上传方式")

    with _wps.client.drive_transfer(store["url"], method, timeout=timeout, data=blob,
                                    headers=store.get("headers"), operation="上传原件字节") as resp:
        if not 200 <= resp.status < 300:
            sys.exit(f"[file_write] PUT 失败：HTTP {resp.status}")

    if file_id:
        state = check_conflict(drive_id, file_id, timeout, baseline["hash"], baseline["version"])
        if str(state["parent_id"]) != str(parent_id):
            raise ValueError("原文件所在目录在上传期间变化，已停止提交")
    try:
        committed = api("post", f"{route}/commit_upload", timeout, {"upload_id": upload_id})
    except _wps.WpsError as exc:
        if not file_id or not exc.uncertain:
            raise
        # A lost commit response may follow a successful replacement. Never resubmit it.
        verify_upload(drive_id, file_id, baseline["version"], hashlib.sha1(blob).hexdigest(), timeout)
        print(f"提交响应异常，已读回并核对更新后的文件 {file_id}")
        return
    saved_id = committed.get("id") or committed.get("file_id")
    if not saved_id or (file_id and str(saved_id) != str(file_id)):
        sys.exit("[file_write] 提交响应中缺少预期文件 ID，请回查目录后再操作")
    version = verify_upload(drive_id, saved_id, baseline["version"] if file_id else None,
                            hashlib.sha1(blob).hexdigest(), timeout)
    print(f"已从云端下载并核对文件 {saved_id}，版本 {version}")
    for key, label in (("name", "name"), ("size", "size"), ("link_url", "link_url")):
        if committed.get(key) is not None:
            print(f"   {label:<9} = {committed.get(key)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="把本地文件写回 WPS 365（原地替换或新建）")
    ap.add_argument("--drive-id", required=True, help="目标盘 drive_id")
    ap.add_argument("--file-id", help="要原地替换的 file_id；省略则新建文件")
    ap.add_argument("--parent-id", help="新建时的父目录 id（默认盘根目录 0）")
    ap.add_argument("--file", "--docx", dest="src_file",
                    help="本地文件路径（字节透传，不限 .docx；与 --md 二选一）")
    ap.add_argument("--md", help="Markdown 文件路径，脚本会先生成 docx 再上传")
    ap.add_argument("--name", help="云端文件名（新建时默认取本地文件名，替换时默认沿用云端名称）")
    ap.add_argument("--on-name-conflict", choices=["fail", "rename"],
                    help="新建文件时的重名策略（默认 fail）")
    ap.add_argument("--backup-dir", help="替换前把当前版本下载到该目录")
    ap.add_argument("--expect-hash", help="基线哈希：云端当前哈希与之不符则放弃更新（冲突）")
    ap.add_argument("--expect-version", type=int,
                    help="基线版本号：云端当前版本与之不符则放弃更新（冲突）")
    ap.add_argument("--force", action="store_true", help="忽略冲突检查，强制覆盖")
    ap.add_argument("--print-state", action="store_true",
                    help="只打印云端当前 version/hash 后退出（用于取基线）")
    ap.add_argument("--timeout", type=int, default=180, help="单次 HTTP 超时秒数")

    ap.add_argument("--dry-run", action="store_true", help="核对本地文件和云端基线，不上传")
    args = ap.parse_args()

    if not args.print_state and bool(args.src_file) == bool(args.md):
        ap.error("--file 或 --md 二选一")

    if args.print_state:
        if not args.file_id:
            ap.error("--print-state 需要 --file-id")
        state = current_state(args.drive_id, args.file_id, args.timeout)
        print(f"version={state['version']}  hash={state['hash']}"
              f"  size={state['size']}  最后修改={state.get('modified_by')}")
        return

    # 准备要上传的本地文件
    if args.src_file:
        src, name = args.src_file, (args.name or os.path.basename(args.src_file))
        if not os.path.exists(src):
            sys.exit(f"[file_write] 找不到 {src}")
    else:
        if not os.path.exists(args.md):
            sys.exit(f"[file_write] 找不到 {args.md}")
        src = os.path.splitext(args.md)[0] + ".docx"
        with open(args.md, encoding="utf-8") as fh:
            md = fh.read()
        build_docx_from_md(md, src)
        name = args.name or os.path.basename(src)
        print(f"已由 Markdown 生成 {src}（{os.path.getsize(src)} 字节）")

    baseline = None
    parent_id = args.parent_id or "0"
    if args.file_id:
        if not args.expect_hash or args.expect_version is None:
            ap.error("替换文件需要 --expect-hash 和 --expect-version")
        baseline = check_conflict(args.drive_id, args.file_id, args.timeout,
                                  args.expect_hash, args.expect_version, args.force)
        if not baseline["hash"] or type(baseline["version"]) is not int or baseline["parent_id"] is None:
            sys.exit("[file_write] 云端元信息缺少文件哈希、版本或父目录，已停止上传")
        parent_id = str(baseline["parent_id"])
        name = args.name or baseline["name"] or name
        if args.parent_id is not None and args.parent_id != parent_id:
            ap.error("--parent-id 与原文件的实际父目录不一致")
        if not args.dry_run:
            backup_dir = args.backup_dir or tempfile.mkdtemp(prefix="wps-agent-office-backup-")
            dest = backup_current(args.drive_id, args.file_id, backup_dir, args.timeout, baseline["name"] or name)
            print(f"已备份当前版本到 {dest}")

    upload(args.drive_id, parent_id, args.file_id, name, src,
           args.on_name_conflict, args.timeout, args.dry_run, baseline)


if __name__ == "__main__":
    _wps.run(main)
