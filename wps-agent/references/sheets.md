# 在线表格

传统 `.xlsx` 使用 `--kind sheets`，智能表格 `.ksheet` 使用 `--kind airsheet`。用 `worksheets` 获取工作表 ID，再通过 A1 区域选择数据。

```sh
python3 scripts/sheets.py worksheets --kind airsheet --file-id FILE_ID
python3 scripts/sheets.py get --kind airsheet --file-id FILE_ID --worksheet-id SHEET_ID --range A1:B3
python3 scripts/sheets.py snapshot --kind airsheet --file-id FILE_ID --worksheet-id SHEET_ID \
  --range A1:B3 --snapshot /tmp/area.json
python3 scripts/sheets.py set --kind airsheet --file-id FILE_ID --worksheet-id SHEET_ID \
  --range A1:B3 --snapshot /tmp/area.json --values /tmp/cells.json
```

`cells.json` 以单元格地址为键，值为输入文本，例如 `{"A2":"新备注","B3":"=SUM(B1:B2)"}`。每次选择最多 1024 个单元格，小范围编辑只写指定地址。脚本在写入前用快照比对目标单元格，并在写入后读回核对。

读回公式时核对原文。WPS 区域接口只提供计算结果时，脚本重新请求原件下载地址，从对应工作表核对公式。原件同步可能晚于单元格更新。遇到这类延迟，把 `set` 改成 `verify` 后传入相同的文件、工作表、区域和 `--values`，即可继续读回，无需再次写入。缺少下载权限时可检查在线界面的公式栏。可传 `--drive-id` 明确指定驱动盘。 [WPS 表格 API 概述](https://open.wps.cn/documents/app-integration-dev/wps365/server/introduce)
