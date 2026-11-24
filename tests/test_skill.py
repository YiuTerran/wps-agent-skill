import contextlib
import gzip
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wps-agent" / "scripts"))

import _wps
import dbsheet
import document
import file_write
import otl_write
import sheets


class Response(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode())
        self.status = status
        self.headers = Message()


def auth_response():
    return Response({"result": "ok", "data": {"access_token": "test-token", "expires_in": 60}})


def api_response(data=None):
    return Response({"code": 0, "data": data or {}})


def http_error(status, payload=None):
    return urllib.error.HTTPError("https://openapi.wps.cn", status, "error", Message(),
                                 io.BytesIO(json.dumps(payload).encode()))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.opener = Mock()
        self.client = _wps.Client(self.opener)

    def test_key_only_at_account_exchange_token_is_reused(self):
        self.opener.open.side_effect = [auth_response(), api_response(), api_response()]
        with (patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}),
              patch.object(_wps, "_shell_api_key") as shell_api_key,
              patch.object(_wps, "_config_path") as config_path):
            self.client.api("GET", "/v7/files/local/meta")
            self.client.api("GET", "/v7/files/local/meta", query={"name": "a&b"})
        shell_api_key.assert_not_called()
        config_path.assert_not_called()
        auth, first, second = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertEqual(auth.full_url, _wps.TOKEN_URL)
        self.assertEqual(json.loads(auth.data), {"grant_type": "api_key", "api_key": "test-key"})
        self.assertEqual(first.get_header("Authorization"), "Bearer test-token")
        self.assertNotIn("test-key", str(first.header_items()))
        self.assertTrue(second.full_url.endswith("?name=a%26b"))

    def test_missing_key_makes_no_request(self):
        with (tempfile.TemporaryDirectory() as directory,
              patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}, clear=True),
              patch.object(Path, "home", return_value=Path(directory)),
              self.assertRaisesRegex(_wps.WpsError, "AGENT_API_KEY")):
            self.client.api("GET", "/v7/files/local/meta")
        self.opener.open.assert_not_called()

    def test_blank_environment_key_falls_back_to_default_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / ".config" / "wps-agent" / "config.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"api_key":"file-key"}', encoding="utf-8")
            self.opener.open.side_effect = [auth_response(), api_response()]
            with (patch.dict(os.environ, {"AGENT_API_KEY": " "}, clear=True),
                  patch.object(Path, "home", return_value=home)):
                self.client.api("GET", "/v7/files/local/meta")
        auth = self.opener.open.call_args_list[0].args[0]
        self.assertEqual(json.loads(auth.data)["api_key"], "file-key")

    @unittest.skipUnless(os.name == "posix", "POSIX shell startup files")
    def test_shell_literal_precedes_json_and_respects_shell_preference(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".zshrc").write_text(
                "export AGENT_API_KEY=old-key\n# AGENT_API_KEY=comment\n"
                "export AGENT_API_KEY='zsh-key'  # WPS\n", encoding="utf-8",
            )
            (home / ".bashrc").write_text('export AGENT_API_KEY="bash-key"\n', encoding="utf-8")
            with (patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=home),
                  patch.object(_wps, "_config_path") as config_path):
                for shell, expected in (("/bin/zsh", "zsh-key"), ("/bin/bash", "bash-key")):
                    with self.subTest(shell=shell), patch.dict(os.environ, {"SHELL": shell}):
                        self.assertEqual(_wps._api_key(), expected)
            config_path.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX shell startup files")
    def test_dynamic_shell_assignment_leaves_json_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            marker = home / "command-ran"
            (home / ".zshrc").write_text(
                f"export AGENT_API_KEY=old-key\nexport AGENT_API_KEY=$(touch '{marker}')\n",
                encoding="utf-8",
            )
            (home / ".profile").write_text('export AGENT_API_KEY="${UNRESOLVED}"\n', encoding="utf-8")
            config = home / ".config" / "wps-agent" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"api_key":"json-key"}', encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=home):
                self.assertEqual(_wps._api_key(), "json-key")
            self.assertFalse(marker.exists())

    def test_invalid_config_stops_without_exposing_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "wps-agent", "config.json")
            path.parent.mkdir()
            with (patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}, clear=True),
                  patch.object(Path, "home", return_value=Path(directory))):
                for contents in ('{"api_key":"sensitive-data",}', '{"api_key":["sensitive-data"]}'):
                    with self.subTest(contents=contents):
                        path.write_text(contents, encoding="utf-8")
                        with self.assertRaisesRegex(_wps.WpsError, "config.json") as error:
                            self.client.api("GET", "/v7/files/local/meta")
                        self.assertNotIn("sensitive-data", str(error.exception))
        self.opener.open.assert_not_called()

    def test_key_exchange_rejection_drops_response_text(self):
        self.opener.open.return_value = Response({"result": "failed", "msg": "test-key"})
        with patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}), self.assertRaises(_wps.WpsError) as error:
            self.client.api("GET", "/v7/files/local/meta")
        self.assertNotIn("test-key", str(error.exception))

    def test_401_renews_reads_but_never_repeats_writes(self):
        read_response = [auth_response(), http_error(401), auth_response(), api_response({"ok": True})]
        with patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}):
            self.opener.open.side_effect = read_response
            self.assertEqual(self.client.api("GET", "/v7/files/local/meta"), {"ok": True})
            self.assertEqual(self.opener.open.call_count, 4)
            self.opener.open.reset_mock()
            self.opener.open.side_effect = [http_error(401)]
            with self.assertRaisesRegex(_wps.WpsError, "401"):
                self.client.api("POST", "/v7/files/local/update", body={"a": 1})
        self.opener.open.assert_called_once()

    def test_file_transfer_only_sends_token_to_wps_drive_gateway(self):
        self.opener.open.side_effect = [auth_response(), api_response(), api_response()]
        with patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}):
            self.client.drive_transfer("https://storage.ag.wps.cn/blob", "GET", timeout=1).close()
            self.client.drive_transfer("https://storage.ag.wps.cn.example.org/blob", "GET", timeout=1,
                                       headers=[{"name": "X-Signed", "value": "signed"}]).close()
        _, wps, other = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertEqual(wps.get_header("Authorization"), "Bearer test-token")
        self.assertIsNone(other.get_header("Authorization"))
        self.assertEqual(other.get_header("X-signed"), "signed")

    def test_gzip_encoded_response_is_decoded(self):
        payload = {"code": 0, "data": {"blocks": []}}
        response = io.BytesIO(gzip.compress(json.dumps(payload).encode()))
        response.status = 200
        response.headers = Message()
        response.headers["Content-Encoding"] = "gzip"
        self.opener.open.side_effect = [auth_response(), response]
        with patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}):
            self.assertEqual(self.client.api("POST", "/v7/airpage/v2/doc/blocks",
                                             body={"block_id": "doc"}, read_only=True),
                             {"blocks": []})

    def test_truncated_gzip_body_still_reports_unparseable(self):
        response = io.BytesIO(b"\x1f\x8b\x08\x00broken")
        response.status = 200
        response.headers = Message()
        response.headers["Content-Encoding"] = "gzip"
        self.opener.open.side_effect = [auth_response(), response]
        with patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}):
            with self.assertRaisesRegex(_wps.WpsError, "响应无法解析"):
                self.client.api("GET", "/v7/airpage/v2/doc/blocks")

    def test_transient_read_failure_is_retried_but_write_is_not(self):
        with (patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}),
              patch.object(_wps.time, "sleep") as sleep):
            self.opener.open.side_effect = [auth_response(), urllib.error.URLError("reset"),
                                            api_response({"ok": True})]
            self.assertEqual(self.client.api("GET", "/v7/airpage/v2/doc/blocks"), {"ok": True})
            self.assertEqual(self.opener.open.call_count, 3)
            sleep.assert_called()
            self.opener.open.reset_mock()
            self.opener.open.side_effect = [urllib.error.URLError("reset")]
            with self.assertRaisesRegex(_wps.WpsError, "网络请求失败或超时"):
                self.client.api("POST", "/v7/airpage/v2/doc/blocks", body={"block_id": "doc"})
        self.opener.open.assert_called_once()

    def test_redirect_and_unapproved_hosts_are_not_followed(self):
        handler = _wps.NoRedirect()
        for status in (301, 302, 307, 308):
            self.assertIsNone(handler.redirect_request(None, None, status, "", {}, "https://other.example"))
        with self.assertRaises(ValueError):
            self.client.api("GET", "/v7/files/local/meta", origin="https://other.example")
        with self.assertRaises(_wps.WpsError):
            self.client.drive_transfer("http://storage.ag.wps.cn/blob", "GET", timeout=1)
        self.opener.open.assert_not_called()

    def test_content_fallback_does_not_mask_permission_errors(self):
        with patch.object(_wps, "client", self.client), patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}):
            self.opener.open.side_effect = [auth_response(), http_error(404), api_response({"plain": "正文"})]
            self.assertEqual(_wps.body_content("drive", "id", "plain"), {"plain": "正文"})
            self.assertTrue(self.opener.open.call_args.args[0].full_url.startswith(_wps.CONTENT_ORIGIN))
            self.opener.open.reset_mock()
            self.opener.open.side_effect = [http_error(403, {"code": 403000001})]
            with self.assertRaisesRegex(_wps.WpsError, "权限不足"):
                _wps.body_content("drive", "id", "plain")
            self.opener.open.assert_called_once()


class DocumentTests(unittest.TestCase):
    def test_links_and_routes(self):
        self.assertEqual(_wps.link_id("https://365.kdocs.cn/l/a123?key=anything#anchor"), "a123")
        self.assertEqual(_wps.link_id("a123"), "a123")
        with self.assertRaises(ValueError):
            _wps.link_id("https://www.kdocs.cn.example.org/l/a123")
        for extension, kind in (("otl", "airpage"), ("docx", "office"), ("ksheet", "airsheet"), ("dbt", "dbsheet")):
            with (patch.object(_wps, "resolve_link", return_value={"drive_id": "d", "file_id": "f"}),
                  patch.object(_wps, "file_meta", return_value={"name": f"example.{extension}"})):
                self.assertEqual(document.locate("a123", None, None, 5)["kind"], kind)

    def test_airpage_append_reads_content_after_uncertain_response(self):
        desired = {"paragraph": {"elements": [{"text": {"attributes": {"content": "added"}}}]}}
        actual = {"paragraph": {**desired["paragraph"], "block_id": "new", "range_marks": []}}
        prepared = {"file_id": "f", "children": [desired], "dry_run": False}
        with (patch.object(otl_write, "doc_children", side_effect=[[], [actual]]) as read,
              patch.object(otl_write, "api", side_effect=_wps.WpsError("写入", "超时", uncertain=True)) as write):
            result = otl_write.write_prepared("f", prepared, progress=lambda _: None)
        self.assertEqual(result["written"], 1)
        self.assertEqual(read.call_count, 2)
        write.assert_called_once()

    def test_airpage_append_refuses_mismatched_text(self):
        wanted = {"paragraph": {"elements": [{"text": {"attributes": {"content": "new"}}}]}}
        existing = {"paragraph": {"block_id": "old", "elements": [{"text": {"attributes": {"content": "old"}}}]}}
        prepared = {"file_id": "f", "children": [wanted], "dry_run": False}
        with (patch.object(otl_write, "doc_children", side_effect=[[], [existing]]),
              patch.object(otl_write, "api"), self.assertRaisesRegex(SystemExit, "不一致")):
            otl_write.write_prepared("f", prepared, progress=lambda _: None)

    def test_airpage_image_path_stays_in_selected_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "超出"):
                otl_write.image_plan("![外部文件](../secret.png)", directory)

    def test_airpage_replace_reports_leftover_range_when_readback_fails(self):
        old = {"paragraph": {"block_id": "old", "elements": [{"text": {"attributes": {"content": "旧"}}}]}}
        prepared = {"file_id": "f", "children": [], "dry_run": False, "written": 0}
        with (patch.object(otl_write, "doc_children",
                           side_effect=[[old], [old], _wps.WpsError("读取", "响应无法解析")]),
              patch.object(otl_write, "api", return_value={}),
              self.assertRaisesRegex(SystemExit, "--delete-range 0:1")):
            otl_write.commit_document("f", prepared, mode="replace", baseline=[old],
                                      progress=lambda _: None)

    def test_airpage_fits_wide_tables_to_body_width(self):
        wide = {"table": {"attributes": {"grid": [260, 320, 280, 240]},
                          "rows": [{"cells": []}]}}
        narrow = {"table": {"attributes": {"grid": [370, 370]}, "rows": [{"cells": []}]}}
        fitted, crowded = otl_write.fit_table_widths([wide, narrow], 740)
        self.assertEqual((fitted, crowded), (1, 0))
        attributes = wide["table"]["attributes"]
        self.assertEqual(sum(attributes["grid"]), 740)
        self.assertEqual(attributes["width"], 740)
        self.assertGreaterEqual(min(attributes["grid"]), otl_write.TABLE_MIN_COL)
        self.assertEqual(narrow["table"]["attributes"]["grid"], [370, 370])
        self.assertNotIn("width", narrow["table"]["attributes"])

    def test_airpage_reports_tables_too_crowded_to_shrink(self):
        def table(columns, text):
            cell = {"attributes": {"colspan": 1, "rowspan": 1},
                    "children": [{"paragraph": {"elements": [
                        {"text": {"attributes": {"content": text}}}]}}]}
            return {"table": {"attributes": {"grid": [150] * columns},
                              "rows": [{"cells": [cell for _ in range(columns)]}]}}

        # 8 列短内容已是 8×116=928，20 列同理；6 列写 8 个汉字是 6×180=1080
        for columns, text in ((8, "测"), (20, "测"), (6, "测站名称长度八个字")):
            block = table(columns, text)
            grid = list(block["table"]["attributes"]["grid"])
            self.assertEqual(otl_write.fit_table_widths([block], 740), (0, 1))
            self.assertEqual(block["table"]["attributes"]["grid"], grid)
            self.assertNotIn("width", block["table"]["attributes"])

    def test_airpage_fits_six_narrow_columns_to_body_width(self):
        cell = {"attributes": {"colspan": 1, "rowspan": 1},
                "children": [{"paragraph": {"elements": [
                    {"text": {"attributes": {"content": "0101"}}}]}}]}
        block = {"table": {"attributes": {"grid": [150] * 6},
                           "rows": [{"cells": [cell for _ in range(6)]}]}}
        self.assertEqual(otl_write.fit_table_widths([block], 740), (1, 0))
        attributes = block["table"]["attributes"]
        self.assertEqual(sum(attributes["grid"]), 740)
        self.assertGreaterEqual(min(attributes["grid"]), otl_write.TABLE_MIN_COL)

    def test_airpage_fits_merged_cell_tables(self):
        def cell(text, colspan=1, rowspan=1):
            return {"attributes": {"colspan": colspan, "rowspan": rowspan},
                    "children": [{"paragraph": {"elements": [
                        {"text": {"attributes": {"content": text}}}]}}]}

        block = {"table": {"attributes": {"grid": [237, 116, 237, 116, 116]},
                           "rows": [{"cells": [cell("一级分类", 2), cell("二级分类", 2), cell("代码", 1, 2)]},
                                    {"cells": [cell("代码"), cell("名称"), cell("代码"), cell("名称")]},
                                    {"cells": [cell("01"), cell("水文"), cell("01"), cell("站网"), cell("0101")]}]}}
        self.assertEqual(otl_write.fit_table_widths([block], 740), (1, 0))
        attributes = block["table"]["attributes"]
        self.assertEqual(sum(attributes["grid"]), 740)
        self.assertGreaterEqual(min(attributes["grid"]), otl_write.TABLE_MIN_COL)

    def test_airpage_rejects_html_table_without_thead_or_tbody(self):
        with self.assertRaisesRegex(SystemExit, "thead"):
            otl_write.check_html_tables("<table><tr><td>甲</td><td>乙</td></tr></table>\n")
        otl_write.check_html_tables("<table><tbody><tr><td>甲</td></tr></tbody></table>\n")
        otl_write.check_html_tables("```html\n<table><tr><td>示例</td></tr></table>\n```\n")

    def test_airpage_table_width_zero_keeps_grid(self):
        block = {"table": {"attributes": {"grid": [800, 800]}, "rows": [{"cells": []}]}}
        self.assertEqual(otl_write.fit_table_widths([block], 0), (0, 0))
        self.assertEqual(block["table"]["attributes"]["grid"], [800, 800])

    def test_airpage_reports_tables_over_body_width(self):
        blocks = [
            {"paragraph": {"elements": [{"text": {"attributes": {"content": "说明"}}}]}},
            {"table": {"attributes": {"grid": [116, 116, 141, 173, 157, 125, 125, 125]}, "rows": []}},
            {"table": {"attributes": {"grid": [370, 370]}, "rows": []}},
        ]
        messages = []
        self.assertEqual(otl_write.report_table_widths(blocks, 740, progress=messages.append), 1)
        self.assertIn("1078px", messages[0])
        self.assertIn("8 列", messages[0])
        self.assertIn("第 1 块", messages[0])
        self.assertEqual(otl_write.report_table_widths(blocks, 0, progress=messages.append), 0)

    def test_airpage_keeps_original_heading_numbers(self):
        md = "## 4.1 概述\n\n正文里的 4.1 不动\n\n### 4.1.1 细节\n\n## 前言"
        masked, count = otl_write.mask_heading_numbers(md)
        self.assertEqual(count, 2)
        self.assertIn("## \u20634.1 概述", masked)
        self.assertIn("正文里的 4.1 不动", masked)
        self.assertIn("\n## 前言", masked)

        blocks = [{"heading": {"block_id": "h", "elements": [
            {"text": {"attributes": {"content": "\u20634.1 概述"}}}]}},
            {"paragraph": {"block_id": "p", "elements": [
                {"text": {"attributes": {"content": "\u2063正文"}}}]}}]
        self.assertEqual(otl_write.unmask_headings(blocks), 1)
        self.assertEqual(blocks[0]["heading"]["elements"][0]["text"]["attributes"]["content"], "4.1 概述")
        self.assertEqual(blocks[1]["paragraph"]["elements"][0]["text"]["attributes"]["content"], "\u2063正文")

    def test_airpage_prepare_masks_headings_only_for_convert(self):
        heading = {"heading": {"block_id": "h", "elements": [
            {"text": {"attributes": {"content": "\u20634.1 概述"}}}], "attributes": {"level": 2}}}
        with (patch.object(otl_write, "api", return_value={"blocks": [heading]}) as api,
              patch.object(otl_write, "image_plan", return_value=[])):
            prepared = otl_write.prepare_markdown("f", "## 4.1 概述\n", images="off",
                                                  progress=lambda _: None)
        self.assertIn("\u20634.1 概述", api.call_args.args[2]["content"])
        self.assertEqual(
            prepared["children"][0]["heading"]["elements"][0]["text"]["attributes"]["content"], "4.1 概述")

    def test_airpage_delete_range_rejects_empty_range(self):
        with self.assertRaises(ValueError):
            otl_write.delete_range("f", 3, 3, 5)

    def test_airpage_delete_range_deletes_then_recounts(self):
        with (patch.object(otl_write, "api") as api,
              patch.object(otl_write, "doc_children", return_value=[]) as read):
            otl_write.delete_range("f", 2, 5, 5)
        api.assert_called_once()
        self.assertEqual(api.call_args.args[2], {"block_id": "doc", "start_index": 2, "end_index": 5})
        read.assert_called_once()

    def test_office_rechecks_before_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "edit.docx")
            path.write_bytes(b"test bytes")
            baseline = {"hash": "original", "version": 1}
            candidate = {"upload_id": "u", "store_request": {"url": "https://upload.example/temporary"}}
            with (patch.object(file_write, "api", return_value=candidate) as business,
                  patch.object(_wps.client, "drive_transfer", return_value=Response({})),
                  patch.object(file_write, "current_state", return_value={"hash": "concurrent", "version": 2}),
                  contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(SystemExit, "冲突")):
                file_write.upload("drive", "parent", "file", "edit.docx", path, None, 5, False, baseline)
            self.assertTrue(business.call_args.args[1].endswith("/request_upload"))
            business.assert_called_once()

    def test_office_readback_checks_new_content(self):
        with (patch.object(file_write, "current_state", return_value={"version": 2}),
              patch.object(_wps, "download_file", return_value=(3, hashlib.sha1(b"old").hexdigest(), "")),
              patch.object(file_write.time, "sleep"), self.assertRaisesRegex(_wps.WpsError, "不一致")):
            file_write.verify_upload("drive", "file", 1, hashlib.sha1(b"new").hexdigest(), 5)


class TableTests(unittest.TestCase):
    def test_sheet_conflict_does_not_write(self):
        previous = [{"row_from": 0, "col_from": 0, "original_cell_value": "old"}]
        current = [{"row_from": 0, "col_from": 0, "original_cell_value": "concurrent"}]
        with (patch.object(sheets, "read_cells", return_value=current), patch.object(_wps.client, "api") as api,
              self.assertRaisesRegex(ValueError, "并发修改")):
            sheets.update("/v7/sheets/f/worksheets/1", sheets.parse_range("A1"), previous, {"A1": "new"}, 5)
        api.assert_not_called()

    def test_sheet_receipt_requires_matching_raw_value_or_formula(self):
        a1 = [{"row_from": 0, "col_from": 0, "cell_text": "4", "original_cell_value": "4"}]
        self.assertTrue(sheets.verify_inputs(a1, {(0, 0): ("A1", "=2+2")}))
        self.assertEqual(sheets.verify_inputs(a1, {(0, 0): ("A1", "=2+2")}, {"A1": "=2+2"}), [])
        self.assertEqual(sheets.verify_inputs(a1, {(0, 0): ("A1", "4")}), [])
        self.assertTrue(sheets.verify_inputs(a1, {(0, 0): ("A1", "5")}))

    def test_excel_formula_reader_selects_worksheet_by_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "test.xlsx")
            with zipfile.ZipFile(path, "w") as book:
                book.writestr("xl/workbook.xml", '<workbook xmlns:r="http://schemas.openxmlformats.org/'
                              'officeDocument/2006/relationships"><sheets><sheet name="Second" r:id="s2"/>'
                              '</sheets></workbook>')
                book.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="s2" '
                              'Target="worksheets/sheet2.xml"/></Relationships>')
                book.writestr("xl/worksheets/sheet2.xml", '<worksheet><sheetData><row>'
                              '<c r="B1"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>')
            self.assertEqual(sheets.workbook_formulas(path, "Second", {"B1"}), {"B1": "=1+1"})

    def test_formula_readback_waits_for_content_when_the_download_is_stale(self):
        target = {"B1": "=2+2"}
        with (patch.object(_wps.client, "api", return_value={"sheets": [{"sheet_id": 1, "name": "Sheet1"}]}),
              patch.object(_wps, "download_file") as download,
              patch.object(sheets.time, "sleep"),
              patch.object(sheets, "workbook_formulas", side_effect=[{"B1": "=1+1"}, target])):
            read = sheets.workbook_formula_reader("/v7/sheets/f/worksheets", 1, "f", "d", target, 5)
            self.assertEqual(read(), target)
        self.assertEqual(download.call_count, 2)

    def test_dbsheet_compares_target_fields_for_update_and_all_for_delete(self):
        sheet = {"fields": [{"id": "a", "name": "A", "type": "MultiLineText"},
                            {"id": "b", "name": "B", "type": "MultiLineText"}]}
        baseline = {"records": {"t": {"fields": {"a": "same", "b": "old"}}}}
        current = {"t": {"id": "t", "fields": json.dumps({"a": "same", "b": "updated"})}}
        self.assertEqual(dbsheet._snapshot_conflicts(baseline, sheet, current, ["t"], {"t": ["a"]}, False), [])
        self.assertEqual(dbsheet._snapshot_conflicts(baseline, sheet, current, ["t"], {}, True)[0]["field_id"], "b")
        self.assertTrue(dbsheet._verify_written(sheet, list(current.values()), {"t": {"b": "wanted"}}))


if __name__ == "__main__":
    unittest.main()
