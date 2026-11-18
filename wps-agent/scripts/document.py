"""Resolve a WPS document or extract its searchable text."""

import argparse
import json
import sys
from pathlib import Path

import _wps


def identify_kind(meta, link):
    suffix = Path(meta.get("name") or "").suffix.lower()
    declared = str(meta.get("type") or link.get("type") or "").lower()
    if suffix == ".dbt" or declared in {"db", "dbsheet"}:
        return "dbsheet"
    if suffix == ".ksheet" or declared in {"airsheet", "ksheet"}:
        return "airsheet"
    if suffix == ".otl" or declared in {"airpage", "otl"}:
        return "airpage"
    return "office" if suffix in {".docx", ".xlsx", ".pptx"} else declared or "unknown"


def locate(link, file_id, drive_id, timeout):
    shared = _wps.resolve_link(link, timeout) if link else {}
    if shared:
        file_id, drive_id = shared["file_id"], shared["drive_id"]
    if not drive_id:
        meta = _wps.meta_by_id(file_id, timeout)
        drive_id = _wps.drive_id_from_meta(meta)
    else:
        meta = _wps.file_meta(drive_id, file_id, timeout)
    return {"drive_id": str(drive_id), "file_id": str(file_id),
            "name": meta.get("name"), "type": meta.get("type") or shared.get("type"),
            "kind": identify_kind(meta, shared), "version": meta.get("version"),
            "parent_id": meta.get("parent_id"), "link_url": meta.get("link_url")}


def main():
    parser = argparse.ArgumentParser(description="解析或读取 WPS 云文档")
    parser.add_argument("action", choices=("inspect", "read"))
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--link", help="WPS 分享链接或 link_id")
    target.add_argument("--file-id", help="文件 ID")
    parser.add_argument("--drive-id", help="指定文件 ID 时可同时指定驱动盘 ID")
    parser.add_argument("--format", choices=("markdown", "plain"), default="markdown")
    parser.add_argument("--out", type=Path, help="把正文写入该文件，省略时输出到标准输出")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if args.link and args.drive_id:
        parser.error("--drive-id 仅与 --file-id 一起使用")

    target = locate(args.link, args.file_id, args.drive_id, args.timeout)
    if args.action == "inspect":
        print(json.dumps(target, ensure_ascii=False, indent=2))
        return
    if target["kind"] == "dbsheet":
        parser.error("多维表格请先用 dbsheet.py schema 读取结构，再读取记录")
    data = _wps.body_content(target["drive_id"], target["file_id"], args.format, args.timeout)
    content = data.get(args.format)
    if not isinstance(content, str):
        raise _wps.WpsError("正文抽取", f"响应缺少 {args.format} 内容")
    if args.out:
        args.out.write_text(content, encoding="utf-8")
        print(f"正文已保存到 {args.out}（{len(content)} 字符）")
    else:
        print(content)
    if data.get("is_partly_exported"):
        print("WPS 标记正文为部分导出", file=sys.stderr)


if __name__ == "__main__":
    _wps.use_utf8_stdio()
    _wps.run(main)
