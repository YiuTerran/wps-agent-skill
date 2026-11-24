# 智能文档

先用 `document.py inspect` 解析分享链接。以下示例在 skill 目录运行，把大写占位符替换为实际值。

```sh
python3 scripts/otl_fetch.py --file-id FILE_ID --out /tmp/wps-otl
python3 scripts/otl_write.py --file-id FILE_ID --list-blocks
python3 scripts/otl_write.py --file-id FILE_ID --scope-blocks BLOCK_ID --snapshot-out /tmp/blocks.json
python3 scripts/otl_write.py --file-id FILE_ID --update-block BLOCK_ID --md change.md --expect-snapshot /tmp/blocks.json
```

`otl_fetch.py` 写出 `doc.md`、解码后的 `otl.model.json` 和附件清单。按需读取正文和图片。`--no-images` 可跳过下载。`doc.md` 是从云端块还原的 Markdown。需要了解原始块字段时查看模型 JSON。附件下载地址是临时凭证，避免将其写进回复。

`--update-block` 更新一个一级段落或标题块。`change.md` 需要转换为与目标同类型的一个块。只修改一句话时，先读出完整目标块，把原有未改文字和格式保留在提交内容中。快照需涵盖本次修改依赖的块。

## 追加、前置和全文替换

```sh
python3 scripts/otl_write.py --file-id FILE_ID --snapshot-out /tmp/blocks.json
python3 scripts/otl_write.py --file-id FILE_ID --md content.md --mode append --title none --expect-snapshot /tmp/blocks.json
```

`--mode prepend` 插在开头。仅在用户要求改整篇时选 `--mode replace`。替换会比对完整块树并备份，先插入并核对新正文，再清理精确匹配的原块。如果后半步失败，先回读新旧块和本地备份再处理。`--dry-run` 请求 Markdown 转换并预演结构，跳过云端正文、标题和附件写入。

## 标题里的条款编号

`## 4.1 概述` 这类以「数字.数字」开头的标题，转换时命中自动编号规则：编号从文本里消失，标题被 WPS 重新编号成 1、2、3…，正文里「按 4.1 执行」的引用就对不上了。只有标题会这样，正文段落、列表项、引文里的 `4.1` 都不受影响。

绕不过去的写法，不要再试：加粗、行内代码、去掉编号后的空格、反斜杠转义、HTML 实体（`&period;`、`&#46;`）、`<h2>` 标签。判定发生在这些标记被还原之后，全部照样被吃。

`otl_write.py` 的做法是请求转换前在编号前插一个不可见占位符（U+2063），转换后从标题元素里抹掉。写入的标题就是原文的 `4.1 概述`，保留编号且不带 `ordered` 属性。还原数与掩码数不一致时脚本直接中止，不会写入半成品。相关常量 `HEADING_NUMBER_RE`、`HEADING_MASK` 和函数 `mask_heading_numbers`、`unmask_headings` 都在 `scripts/otl_write.py`。

源 Markdown 保持 `## 4.1 概述` 原样即可。不要为了过转换把点改成全角点、加粗或加括号，那会改动原文编号。

验证：`--dry-run` 只请求转换，回读的标题文本应带原编号；线上用 `--count-blocks` 和块树核对，`heading` 里不应有 `ordered`。

## 大文档与中断恢复

数千块的标准、手册类文档可以直接整篇替换，脚本会分批写入并逐批读回校验；大正文响应由网关压缩，传输层已自动解压，读取失败会按幂等重试并延长超时。

替换分两步：先插入新正文，核对无误后删除原来的连续块区间。删除前读回失败时，脚本会打印残留区间并给出恢复命令，此时文档里同时存在新旧两份正文：

```sh
python3 scripts/otl_write.py --file-id FILE_ID --count-blocks
python3 scripts/otl_write.py --file-id FILE_ID --delete-range 1710:3417
```

`--count-blocks` 打印一级块总数、类型分布和首末块，用于判断是否还有重复副本；`--delete-range START:END` 按一级块索引区间删除，索引从 0 开始。确认残留区间时按块内容对齐——新副本以文首标记块（如来源说明）开始，原副本紧随其后，区间终点为插入块数加原块数。

## 表格宽度

正文版心 740px，超过这个宽度表格会顶到页边。列宽最终由服务端按单元格内容兜底，写入时给的 `grid` 只是期望值。实测的列宽下限：1~5 个汉字一律 116px，之后每多一个字加 16px（9 字 180px、13 字 244px）。所以能不能压进版心取决于内容，不是列数：

| 表格 | 内容下限合计 | 结果 |
| --- | --- | --- |
| 6 列，每列 4 位代码 | 6×116 = 696 | 压到 740，正常 |
| 8 列，每列 1~2 字 | 8×116 = 928 | 溢出，压不动 |
| 5 列，其中两列 9 个汉字 | 116+180+116+164+180 = 756 | 溢出 16px |
| 20 列 | 2320 | 必须拆表 |

Markdown 表格和 HTML 表格走同一条路：都经 `blocks/convert` 变成同一套 table 块，压宽在转换之后统一做，合并单元格按 colspan 把内容下限均摊到跨过的几列。

`--table-width` 指定期望总宽（默认 740，0 不处理）。脚本先按单元格文字估出每列下限，下限合计超出时直接报「放不进」，不再做无效的压缩；没超时按比例压到总宽并写入 `table.attributes.width`，同时保证任何一列不低于自己的内容下限。写完回读核对，报出仍然超宽的块索引、列数和实际宽度。

```sh
python3 scripts/otl_write.py --file-id FILE_ID --md content.md --mode replace \
  --expect-snapshot /tmp/blocks.json --table-width 740
```

生成表格时按「能不能放进 740px」设计，而不是死守列数：字段清单用「字段标识符｜类型｜说明」三列，说明控制在 13 个字内；短代码列可以多放几列，长文字列要少。列多又都带长说明时拆成两张并重复主键列，比压列宽可靠。

手写 HTML 表格必须带 `<thead>` 或 `<tbody>`：实测 `<table><tr><td>` 会被 convert 整段丢掉，正文里的表格凭空消失且没有报错。脚本在转换前检查，缺表头表体会直接停止。

上传本地图片需要带 Pillow 的 Python 解释器。把图片放在 Markdown 文件所在目录或 `--img-base` 指定的目录内。脚本将返回的附件 ID 填入图片块。超长表按服务端 100 行限制拆表。后续读取时从块树校验顺序和正文，转码后的图片外观需单独检查。
