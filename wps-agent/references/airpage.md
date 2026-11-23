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

## 大文档与中断恢复

数千块的标准、手册类文档可以直接整篇替换，脚本会分批写入并逐批读回校验；大正文响应由网关压缩，传输层已自动解压，读取失败会按幂等重试并延长超时。

替换分两步：先插入新正文，核对无误后删除原来的连续块区间。删除前读回失败时，脚本会打印残留区间并给出恢复命令，此时文档里同时存在新旧两份正文：

```sh
python3 scripts/otl_write.py --file-id FILE_ID --count-blocks
python3 scripts/otl_write.py --file-id FILE_ID --delete-range 1710:3417
```

`--count-blocks` 打印一级块总数、类型分布和首末块，用于判断是否还有重复副本；`--delete-range START:END` 按一级块索引区间删除，索引从 0 开始。确认残留区间时按块内容对齐——新副本以文首标记块（如来源说明）开始，原副本紧随其后，区间终点为插入块数加原块数。

上传本地图片需要带 Pillow 的 Python 解释器。把图片放在 Markdown 文件所在目录或 `--img-base` 指定的目录内。脚本将返回的附件 ID 填入图片块。超长表按服务端 100 行限制拆表。后续读取时从块树校验顺序和正文，转码后的图片外观需单独检查。
