#!/usr/bin/env python3
"""把 Markdown 写进 WPS 365 智能文档（.otl），本地图片一并上传成文档附件。

路线：Markdown --blocks/convert--> 块 --（补图片 id / 拆超长表）--> blocks/create
  POST /v7/airpage/v2/{_wps.segment(file_id)}/blocks/convert
  POST /v7/coop/files/{_wps.segment(file_id)}/attachments/upload/{address,complete}   ← 本地图片
  POST /v7/airpage/v2/{_wps.segment(file_id)}/blocks/create

为什么走 convert 而不是自己拼块：convert 返回的块结构与 blocks/create 期望的
doc_child 是同一个 union，实测可直接透传；自己拼块要维护十几种节点类型。

为什么图片要单独处理：convert 认得 `![](x.png)` / `<img src="x.png">`，但产出的
picture 块 `id` 是空串、尺寸是 300x300 占位；直接把空 id 写进去会被服务端以
`invalid DocChildren.Children[i]` 拒掉（看不出真正原因）。所以这里先在本地把图片
传成附件拿 id，再按出现顺序回填。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import unicodedata
import _wps

_wps.use_utf8_stdio()
MAX_BLOCKS_PER_CALL = 1000  # 服务端限制：doc_children.children 单次 ≤1000
MAX_TABLE_ROWS = 100        # 服务端限制：table.rows maxItems=100，超了必须拆表
DOC_WIDTH = 740             # 正文默认宽度（px），86% ≈ 636
#: 表格单列下限（px）：服务端按单元格内容兜底，1~5 个汉字也只到这么宽。
TABLE_MIN_COL = 116
#: 内容超过 BASE 个字后，每多一个字列宽下限增加约这么多（实测 9 字 180、13 字 244）。
TABLE_COL_PER_CHAR = 16
TABLE_COL_BASE_CHARS = 5
#: 允许的溢出比例：超出不到这个比例的表不动，切行会白白增加行高。
TABLE_OVERFLOW_TOLERANCE = 0.05
DEFAULT_IMG_PCT = 86
#: `<img src="...">` 与 `![alt](...)`，按出现顺序 —— 与 convert 出的 picture 块一一对应。
IMAGE_RE = re.compile(r'<img[^>]*?\bsrc="([^"]+)"|!\[[^\]]*\]\(([^)\s]+)\)', re.IGNORECASE)
#: 编号标题，如 `## 4.1 概述`。convert 会把它当成自动编号，把编号从文本里吃掉，
#: 编号变成重组后的 1、2、3…；改写原文编号才能对上正文里的「按 4.1 执行」。
#: 加粗、行内代码、去空格、反斜杠转义、HTML 实体都试过，转换前都会被还原成数字开头。
HEADING_NUMBER_RE = re.compile(r"^(#{1,6}[ \t]+)(\d+(?:\.\d+)*)(?=[ \t]|$)")
#: 挡住行首的不可见字符（U+2063 INVISIBLE SEPARATOR）。转换后抹掉，标题文本与原文一致。
HEADING_MASK = "\u2063"


def api(method: str, path: str, data: dict | None, timeout: int,
        query: dict | None = None) -> dict:
    action = path.rsplit("/", 1)[-1]
    return _wps.client.api(method, path, body=data, query=query, timeout=timeout,
                           operation=f"智能文档 {action}", read_only=action in {"blocks", "convert"})


def block_children(data: dict, block_id: str = "doc") -> list:
    """从 blocks 查询响应里取出目标块的子块列表。

    响应结构是 data.block[<block_id>]，块以 block_id 为键存放 ——
    按 data.block.children 取会永远得到空列表，进而让 replace 变成静默追加。
    """
    block = data.get("block") or {}
    node = block.get(block_id)
    if not isinstance(node, dict) or not isinstance(node.get("children"), list):
        raise _wps.WpsError("读取智能文档块", "无法确认正文块树")
    return node["children"]


def doc_children(file_id: str, timeout: int) -> list:
    """文档根下的一级子块列表。"""
    data = api("post", f"/v7/airpage/v2/{_wps.segment(file_id)}/blocks", {"block_id": "doc"}, timeout)
    return block_children(data, "doc")


def backup_blocks(file_id: str, blocks: list) -> None:
    with tempfile.NamedTemporaryFile("w", prefix="wps-agent-otl-backup-", suffix=".json",
                                     encoding="utf-8", delete=False) as handle:
        json.dump({"file_id": file_id, "children": blocks}, handle, ensure_ascii=False)
        print(f"正文块备份：{handle.name}")


def block_preview(block: dict, limit: int = 30) -> str:
    """一级块的开头文字，用于人工确认块树位置。"""
    kind = next(iter(block), "")
    node = block.get(kind) or {}
    if kind in {"paragraph", "heading", "blockquote"}:
        text = "".join((element.get("text") or {}).get("attributes", {}).get("content", "")
                       for element in node.get("elements") or [])
        return text[:limit]
    if kind == "table":
        return f"<table {len(node.get('rows') or [])} 行>"
    return f"<{kind}>"


def describe_children(children: list, progress=print) -> None:
    """打印块树概况：总数、类型分布、首末块 —— 导出接口有缓存，不能用它判断写入结果。"""
    kinds: dict[str, int] = {}
    for block in children:
        kind = next(iter(block), "")
        kinds[kind] = kinds.get(kind, 0) + 1
    detail = " / ".join(f"{kind} {count}" for kind, count in sorted(kinds.items(), key=lambda item: -item[1]))
    progress(f"一级块 {len(children)} 个（{detail or '空'}）")
    if children:
        progress(f"  首块 {block_preview(children[0])!r}　末块 {block_preview(children[-1])!r}")


def delete_range(file_id: str, start: int, end: int, timeout: int) -> list:
    """按一级块索引区间删除，用于清理 replace 之后残留的原块。"""
    if not 0 <= start < end:
        raise ValueError("区间需满足 0 ≤ START < END")
    api("post", f"/v7/airpage/v2/{_wps.segment(file_id)}/blocks/delete",
        {"block_id": "doc", "start_index": start, "end_index": end}, timeout)
    print(f"已删除一级块区间 [{start}, {end})")
    children = doc_children(file_id, timeout)
    describe_children(children)
    return children


def strip_front_matter(md: str) -> tuple[str, dict]:
    """摘掉文首的 YAML front matter（返回 正文, 元数据）。

    直接把 `---` 包起来的 YAML 喂给 convert，会变成一条水平线加一整行 YAML 文本。
    """
    meta: dict[str, str] = {}
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            for line in md[3:end].strip().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
            md = md[end + 4:]
    return md.lstrip("\n"), meta


def resolve_title(explicit: str | None, meta_title: str | None,
                  md_path: str | None) -> str:
    """决定文档标题：显式文本 > front matter 的 title > md 文件名。

    新建的智能文档**标题块是空的**（只有文件名），所以写正文时顺手补上，
    否则文档顶部一直是「输入标题」占位。
    """
    if explicit and explicit not in ("auto", "none"):
        return explicit.strip()
    if explicit == "none":
        return ""
    if meta_title:
        return meta_title.strip()
    if md_path:
        return os.path.splitext(os.path.basename(md_path))[0].strip()
    return ""


def title_text(data: dict) -> str:
    node = ((data.get("block") or {}).get("doc") or {}).get("title") or {}
    return "".join(str(((el or {}).get("text") or {}).get("attributes", {}).get("content") or "")
                   for el in (node.get("elements") or []))


def doc_title(file_id: str, timeout: int) -> str:
    data = api("post", f"/v7/airpage/v2/{_wps.segment(file_id)}/blocks", {"block_id": "doc"}, timeout)
    return title_text(data)


def set_title(file_id: str, title: str, timeout: int) -> None:
    """写标题块。`update_elements` 要按块类型再嵌一层，标题是 `title_elements`。"""
    api("post", f"/v7/airpage/v2/{_wps.segment(file_id)}/blocks/update",
        {"block_id": "title",
         "update_elements": {"title_elements": {"elements": [
             {"text": {"attributes": {"content": title}, "range_marks": []}}]}}}, timeout)


def ensure_title(file_id: str, title: str, timeout: int,
                 overwrite: bool = False, expected: str | None = None) -> str:
    if not title:
        return "unchanged"
    current = doc_title(file_id, timeout)
    if current == title:
        return "unchanged"
    if current and not overwrite:
        return "kept"
    if overwrite and current != expected:
        raise ValueError("正文已处理。标题在快照之后发生变化，请重新读取后再改标题")
    try:
        set_title(file_id, title, timeout)
    except _wps.WpsError as exc:
        if not exc.uncertain:
            raise
        print("标题写入响应异常，正在读回确认结果")
    if doc_title(file_id, timeout) != title:
        sys.exit("[otl_write] 标题写后读回不一致，请检查云端结果")
    return "set"


def is_blank_paragraph(block: dict) -> bool:
    """新建的空文档自带一个没有 elements 的段落；它占着索引，写之前要清掉。"""
    if not isinstance(block, dict) or next(iter(block), "") != "paragraph":
        return False
    return not ((block.get("paragraph") or {}).get("elements") or [])


# ---------------------------------------------------------------- 并发冲突检查

def _block_id(block: dict) -> str:
    """块是单键 union（{"heading": {...}}），block_id 在内层。"""
    inner = next(iter(block.values())) if isinstance(block, dict) and block else {}
    return str((inner or {}).get("block_id") or "")


def snapshot(file_id: str, timeout: int,
             scope_ids: list[str] | None = None,
             scope_index: list[int] | None = None) -> dict:
    """记录每个一级块的哈希，并声明「本次改动依赖哪些块」。

    局部对比的前提是**自己知道要改哪几个块**：`blocks` 存全文（用于整体判断），
    `scope` 存本次真正依赖/会覆盖的块。写入前只校验 scope 内的块是否变化 ——
    别人改了无关的块不该挡住我。普通文件没有块结构，只能比整文件哈希
    （见 file_write.py）。
    """
    data = api("post", f"/v7/airpage/v2/{_wps.segment(file_id)}/blocks", {"block_id": "doc"}, timeout)
    children = block_children(data, "doc")
    want_ids = set(scope_ids or [])
    want_idx = set(scope_index or [])
    blocks, scope = [], []
    for i, block in enumerate(children):
        bid = _block_id(block)
        blob = json.dumps(block, sort_keys=True, ensure_ascii=False).encode()
        blocks.append({"block_id": bid, "hash": hashlib.sha1(blob).hexdigest()})
        if bid in want_ids or i in want_idx:
            scope.append(bid)
    if want_ids - set(scope) or any(index < 0 or index >= len(children) for index in want_idx):
        sys.exit("[otl_write] 快照指定了不存在的块或索引")
    return {"file_id": file_id, "version": data.get("version"), "title": title_text(data),
            "scope_explicit": scope_ids is not None or scope_index is not None,
            "scope": scope, "blocks": blocks}


def diff_maps(old_map: dict, new_map: dict) -> tuple[list[str], list[str], list[str]]:
    """返回 (新增, 删除, 内容变化) 的 block_id 列表。"""
    added = [i for i in new_map if i not in old_map]
    removed = [i for i in old_map if i not in new_map]
    changed = [i for i in old_map if i in new_map and old_map[i] != new_map[i]]
    return added, removed, changed


def verify_snapshot(file_id: str, snap_path: str, mode: str, timeout: int,
                    allow_added: bool, force: bool, target_id: str | None = None) -> int:
    """写入前做**局部**对比：只看快照声明依赖的块有没有被改。

    * 声明了 scope：只有 scope 内的块**被改或被删**才算冲突；无关块变了只提示。
    * 没声明 scope：保守处理，全文任何变化都算冲突（`--allow-added` 可放行纯新增）。
    * `mode=replace`：会清空重写，无论 scope 都按全文比对。
    """
    if not os.path.exists(snap_path):
        sys.exit(f"[otl_write] 找不到快照文件 {snap_path}")
    with open(snap_path, encoding="utf-8") as fh:
        old = json.load(fh)
    if old.get("file_id") != file_id or not isinstance(old.get("blocks"), list):
        sys.exit("[otl_write] 快照与目标文档不匹配")
    if target_id and old.get("scope_explicit") and target_id not in old.get("scope", []):
        sys.exit("[otl_write] 快照未包含要修改的块")
    new = snapshot(file_id, timeout)

    old_full = {b["block_id"]: b["hash"] for b in old.get("blocks") or []}
    new_full = {b["block_id"]: b["hash"] for b in new.get("blocks") or []}
    added, removed, changed = diff_maps(old_full, new_full)

    old_order = [b["block_id"] for b in old.get("blocks") or []]
    new_order = [b["block_id"] for b in new.get("blocks") or []]
    new_ids = set(new_order)
    old_ids = set(old_order)
    old_shared_order = [bid for bid in old_order if bid in new_ids]
    new_shared_order = [bid for bid in new_order if bid in old_ids]
    order_changed = old_shared_order != new_shared_order
    order_changed_ids = ([old_bid for old_bid, new_bid in
                          zip(old_shared_order, new_shared_order) if old_bid != new_bid]
                         if order_changed else [])

    scope = old.get("scope") or []
    explicit = bool(old.get("scope_explicit"))
    sc_changed = [i for i in scope if i in new_full and new_full[i] != old_full.get(i)]
    sc_gone = [i for i in scope if i not in new_full]

    if not (added or removed or changed or order_changed):
        print(f"并发检查：文档未被改动（{len(new_full)} 个一级块，"
              f"version {old.get('version')}→{new.get('version')}）")
        return len(new_full)

    detail = "；".join(
        [f"{len(changed)} 个块内容变化"] * bool(changed)
        + [f"{len(removed)} 个块被删除"] * bool(removed)
        + [f"{len(added)} 个块新增"] * bool(added)
        + ["一级块顺序变化"] * bool(order_changed))

    if mode == "replace":
        conflict, why = True, "replace 会清空重写，按全文比对"
    elif explicit:
        conflict = bool(sc_changed or sc_gone)
        why = f"只比对声明的 {len(scope)} 个依赖块"
    elif allow_added and not changed and not removed and not order_changed:
        conflict, why = False, "仅新增块，未改未删"
    else:
        conflict, why = True, "未声明依赖块，保守按全文比对"

    if not conflict:
        print(f"并发检查：文档整体有变化（{detail}），但{why} —— 不影响本次写入，继续")
        return len(new_full)
    if force:
        print(f"⚠️  已忽略冲突（--force）：{detail}")
        return len(new_full)

    hit = ((sc_changed + sc_gone) if (explicit and mode != "replace")
           else changed + removed + added + order_changed_ids)
    hit = list(dict.fromkeys(hit))
    sys.exit("[otl_write] ⚠️  冲突：本次依赖的块已被他人修改，已放弃写入。\n"
             f"   判定：{why}\n"
             f"   文档整体：{detail}\n"
             f"   受影响：{', '.join(hit[:8])}{' …' if len(hit) > 8 else ''}\n"
             "   处理：先 otl_fetch.py 重新读取、把你的改动合并进去再写；"
             "确认要覆盖请加 --force。")


def block_summary(block: dict) -> str:
    """给块生成一行摘要，便于挑出要声明的依赖块。"""
    kind = next(iter(block)) if isinstance(block, dict) and block else "?"
    inner = next(iter(block.values())) if isinstance(block, dict) and block else {}
    texts = []
    for el in (inner or {}).get("elements") or []:
        for key, val in (el or {}).items():
            if key == "text":
                texts.append(str(((val or {}).get("attributes") or {}).get("content") or ""))
    return f"[{kind}] {''.join(texts).strip().replace(chr(10), ' ')[:52]}"


def list_blocks(file_id: str, timeout: int) -> None:
    """列出根下所有一级块：索引、block_id、摘要 —— 用来决定 scope。"""
    data = api("post", f"/v7/airpage/v2/{_wps.segment(file_id)}/blocks", {"block_id": "doc"}, timeout)
    children = block_children(data, "doc")
    print(f"version={data.get('version')}  一级块 {len(children)} 个")
    for i, block in enumerate(children):
        print(f"  {i:>4}  {_block_id(block):<16} {block_summary(block)}")


# ---------------------------------------------------------------- 本地图片

def image_plan(md: str, base: str) -> list[dict]:
    """按出现顺序列出 Markdown 里引用的图片；本地文件与远程 URL 分开标记。"""
    plan = []
    base = os.path.realpath(base)
    for match in IMAGE_RE.finditer(md):
        src = (match.group(1) or match.group(2) or "").strip()
        if not src:
            continue
        clean = src.split("?")[0].split("#")[0]
        if clean.startswith(("http://", "https://", "//", "data:")):
            plan.append({"src": src, "path": None, "local": False})
            continue
        path = os.path.realpath(os.path.join(base, clean))
        if os.path.commonpath([base, path]) != base:
            raise ValueError("图片路径超出 Markdown 或 --img-base 指定的目录")
        plan.append({"src": src, "path": path, "local": os.path.isfile(path)})
    return plan


def image_size(path: str) -> tuple[int, int]:
    """图片原始宽高；picture 块写死要求 width/height > 0。"""
    try:
        from PIL import Image
    except ImportError:
        sys.exit("[otl_write] 上传本地图片需要带 Pillow 的 Python 解释器")
    with Image.open(path) as img:
        if img.format not in {"PNG", "JPEG", "GIF", "WEBP", "BMP"}:
            raise ValueError("本地图片只支持 PNG、JPEG、GIF、WebP 和 BMP")
        return img.size


def upload_attachment(file_id: str, path: str, timeout: int, expected_hash: str) -> str:
    """本地文件 → 智能文档附件 → 附件 id（13 位，就是 picture 块的 `id`）。

    三步：申请上传地址 → PUT 字节 → complete。**不能**用
    `/v7/coop/airpage/{_wps.segment(file_id)}/attachments/upload`（该路由服务端返回
    `method AttachmentsUpload not implemented`）。
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    if hashlib.sha256(blob).hexdigest() != expected_hash:
        raise ValueError("本地图片在准备后发生变化，已停止上传")
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp",
             ".svg": "image/svg+xml", ".bmp": "image/bmp"}.get(ext, "application/octet-stream")
    addr = api("post", f"/v7/coop/files/{_wps.segment(file_id)}/attachments/upload/address",
               {"name": name, "size": len(blob), "content_type": ctype,
                "md5": hashlib.md5(blob).hexdigest()}, timeout)
    request = addr.get("request") or {}
    if not request.get("url") or not addr.get("upload_id"):
        sys.exit("[otl_write] 申请附件上传地址失败，响应缺少地址或上传 ID")
    with _wps.client.transfer(request["url"], request.get("method") or "PUT", timeout=timeout,
                              data=blob, headers=_wps.transfer_headers(request.get("headers")),
                              operation="上传智能文档附件") as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}

    def pick(value):
        """send_back_params 的值形如 `header.ETag` / `header.newfilename`：从 PUT 响应头取。

        不同存储后端的头名不一样（ks3_compatible 是 newfilename，obs 是 x-obs-save-key），
        写死头名会让 complete 报 `params is empty, must contain key and etag`。
        """
        if isinstance(value, str) and value.lower().startswith("header."):
            return headers.get(value.split(".", 1)[1].lower())
        return value

    params = {k: pick(v) for k, v in (addr.get("send_back_params") or {}).items()}
    params.setdefault("etag", headers.get("etag"))
    if not params.get("key"):
        params["key"] = headers.get("x-obs-save-key") or headers.get("newfilename")
    params["etag"] = (params.get("etag") or "").strip('"')
    if not params.get("key"):
        sys.exit(f"[otl_write] 附件上传后取不到 key，PUT 响应头：{sorted(headers)}")
    done = api("post", f"/v7/coop/files/{_wps.segment(file_id)}/attachments/upload/complete",
               {"upload_id": addr["upload_id"], "params": params}, timeout)
    attachment_id = (done or {}).get("attachment_id")
    if not attachment_id:
        sys.exit("[otl_write] 附件提交响应缺少 attachment_id")
    return str(attachment_id)


def picture_refs(blocks: list) -> list[tuple[list, int, dict]]:
    """按文档顺序收集 picture 块，连同它所在的列表与下标（便于原地替换）。"""
    refs: list[tuple[list, int, dict]] = []

    def walk(seq: list) -> None:
        for index, block in enumerate(seq):
            if not isinstance(block, dict) or not block:
                continue
            kind = next(iter(block))
            inner = block[kind] or {}
            if kind == "picture":
                refs.append((seq, index, inner))
                continue
            if kind == "picture_column":
                children = inner.get("children") or []
                for position, child in enumerate(children):
                    if isinstance(child, dict) and "picture" in child:
                        refs.append((children, position, child["picture"]))
                continue
            for key in ("children", "columns"):
                for child in inner.get(key) or []:
                    if isinstance(child, dict):
                        walk([child])
            for row in inner.get("rows") or []:
                for cell in row.get("cells") or []:
                    walk(cell.get("children") or [])

    walk(blocks)
    return refs


def placeholder_paragraph(text: str) -> dict:
    return {"paragraph": {"attributes": {"align": "left"},
                          "elements": [{"text": {"attributes": {"content": text},
                                                 "range_marks": []}}],
                          "range_marks": []}}


def fill_pictures(blocks: list, plan: list[dict], img_pct: int, doc_width: int,
                  progress=print) -> int:
    """把上传好的附件 id / 真实尺寸回填到对应的 picture 块。

    convert 出的 picture 块顺序与 Markdown 里 `<img>` / `![]()` 的出现顺序一致，
    所以按位置配对即可；远程图片没有本地字节，替换成一行说明而不是留空 id。
    """
    refs = picture_refs(blocks)
    if len(refs) != len(plan):
        sys.exit(f"[otl_write] 图片块 {len(refs)} 个，但 Markdown 里找到 {len(plan)} 张图 —— "
                 "顺序配对不成立，请检查是否有宏/代码块里的假图片语法")
    for (seq, index, inner), item in zip(refs, plan):
        if not item["local"]:
            seq[index] = placeholder_paragraph(f"〔未上传的远程图片：{item['src']}〕")
            progress(f"  跳过远程图片（无本地字节）：{item['src']}")
            continue
        width = item.get("width") or 0
        height = item.get("height") or 0
        target = min(round(img_pct * doc_width / 100), width) if img_pct else width
        inner["attributes"] = {"align": "center", "border_type": "none", "rotate": 0,
                              "id": item["id"], "width": width, "height": height,
                              "render_width": target}
        inner["range_marks"] = inner.get("range_marks") or []
        inner.pop("block_id", None)
    return len(refs)


# ---------------------------------------------------------------- 超长表格

def _paragraph_text(paragraph) -> str:
    """段落里的纯文本，用于量宽度。"""
    out = []
    for element in (paragraph or {}).get("elements") or []:
        text = element.get("text") if isinstance(element, dict) else None
        if isinstance(text, dict):
            content = (text.get("attributes") or {}).get("content")
            if isinstance(content, str):
                out.append(content)
    return "".join(out)


def _cell_paragraphs(cell) -> list[str]:
    """单元格里每一段的文本。

    服务端按最长的一行给列宽（同一段里不会自动换行），所以必须逐段量，不能把一段
    以上的内容拼成一整串再算。
    """
    lines = []
    for child in (cell or {}).get("children") or []:
        if isinstance(child, dict) and "paragraph" in child:
            lines.append(_paragraph_text(child["paragraph"]))
    return lines


def _text_weight(text: str) -> float:
    """按字宽折算：汉字与全角算 1，ASCII 约 0.55。"""
    weight = 0.0
    for char in text:
        weight += 1.0 if unicodedata.east_asian_width(char) in {"W", "F"} else 0.55
    return weight


def _line_width(weight: float, floor: int = TABLE_MIN_COL) -> int:
    """行长对应的列宽：floor 以内按 floor，之后每字加 TABLE_COL_PER_CHAR。"""
    return int(round(floor + TABLE_COL_PER_CHAR * max(0.0, weight - TABLE_COL_BASE_CHARS)))


#: 不能从中间断开的词：标识符、类型、代码。汉字可逐字断行，这些断了就读不懂。
TOKEN_RE = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_.\-/%()]*")


def _token_weight(text: str) -> float:
    """最长不可断词的宽度。汉字算 1（可逐字断开），ASCII 词整体算。"""
    return max((_text_weight(match.group(0)) for match in TOKEN_RE.finditer(text or "")),
               default=1.0)


def _chars_for_width(width: int, floor: int = TABLE_MIN_COL) -> int:
    """宽度能放多少个字，与 _line_width 互为反函数。"""
    chars = int((width - floor) // TABLE_COL_PER_CHAR) + TABLE_COL_BASE_CHARS
    return max(chars, TABLE_COL_BASE_CHARS)


def _column_metrics(table: dict, floor: int = TABLE_MIN_COL) -> tuple[list[int], list[int], list[float]]:
    """返回（每列自然宽度, 每列不可再压的下限, 每列正文字数）。

    - 自然宽度：该列所有段落里最长的一行；跨列单元格只记到它起始的那一列。实测
      `colspan=2` 的表头写成 740 后回读是 [260, 164, ...]，合并内容只挤起始列。
    - 不可再压的下限：普通列取 floor 与「最长不可断词」的较大者——汉字能逐字断行，
      但 `ST_RSVRSTRL_B` 这类标识符断开就成了两行乱码；跨列单元格另按自然宽度抬升，
      切行会改变内容，合并单元格不做切行，所以它的宽度是硬约束。
    - 正文字数：用来分配版心宽度，只看数据行不看表头；手动调列宽看的也是内容量。
    """
    rows = table.get("rows") or []
    grid = (table.get("attributes") or {}).get("grid") or []
    columns = len(grid)
    if not columns:
        columns = max((sum(max(int((cell.get("attributes") or {}).get("colspan") or 1), 1)
                           for cell in (row.get("cells") or [])) for row in rows), default=0)
    if columns <= 0:
        return [], [], []
    natural = [float(floor)] * columns
    baseline = [float(floor)] * columns
    weights = [0.0] * columns
    pending = [0] * columns          # 被上方 rowspan 占住的列，>0 表示还占几行
    for row_index, row in enumerate(rows):
        column = 0
        for cell in row.get("cells") or []:
            while column < columns and pending[column] > 0:
                column += 1
            if column >= columns:
                break
            attributes = cell.get("attributes") or {}
            colspan = max(int(attributes.get("colspan") or 1), 1)
            rowspan = max(int(attributes.get("rowspan") or 1), 1)
            lines = _cell_paragraphs(cell)
            longest = max((_text_weight(line) for line in lines), default=0.0)
            need = _line_width(longest, floor)
            body = 0.0 if row_index == 0 else sum(_text_weight(line) for line in lines)
            span = min(column + colspan, columns)
            natural[column] = max(natural[column], need)
            if colspan > 1:
                baseline[column] = max(baseline[column], need)
            else:
                token_need = max((_line_width(_token_weight(line), floor) for line in lines),
                                 default=floor)
                baseline[column] = max(baseline[column], token_need)
            for index in range(column, span):
                weights[index] += body / colspan
                if rowspan > 1:
                    pending[index] = rowspan
            column = span
        pending = [max(value - 1, 0) for value in pending]
    return ([int(round(value)) for value in natural],
            [int(round(value)) for value in baseline], weights)


def _plan_widths(weights: list[float], minima: list[int], total: int,
                 floor: int = TABLE_MIN_COL) -> list[int] | None:
    """按内容量把版心宽度分给各列：先满足内容下限，余量按字数开方分配。

    开方是为了缓和差距：一列字数再多也只拿同量级列的两三倍，避免一列独大、其余
    挤成竖排。
    """
    columns = len(weights)
    if columns <= 0 or len(minima) != columns:
        return None
    base = [max(floor, low) for low in minima]
    if sum(base) > total:
        return None
    extra = total - sum(base)
    if extra <= 0 or not any(weights):
        return base
    roots = [math.sqrt(max(weight, 0.0)) for weight in weights]
    root_sum = sum(roots) or columns
    exact = [low + extra * root / root_sum for low, root in zip(base, roots)]
    widths = [int(value) for value in exact]
    gap = total - sum(widths)
    order = sorted(range(columns), key=lambda i: exact[i] - widths[i], reverse=True)
    for index in order[:max(gap, 0)]:
        widths[index] += 1
    return widths


BREAK_CHARS = "，。；：、！？）”』】》,.;:!?)]}"


def _atoms(text: str) -> list[str]:
    """切成不可再分的单元：汉字单字，ASCII 词整体。"""
    atoms = []
    index = 0
    while index < len(text):
        match = TOKEN_RE.match(text, index)
        if match and match.end() > index:
            atoms.append(match.group(0))
            index = match.end()
        else:
            atoms.append(text[index])
            index += 1
    return atoms


def _break_point(line: str, limit: int) -> int:
    """行内最后一个适合断行的位置（标点或空白之后），太靠前就不切。"""
    for index in range(min(len(line), limit) - 1, max(limit // 2, 1) - 1, -1):
        if line[index] in BREAK_CHARS or line[index].isspace():
            return index + 1
    return 0


def _split_text(text: str, limit: int) -> list[str]:
    """把文字切成每行 limit 字，优先在标点或空白处断开，标识符不拆开。"""
    if limit <= 0:
        return [text]
    pieces = []
    line = ""
    for atom in _atoms(text):
        if not line and atom.isspace():
            continue                            # 行首不留空白
        if len(line) + len(atom) <= limit:
            line += atom
            continue
        if not line:
            line = atom                        # 单个词就超宽，独占一行
            continue
        cut = _break_point(line, limit)
        if cut:
            pieces.append(line[:cut].rstrip())
            line = line[cut:].lstrip() + atom
        else:
            pieces.append(line.rstrip())
            line = atom.lstrip()
    if line.strip():
        pieces.append(line)
    return pieces


def _reflow_cell(cell: dict, limit: int) -> bool:
    """把单元格里超过 limit 字的段落拆成多段，相当于手动拖窄列后的换行。"""
    changed = False
    out = []
    for child in cell.get("children") or []:
        paragraph = child.get("paragraph") if isinstance(child, dict) else None
        if not isinstance(paragraph, dict):
            out.append(child)
            continue
        text = _paragraph_text(paragraph)
        elements = paragraph.get("elements") or []
        # 只处理纯文本段落：多元素的有行内样式，按字符重组会丢掉样式边界
        if len(text) <= limit or len(elements) != 1:
            out.append(child)
            continue
        for piece in _split_text(text, limit):
            clone = json.loads(json.dumps(paragraph, ensure_ascii=False))
            clone["elements"] = [{"text": {"attributes": {"content": piece}}}]
            clone.pop("block_id", None)
            clone.pop("range_marks", None)
            out.append({"paragraph": clone})
        changed = True
    if changed:
        cell["children"] = out
    return changed


def reflow_table(table: dict, total: int, floor: int = TABLE_MIN_COL,
                 tolerance: float = TABLE_OVERFLOW_TOLERANCE) -> bool | None:
    """按内容重排超宽表格：切行让列宽总和回到版心。

    服务端只按单元格最长行给宽、不会自动换行，所以列宽写小也没用，整张表会被重算。
    要真正压窄只能自己决定在哪里换行：先按正文字数把版心分给各列，再把每列里超过
    该宽度的段落切成多段。列数太多、每列连下限都分不到时不切行——切了也进不了版心，
    只会把手动调整时要删的空行留下。返回 True 表示已重排，False 表示不动。
    """
    natural, baseline, weights = _column_metrics(table, floor)
    if not natural:
        return False
    grid = (table.get("attributes") or {}).get("grid") or []
    known = [value for value in grid if isinstance(value, (int, float))] if isinstance(grid, list) else []
    # 以「自然宽度」和 WPS 自己给的 grid 中较大的为准，都超出版心才值得切行
    current = max(sum(natural), int(sum(known)) if len(known) == len(natural) else 0)
    if current <= total * (1 + tolerance):
        return False
    targets = _plan_widths(weights, baseline, total, floor)
    if targets is None:
        return False
    rows = table.get("rows") or []
    pending = [0] * len(targets)
    for row in rows:
        column = 0
        for cell in row.get("cells") or []:
            while column < len(targets) and pending[column] > 0:
                column += 1
            if column >= len(targets):
                break
            attributes = cell.get("attributes") or {}
            colspan = max(int(attributes.get("colspan") or 1), 1)
            rowspan = max(int(attributes.get("rowspan") or 1), 1)
            span = min(column + colspan, len(targets))
            # 跨列单元格的宽度只挤压起始列，下限已算在 targets[column] 里，不再切
            if colspan == 1:
                _reflow_cell(cell, _chars_for_width(targets[column], floor))
            if rowspan > 1:
                for index in range(column, span):
                    pending[index] = rowspan
            column = span
        pending = [max(value - 1, 0) for value in pending]
    attributes = table.setdefault("attributes", {})
    attributes["grid"] = targets
    attributes["width"] = total
    return True


def reflow_tables(blocks: list, total: int, tolerance: float = TABLE_OVERFLOW_TOLERANCE) -> int:
    """给所有超宽表格切行，返回重排的张数。"""
    if total <= 0:
        return 0
    reflowed = 0
    for block in blocks:
        table = block.get("table") if isinstance(block, dict) else None
        if isinstance(table, dict) and reflow_table(table, total, tolerance=tolerance):
            reflowed += 1
    return reflowed


def _shrink_grid(grid: list, total: int, minima: list[int] | int) -> list | None:
    """把列宽压到 total，每列不低于自己的内容下限；总下限超了就返回 None。"""
    widths = []
    for value in grid:
        try:
            widths.append(max(int(value), 1))
        except (TypeError, ValueError):
            return None
    if isinstance(minima, int):
        minima = [minima] * len(widths)
    if not widths or len(minima) != len(widths) or sum(minima) > total:
        return None
    extra = total - sum(minima)
    caps = [max(width - low, 0) for width, low in zip(widths, minima)]
    if sum(caps) >= extra:                     # 有余量的列按余量分，窄列不再被压
        weight = sum(caps) or len(caps)
        shares = [extra * cap / weight for cap in caps]
    else:                                      # 全部压到下限还不够，按原宽比例再加
        weight = sum(widths) or len(widths)
        shares = [extra * width / weight for width in widths]
    exact = [low + share for low, share in zip(minima, shares)]
    scaled = [int(value) for value in exact]
    gap = total - sum(scaled)
    if gap > 0:                                # 取整丢的像素补给小数部分最大的列
        order = sorted(range(len(exact)), key=lambda i: exact[i] - scaled[i], reverse=True)
        for index in order[:gap]:
            scaled[index] += 1
    return scaled


def fit_table_widths(blocks: list, total: int, min_col: int = TABLE_MIN_COL) -> tuple[int, int]:
    """把超宽表格压到正文宽度，返回（压住的张数, 内容放不下的张数）。

    服务端只把写入的 grid 当期望值，最终按单元格内容兜底：单列下限 116px（1~5 个
    汉字），之后每多一个字加 16px（9 字约 180px、13 字约 244px）。列数 × 内容下限
    超过版心时，压也白压，只能改内容结构（减少列、缩短单元格或拆表）。HTML 表格与
    Markdown 表格一样在这里处理：两者都经 convert 变成同一套 table 块，合并单元格
    的内容按跨列均摊。
    """
    if total <= 0:
        return 0, 0
    fitted = crowded = 0
    for block in blocks:
        table = block.get("table") if isinstance(block, dict) else None
        if not isinstance(table, dict):
            continue
        attributes = table.setdefault("attributes", {})
        grid = attributes.get("grid")
        known = [value for value in grid if isinstance(value, (int, float))] if isinstance(grid, list) else []
        # 已经按切行排好的表不再二次压缩：合并单元格会让下限估算偏大
        if known and attributes.get("width") == total and sum(known) == total:
            continue
        minima = _column_metrics(table, min_col)[0]
        if not minima:
            continue
        if len(known) != len(minima):
            known = list(minima)               # 没有 grid 时先按内容下限铺开
            attributes["grid"] = known
        if sum(minima) > total:
            crowded += 1
            continue
        # 服务端会把低于内容下限的列撑到下限，所以按「原宽与下限取大」判断是否需要压
        desired = [max(int(value), low) for value, low in zip(known, minima)]
        if sum(desired) <= total:
            continue
        scaled = _shrink_grid(desired, total, minima)
        if scaled is None:
            crowded += 1
            continue
        attributes["grid"] = scaled
        attributes["width"] = total
        fitted += 1
    return fitted, crowded


def overflowing_tables(blocks: list, total: int,
                       tolerance: float = TABLE_OVERFLOW_TOLERANCE) -> list[tuple[int, int, int]]:
    """列出仍宽于版心的表：返回 [(块序号, 列数, 总宽)]。"""
    if total <= 0:
        return []
    limit = total * (1 + tolerance)
    found = []
    for index, block in enumerate(blocks):
        table = block.get("table") if isinstance(block, dict) else None
        if not isinstance(table, dict):
            continue
        grid = (table.get("attributes") or {}).get("grid")
        if isinstance(grid, list) and grid and all(isinstance(v, (int, float)) for v in grid):
            if sum(grid) > limit:
                found.append((index, len(grid), int(sum(grid))))
    return found


def split_wide_tables(blocks: list, max_rows: int = MAX_TABLE_ROWS) -> tuple[list, int]:
    """把超过 max_rows 行的表格拆成多张（重复表头）。

    服务端 table.rows 是 maxItems=100，超了写入直接 400；这些标准里的字段清单表
    动辄几百行，只能拆。返回 (新块列表, 拆分出来的额外表格数)。
    """
    out, split = [], 0
    for block in blocks:
        rows = (block.get("table") or {}).get("rows") if isinstance(block, dict) else None
        if not rows or len(rows) <= max_rows:
            out.append(block)
            continue
        head, data = rows[0], rows[1:]
        step = max_rows - 1
        for start in range(0, len(data), step):
            clone = json.loads(json.dumps(block, ensure_ascii=False))
            clone["table"]["rows"] = json.loads(
                json.dumps([head] + data[start:start + step], ensure_ascii=False))
            _strip_block_ids(clone)
            out.append(clone)
            split += 1
        split -= 1  # 第一张不算额外
    return out, split


def _strip_block_ids(obj) -> None:
    """拆分出的副本不能复用原表的 block_id，否则同一批里 id 撞车。"""
    if isinstance(obj, dict):
        obj.pop("block_id", None)
        for value in obj.values():
            _strip_block_ids(value)
    elif isinstance(obj, list):
        for value in obj:
            _strip_block_ids(value)


# ---------------------------------------------------------------- 写入主流程

def file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for piece in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(piece)
    return digest.hexdigest()


def mask_heading_numbers(md: str) -> tuple[str, int]:
    """在编号标题的行首插入不可见占位符，返回（新 Markdown, 处理条数）。

    WPS 的 Markdown 转换把 `## 4.1 概述` 当成自动编号：编号从文本里消失，标题改成
    重组后的 1、2、3…。行首多一个不可见字符后不再命中该规则，转换后抹掉即得原文。
    """
    lines: list[str] = []
    masked = 0
    for line in md.splitlines():
        match = HEADING_NUMBER_RE.match(line)
        if match:
            head = match.group(1)
            line = head + HEADING_MASK + line[len(head):]
            masked += 1
        lines.append(line)
    return "\n".join(lines), masked


def unmask_headings(blocks: list) -> int:
    """抹掉标题文本里的占位符，返回还原的标题数。"""
    restored = 0
    for block in blocks:
        if not (isinstance(block, dict) and isinstance(block.get("heading"), dict)):
            continue
        hit = False
        for element in block["heading"].get("elements") or []:
            text = element.get("text")
            if not isinstance(text, dict):
                continue
            attributes = text.get("attributes") or {}
            content = attributes.get("content")
            if isinstance(content, str) and HEADING_MASK in content:
                attributes["content"] = content.replace(HEADING_MASK, "")
                hit = True
        restored += 1 if hit else 0
    return restored


FENCE_RE = re.compile(r"^```[^\n]*\n.*?^```", re.MULTILINE | re.DOTALL)
HTML_TABLE_RE = re.compile(r"<table\b[^>]*>(.*?)</table\s*>", re.IGNORECASE | re.DOTALL)


def check_html_tables(md: str) -> None:
    """HTML 表格必须带 thead 或 tbody。

    实测 `<table><tr><td>` 会被 convert 整段丢掉，只剩一个空的 column 块，正文里
    的表格凭空消失且没有任何报错。围栏代码块里的示例跳过，不算正文表格。
    """
    text = FENCE_RE.sub("", md)
    text = re.sub(r"`[^`\n]*`", "", text)          # 行内代码里的示例也不算正文表格
    for match in HTML_TABLE_RE.finditer(text):
        inner = match.group(1).lower()
        if "<thead" in inner or "<tbody" in inner:
            continue
        snippet = re.sub(r"\s+", " ", match.group(0))[:60]
        sys.exit(f"[otl_write] HTML 表格缺少 <thead>/<tbody>，转换会整段丢弃：{snippet}…；"
                 "补上表头或表体标签再写入")


def prepare_markdown(file_id: str, md: str, *, base: str = ".",
                     timeout: int = 180, dry_run: bool = False,
                     images: str = "auto", img_pct: int = DEFAULT_IMG_PCT,
                     doc_width: int = DOC_WIDTH, table_width: int = DOC_WIDTH,
                     reflow: bool = True, progress=print) -> dict:
    """Prepare all blocks before editing the document body."""
    check_html_tables(md)
    plan = image_plan(md, base) if images != "off" else []
    masked_md, masked = mask_heading_numbers(md)
    converted = api("post", f"/v7/airpage/v2/{_wps.segment(file_id)}/blocks/convert",
                    {"format": "markdown", "content": masked_md}, timeout)
    blocks = converted.get("blocks") or []
    if not blocks:
        sys.exit("[otl_write] 转换结果为空，未写入任何内容")
    if masked:
        restored = unmask_headings(blocks)
        if restored != masked:
            sys.exit(f"[otl_write] 标题编号占位符未完全还原（{restored}/{masked}），已停止；"
                     "未写入任何内容")
        progress(f"标题编号：{masked} 个按原文保留，不转成自动编号")

    kinds: dict[str, int] = {}
    for block in blocks:
        key = next(iter(block)) if isinstance(block, dict) and block else "?"
        kinds[key] = kinds.get(key, 0) + 1
    progress(f"转换：Markdown {len(md)} 字符 → {len(blocks)} 个块（"
             + " / ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]))
             + "）")

    uploaded = 0
    pictured = 0
    if images == "off":
        if picture_refs(blocks):
            sys.exit("[otl_write] 文档里有图片块，但 --images off：空 id 的图片块会被服务端拒掉，"
                     "改用 --images auto")
    else:
        if len(picture_refs(blocks)) != len(plan):
            sys.exit("[otl_write] 图片块与 Markdown 图片数量不一致，已停止；未删除正文")
        # Validate every local image before uploading the first attachment.
        for item in plan:
            if item["path"] is not None and not item["local"]:
                sys.exit(f"[otl_write] 本地图片不存在：{item['path']}")
            if item["local"]:
                item["sha256"] = file_digest(item["path"])
                item["width"], item["height"] = image_size(item["path"])
        if plan:
            for item in plan:
                if not item["local"]:
                    continue
                if dry_run:
                    item["id"] = "dry-run"
                    continue
                item["id"] = upload_attachment(file_id, item["path"], timeout, item["sha256"])
                uploaded += 1
                progress(f"  上传图片 {os.path.basename(item['path'])} "
                         f"{item['width']}x{item['height']} → {item['id']}")
            progress(f"图片：{len(plan)} 张（本地上传 {uploaded} 张）")
        fill_pictures(blocks, plan, img_pct, doc_width, progress)
        pictured = len(plan)

    blocks, split = split_wide_tables(blocks)
    if split:
        progress(f"超长表格：按 {MAX_TABLE_ROWS} 行上限拆出 {split} 张（重复表头）")

    reflowed = reflow_tables(blocks, table_width) if reflow else 0
    if reflowed:
        progress(f"表格宽度：{reflowed} 张超宽表按内容切行，列宽之和回到 {table_width}px")

    fitted, crowded = fit_table_widths(blocks, table_width)
    if fitted:
        progress(f"表格宽度：{fitted} 张缩到 {table_width}px 以内（与正文同宽）")
    wide = overflowing_tables(blocks, table_width)
    if wide:
        widest = max(wide, key=lambda item: item[2])
        progress(f"表格宽度：{len(wide)} 张超过 {table_width}px（每列 {TABLE_MIN_COL}px 是服务端写入下限，"
                 f"最宽 {widest[2]}px / {widest[1]} 列）；已保持原样，需在 WPS 里手动拖窄")

    return {"file_id": file_id, "children": blocks, "dry_run": dry_run,
            "blocks": len(blocks), "written": 0, "images": pictured,
            "uploaded": uploaded, "split_tables": split, "table_reflowed": reflowed,
            "table_fitted": fitted, "table_crowded": crowded, "table_manual": len(wide),
            "table_width": table_width}


def body_fingerprint(children: list) -> str:
    return hashlib.sha256(json.dumps(children, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def content_signature(block):
    """Compare visible content across the conversion and persisted block schemas."""
    if isinstance(block, list):
        result = []
        for child in block:
            item = content_signature(child)
            if item == "" or item == ():
                continue
            if isinstance(item, str) and result and isinstance(result[-1], str):
                result[-1] += item
            else:
                result.append(item)
        return tuple(result)
    if not isinstance(block, dict):
        return ()
    if "text" in block and isinstance(block["text"], dict):
        return (block["text"].get("attributes") or {}).get("content", "")
    parts = []
    for key, value in sorted(block.items()):
        if key in {"block_id", "attributes", "range_marks"}:
            continue
        if key in {"id", "content", "url"} and isinstance(value, str):
            parts.append((key, value))
        elif isinstance(value, (dict, list)):
            item = content_signature(value)
            if item or (isinstance(value, dict) and key not in {"attributes", "range_marks"}):
                parts.append((key, item))
    return tuple(parts)


def write_prepared(file_id: str, prepared: dict, *, index: int = 0,
                   timeout: int = 180, chunk: int = 200, progress=print) -> dict:
    if prepared["dry_run"] or prepared["file_id"] != file_id:
        raise ValueError("Cannot commit a preview or preparation for another document")
    blocks = prepared["children"]
    current = doc_children(file_id, timeout)
    before = len(current)
    known = {_block_id(block) for block in current}
    if not 0 <= index <= before:
        sys.exit("[otl_write] 插入位置已失效，已停止写入")

    step = max(1, min(chunk, MAX_BLOCKS_PER_CALL))
    written = 0
    for start in range(0, len(blocks), step):
        piece = blocks[start:start + step]
        offset = index + written
        try:
            api("post", f"/v7/airpage/v2/{_wps.segment(file_id)}/blocks/create",
                {"block_id": "doc", "index": offset,
                 "doc_children": {"children": piece}}, timeout)
        except _wps.WpsError as exc:
            if not exc.uncertain:
                raise
            progress("写入响应异常，正在读取云端块确认结果")
        try:
            current = doc_children(file_id, timeout)
        except _wps.WpsError as exc:
            sys.exit(f"[otl_write] 已写入 {written}/{len(blocks)} 个块，随后读取块树失败：{exc}\n"
                     f"请用 --count-blocks 确认云端块数，再决定重写还是继续")
        saved = current[offset:offset + len(piece)]
        if (content_signature(saved) != content_signature(piece)
                or any(not _block_id(block) or _block_id(block) in known for block in saved)):
            sys.exit("[otl_write] 新增块的内容或位置与预期不一致，请回查云端内容后再操作")
        known.update(_block_id(block) for block in saved)
        written += len(piece)
        progress(f"已读回校验 {written}/{len(blocks)} 个块")
    return {key: value for key, value in prepared.items() if key not in ("children", "dry_run")} | {
        "written": written, "final_blocks": len(current)}


def _insert_blocks(file_id: str, index: int, blocks: list, timeout: int, chunk: int,
                   progress=print, verify: bool = True) -> int:
    """在 index 处插入若干块，默认逐批读回校验，返回插入个数。"""
    step = max(1, min(chunk, MAX_BLOCKS_PER_CALL))
    written = 0
    for start in range(0, len(blocks), step):
        piece = blocks[start:start + step]
        offset = index + written
        try:
            api("post", f"/v7/airpage/v2/{_wps.segment(file_id)}/blocks/create",
                {"block_id": "doc", "index": offset, "doc_children": {"children": piece}}, timeout)
        except _wps.WpsError as exc:
            if not exc.uncertain:
                raise
            progress("写入响应异常，正在读取云端块确认结果")
        written += len(piece)
        if not verify:
            continue
        saved = doc_children(file_id, timeout)[offset:offset + len(piece)]
        if [content_signature(block) for block in saved] != [content_signature(block) for block in piece]:
            sys.exit("[otl_write] 新增块的内容或位置与预期不一致，请回查云端内容后再操作")
        progress(f"已写入并读回 {written}/{len(blocks)} 个块")
    if not verify and len(blocks) > step:
        progress(f"已写入 {written} 个块")
    return written


def replace_blocks_diff(file_id: str, current: list, prepared: dict, *,
                        timeout: int = 180, chunk: int = 200, progress=print) -> dict:
    """按块差异替换正文，内容没变的块原地保留。

    整篇重写会把每张表重新创建一次，服务端按 116px/列 重算列宽，用户手动拖窄的列宽
    就白调了。这里用块内容签名做 diff，只删除、插入真正变化的区间；未变的表格（含手动
    列宽）原样留在云端。
    """
    new = prepared["children"]
    old_keys = [content_signature(block) for block in current]
    new_keys = [content_signature(block) for block in new]
    ops = [op for op in difflib.SequenceMatcher(None, old_keys, new_keys,
                                                autojunk=False).get_opcodes()
           if op[0] != "equal"]
    stats = {key: value for key, value in prepared.items() if key not in ("children", "dry_run")}
    if not ops:
        progress("正文与云端一致，未写入任何块（表格列宽保持不变）")
        return {**stats, "written": 0, "deleted": 0, "final_blocks": len(current)}

    inserted = deleted = 0
    # 大文档逐段回读太慢，只在结尾整篇核对；小文档边写边读
    verify_each = len(current) <= 1500
    # 从后往前处理：后面的插入/删除不会挪动前面待处理区间的索引
    for tag, i1, i2, j1, j2 in reversed(ops):
        piece = new[j1:j2]
        if piece:
            inserted += _insert_blocks(file_id, i1, piece, timeout, chunk, progress,
                                       verify=verify_each)
        if i2 > i1:
            start = i1 + len(piece)
            api("post", f"/v7/airpage/v2/{_wps.segment(file_id)}/blocks/delete",
                {"block_id": "doc", "start_index": start, "end_index": start + (i2 - i1)}, timeout)
            deleted += i2 - i1
            progress(f"已删除旧块 {deleted} 个（区间 [{start}, {start + (i2 - i1)})）")

    try:
        saved = doc_children(file_id, timeout)
    except _wps.WpsError as exc:
        sys.exit(f"[otl_write] 已写入 {inserted} 个块，但回读失败：{exc}\n"
                 "请用 --count-blocks 确认云端块数后再决定是否重跑")
    if [content_signature(block) for block in saved] != new_keys:
        sys.exit("[otl_write] 增量替换后回读与预期不一致，请检查云端正文与备份")
    return {**stats, "written": inserted, "deleted": deleted, "final_blocks": len(saved)}


def commit_document(file_id: str, prepared: dict, *, mode: str,
                    baseline: list, timeout: int = 180, chunk: int = 200,
                    progress=print) -> dict:
    if prepared["dry_run"] or prepared["file_id"] != file_id:
        raise ValueError("Cannot commit a preview or preparation for another document")
    current = doc_children(file_id, timeout)
    if body_fingerprint(current) != body_fingerprint(baseline):
        sys.exit("[otl_write] 准备期间正文发生变化，已放弃写入；请重新读取并合并改动")
    backup_blocks(file_id, current)
    if mode == "replace":
        return replace_blocks_diff(file_id, current, prepared, timeout=timeout,
                                   chunk=chunk, progress=progress)
    index = 0 if mode == "prepend" else len(current)
    if mode == "append" and current and is_blank_paragraph(current[-1]):
        index -= 1
    return write_prepared(file_id, prepared, index=index, timeout=timeout,
                          chunk=chunk, progress=progress)


def report_table_widths(children: list, limit: int, progress=print) -> int:
    """统计仍超出正文宽度的表格并报告，返回张数。

    列宽最终由服务端按单元格内容兜底（116px 起，字越多越宽），写入时的 grid 只是
    期望值，文字多的表会被撑开，所以以回读结果为准。
    """
    if limit <= 0:
        return 0
    over = overflowing_tables(children, limit)
    if over:
        index, columns, width = max(over, key=lambda item: item[2])
        progress(f"表格宽度：{len(over)} 张超过 {limit}px，最宽 {width}px（第 {index} 块，{columns} 列）")
    return len(over)


def update_block(file_id, block_id, prepared, snap_path, timeout, force):
    children = doc_children(file_id, timeout)
    current = next((block for block in children if _block_id(block) == block_id), None)
    if current is None:
        raise ValueError("目标块已不存在，请重新读取文档")
    kind = next(iter(current))
    replacement = prepared["children"]
    if (kind not in {"paragraph", "heading"} or len(replacement) != 1
            or kind not in replacement[0] or not isinstance(replacement[0][kind].get("elements"), list)):
        raise ValueError("局部更新只接受与目标同类型的单个段落或标题块")
    elements = replacement[0][kind]["elements"]
    backup_blocks(file_id, [current])
    verify_snapshot(file_id, snap_path, "update", timeout, False, force, block_id)
    try:
        api("post", f"/v7/airpage/v2/{_wps.segment(file_id)}/blocks/update",
            {"block_id": block_id, "update_elements": {f"{kind}_elements": {"elements": elements}}}, timeout)
    except _wps.WpsError as exc:
        if not exc.uncertain:
            raise
        print("写入响应异常，正在读取目标块确认结果")
    saved = next((block for block in doc_children(file_id, timeout) if _block_id(block) == block_id), {})
    saved_elements = (saved.get(kind) or {}).get("elements")
    if saved_elements is None or content_signature(saved_elements) != content_signature(elements):
        raise ValueError("目标块读回与预期不一致，请先检查云端结果")
    print(f"已读回校验目标块 {block_id}")


def main() -> None:
    ap = argparse.ArgumentParser(description="把 Markdown 写入 WPS 365 智能文档")
    ap.add_argument("--file-id", required=True, help="智能文档 file_id")
    ap.add_argument("--md", help="Markdown 文件路径")
    ap.add_argument("--text", help="直接给 Markdown 文本（与 --md 二选一）")
    ap.add_argument("--mode", choices=["append", "replace", "prepend"], default="append",
                    help="append 追加到末尾（默认）/ replace 清空后写入 / prepend 插到最前")
    ap.add_argument("--images", choices=["auto", "off"], default="auto",
                    help="auto：本地图片上传成附件并回填 id（默认）；off：不处理图片")
    ap.add_argument("--img-base", help="图片相对路径的基准目录（默认取 --md 所在目录）")
    ap.add_argument("--img-width", type=int, default=DEFAULT_IMG_PCT,
                    help=f"图片渲染宽度占正文宽度的百分比（默认 {DEFAULT_IMG_PCT}；0 表示用原始宽度）")
    ap.add_argument("--doc-width", type=int, default=DOC_WIDTH,
                    help=f"正文宽度像素（默认 {DOC_WIDTH}）")
    ap.add_argument("--table-width", type=int, default=None,
                    help="表格最大总宽像素（默认与 --doc-width 相同；0 不限制）")
    ap.add_argument("--no-reflow", action="store_true",
                    help="超宽表不切行，只按列宽压缩（切行会把长单元格拆成多段）")
    ap.add_argument("--chunk", type=int, default=200, help="每批写入的块数（默认 200）")
    ap.add_argument("--title", default="auto",
                    help="文档标题：auto 取自 front matter 的 title 或 md 文件名（默认）/ none 不写 / 其他值按原文写入")
    ap.add_argument("--retitle", action="store_true", help="标题已有内容也覆盖（默认只在标题为空时写）")
    ap.add_argument("--front-matter", choices=["drop", "keep"], default="drop",
                    help="文首 YAML front matter：drop 丢弃（默认）/ keep 原样交给转换器")
    ap.add_argument("--timeout", type=int, default=180, help="单次 HTTP 超时秒数")
    ap.add_argument("--dry-run", action="store_true", help="只转换不写入，打印块结构摘要")
    ap.add_argument("--snapshot-out", metavar="PATH",
                    help="只保存当前文档的块级快照后退出（配合 --expect-snapshot 做并发检查）")
    ap.add_argument("--scope-blocks", metavar="ID,ID",
                    help="声明本次改动依赖/会覆盖的块（block_id，逗号分隔）—— 只校验这些块")
    ap.add_argument("--scope-index", metavar="N,N",
                    help="同上，但按一级块索引声明（更省事，见 --list-blocks）")
    ap.add_argument("--list-blocks", action="store_true",
                    help="列出根下所有一级块的索引与 block_id 后退出")
    ap.add_argument("--update-block", help="局部修改一个一级段落或标题块，传 block_id")
    ap.add_argument("--expect-snapshot", metavar="PATH",
                    help="写入前比对快照：依赖的块被他人改过则放弃写入并提示冲突")
    ap.add_argument("--allow-added", action="store_true",
                    help="仅「新增块」不算冲突（未声明 scope 时的宽松档）")
    ap.add_argument("--force", action="store_true", help="忽略冲突检查，强制写入")
    ap.add_argument("--count-blocks", action="store_true",
                    help="只统计当前一级块数量与类型分布后退出（导出接口有缓存，用它核对写入结果）")
    ap.add_argument("--delete-range", metavar="START:END",
                    help="删除一级块区间 [START, END) 后退出；用于清理 replace 中断后残留的原块")
    args = ap.parse_args()

    if args.count_blocks:
        describe_children(doc_children(args.file_id, args.timeout))
        return

    if args.delete_range:
        try:
            start, end = (int(part) for part in args.delete_range.split(":", 1))
        except ValueError:
            ap.error("--delete-range 需要 START:END，例如 1710:3417")
        delete_range(args.file_id, start, end, args.timeout)
        return

    if args.list_blocks:
        list_blocks(args.file_id, args.timeout)
        return

    if args.snapshot_out:
        scope_ids = [s.strip() for s in (args.scope_blocks or "").split(",") if s.strip()]
        scope_idx = [int(s) for s in (args.scope_index or "").split(",") if s.strip()]
        snap = snapshot(args.file_id, args.timeout,
                        scope_ids or None, scope_idx or None)
        with open(args.snapshot_out, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=1)
        note = (f"声明依赖 {len(snap['scope'])} 个块" if snap["scope_explicit"]
                else "未声明依赖块（将保守按全文比对）")
        print(f"已保存快照：{args.snapshot_out}"
              f"（{len(snap['blocks'])} 个一级块，version {snap.get('version')}；{note}）")
        return

    if bool(args.md) == bool(args.text):
        ap.error("--md 或 --text 二选一")
    if not args.dry_run and not args.expect_snapshot:
        ap.error("正文写入需要 --expect-snapshot")
    if args.update_block and (args.mode != "append" or args.retitle):
        ap.error("局部更新与正文插入、替换或改文档标题应分开执行")
    if args.expect_snapshot:
        verify_snapshot(args.file_id, args.expect_snapshot, args.mode, args.timeout,
                        args.allow_added, args.force, args.update_block)
    base = os.getcwd()
    if args.md:
        with open(args.md, encoding="utf-8") as fh:
            md = fh.read()
        base = os.path.dirname(os.path.abspath(args.md))
    else:
        md = args.text
    if args.img_base:
        base = os.path.abspath(args.img_base)
    meta: dict = strip_front_matter(md)[1]
    if args.front_matter == "drop":
        md = strip_front_matter(md)[0]
        if meta:
            print(f"已摘掉 front matter（{len(meta)} 个字段）")

    title = resolve_title(args.title, meta.get("title"), args.md)
    expected_title = None
    if args.retitle and title and not args.dry_run:
        with open(args.expect_snapshot, encoding="utf-8") as handle:
            expected_title = json.load(handle).get("title")
        if not isinstance(expected_title, str) or doc_title(args.file_id, args.timeout) != expected_title:
            raise ValueError("标题快照已失效，请重新读取后再改标题")

    baseline = doc_children(args.file_id, args.timeout)
    table_width = args.doc_width if args.table_width is None else args.table_width
    prepared = prepare_markdown(
        args.file_id, md, base=base, timeout=args.timeout,
        dry_run=args.dry_run, images="off" if args.update_block else args.images, img_pct=args.img_width,
        doc_width=args.doc_width, table_width=table_width, reflow=not args.no_reflow)
    if args.dry_run:
        print("dry-run：未修改正文或标题，未上传图片")
        return

    if args.update_block:
        update_block(args.file_id, args.update_block, prepared, args.expect_snapshot, args.timeout, args.force)
        return

    # Recheck after potentially slow conversion and attachment uploads.
    if args.expect_snapshot:
        verify_snapshot(args.file_id, args.expect_snapshot, args.mode,
                        args.timeout, args.allow_added, args.force)
        baseline = doc_children(args.file_id, args.timeout)
    stats = commit_document(args.file_id, prepared, mode=args.mode,
                            baseline=baseline, timeout=args.timeout, chunk=args.chunk)
    if args.mode == "replace":
        print(f"完成：replace 写入 {stats['written']} 个块、删除 {stats.get('deleted', 0)} 个旧块"
              f"到 {args.file_id}（图片 {stats['images']} 张，拆表 {stats['split_tables']} 张）")
    else:
        print(f"完成：{args.mode} {stats['written']} 个块到 {args.file_id}"
              f"（图片 {stats['images']} 张，拆表 {stats['split_tables']} 张）")

    over = 0
    if table_width > 0:
        try:
            over = report_table_widths(doc_children(args.file_id, args.timeout), table_width)
        except _wps.WpsError as exc:
            print(f"表格宽度：回读失败，未能核对实际宽度（{exc}）")
    if over:
        print(f"⚠️ 有 {over} 张表超过正文宽度 {table_width}px，接口无法再压：写入时每列最少 "
              f"{TABLE_MIN_COL}px，列数一多就超出版心。请在 WPS 里手动拖窄列宽；"
              "重跑同步时内容未变的表格不会被重写，手动调的列宽会保留。")

    action = ensure_title(args.file_id, title, args.timeout, overwrite=args.retitle, expected=expected_title)
    if action == "set":
        print(f"标题：已写入「{title}」")
    elif action == "kept":
        print(f"标题：文档已有标题，保留不动（要覆盖加 --retitle）")
    elif title:
        print(f"标题：已是「{title}」")


if __name__ == "__main__":
    _wps.run(main)
