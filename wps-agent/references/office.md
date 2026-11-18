# Office 文件

先确认是普通 `.docx`、`.xlsx` 或 `.pptx` 文件。在线表格要修改单元格时，使用[在线表格接口](sheets.md)。下载和上传示例在 skill 目录运行。

```sh
python3 scripts/file_fetch.py --file-id FILE_ID --drive-id DRIVE_ID --out /tmp/wps-office --original-only
python3 scripts/file_write.py --file-id FILE_ID --drive-id DRIVE_ID --file edited.docx \
  --expect-hash HASH --expect-version VERSION --backup-dir /tmp/wps-office-backup
```

`file_fetch.py` 也接受 `--link`。`meta.json` 记录云端 `hash`、`version`、文件 ID 和所在目录。下载原件后用适合该文件格式的工具编辑副本。读取 `.docx` 正文可用 `document.py read`，默认的 `.xlsx` 本地解析需要 `openpyxl`。`--original-only` 只下载字节和元信息，可直接用于本地 Office 工具。编辑或呈现文件时，根据用户要求校验版式。

上传替换原文件时，脚本下载并保留云端原版本。文件上传分为申请地址、传输字节、提交。提交前再次校验同一文件的哈希和版本，完成后核对文件 ID、版本和下载字节。`--dry-run` 检查基线，跳过上传。若创建请求响应丢失，先在目标目录按名称核对，再决定下一步。

新文件用 `--drive-id DRIVE_ID --parent-id PARENT_ID --file FILE`，默认同名冲突时停止。用户明确接受生成副本时可用 `--on-name-conflict rename`。可选的 `--md` 依赖 `python-docx`，只适用于基本的 Markdown 转 Word。 [WPS 文件上传流程](https://open.wps.cn/documents/app-integration-dev/wps365/server/yundoc/file-transfer/get-upload-file)
