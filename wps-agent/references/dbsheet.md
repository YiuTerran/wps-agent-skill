# 多维表格

`.dbt` 按数据表和记录读取。先列结构，区分 `file_id`、数据表 `sheet_id` 和记录 ID。共享链接可直接传给 `dbsheet.py --link` 所在的子命令。

```sh
python3 scripts/dbsheet.py schema --file-id FILE_ID --out /tmp/schema.json
python3 scripts/dbsheet.py list --file-id FILE_ID --sheet SHEET --max-records 50
python3 scripts/dbsheet.py snapshot --file-id FILE_ID --sheet SHEET --record RECORD_ID --out /tmp/record.json
python3 scripts/dbsheet.py update --file-id FILE_ID --sheet SHEET --record RECORD_ID \
  --set '字段名=新值' --expect-snapshot /tmp/record.json
```

`list` 支持视图、字段、过滤器和分页。需要保留字段原始值时用 `--raw`。表单视图请定位底层数据表，文档型工作表没有可列出的记录。`attachments` 从原始记录定位附件，可用 `--images-only` 或 `--list-only`。

`create` 支持 `--set`、`--set-json`、`--records-file` 及单条创建前查重。超时后先列出可能已经创建的记录。`update` 只提交指定字段，`delete` 比对整条记录后执行。更新和删除都要求带对应快照，结构发生变化时重新取快照。脚本在请求后读回记录。日期使用 `YYYY-MM-DD` 或 `YYYY/MM/DD`。关联字段写入使用记录 ID，筛选使用显示名。选择字段的选项应以读取的结构为准。 [WPS 多维表格更新记录接口](https://open.wps.cn/documents/app-integration-dev/wps365/server/dbsheet/records/update-record)
