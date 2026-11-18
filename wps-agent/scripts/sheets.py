"""Read and edit live cells in a WPS spreadsheet or smart sheet."""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import _wps


def cell_address(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", value.upper())
    if not match:
        raise ValueError(f"无效的 A1 单元格地址 {value}")
    col = 0
    for char in match[1]:
        col = col * 26 + ord(char) - ord("A") + 1
    row = int(match[2])
    if row > 1048576 or col > 16384:
        raise ValueError(f"单元格地址超出边界 {value}")
    return row - 1, col - 1


def bounds(start, end) -> dict:
    return {"row_from": start[0], "row_to": end[0], "col_from": start[1], "col_to": end[1]}


def parse_range(value: str) -> dict:
    first, _, last = value.partition(":")
    start, end = cell_address(first), cell_address(last or first)
    height, width = end[0] - start[0] + 1, end[1] - start[1] + 1
    if height < 1 or width < 1 or height * width > 1024:
        raise ValueError("区域需按左上角到右下角填写，每次最多读取 1024 个单元格")
    return bounds(start, end)


def read_cells(route, area, timeout):
    data = _wps.client.api("GET", f"{route}/range_data", query=area, timeout=timeout,
                           operation="读取单元格区域")
    cells = data.get("range_data")
    if not isinstance(cells, list):
        raise _wps.WpsError("读取单元格区域", "响应缺少 range_data")
    return cells


def index_cells(cells, targets):
    result = {}
    for cell in cells:
        if not isinstance(cell, dict) or not all(type(cell.get(key)) is int for key in ("row_from", "col_from")):
            raise ValueError("服务器返回无法定位的单元格")
        start = (cell["row_from"], cell["col_from"])
        end = (cell.get("row_to", start[0]), cell.get("col_to", start[1]))
        if start in targets:
            result[start] = cell
        for point in targets:
            if point != start and start[0] <= point[0] <= end[0] and start[1] <= point[1] <= end[1]:
                raise ValueError("目标位于合并区域内，请先确认合并单元格的左上角")
    return result


def verify_inputs(cells, expected, formulas=None):
    """Verify formula text separately from calculated or formatted values."""
    failures = []
    indexed = index_cells(cells, expected)
    for point, (address, value) in expected.items():
        cell = indexed.get(point, {})
        raw = cell.get("original_cell_value")
        formula = cell.get("formula") or (raw if isinstance(raw, str) and raw.startswith("=") else None)
        if value.startswith("="):
            formula = formula or (formulas or {}).get(address.upper())
            if formula != value:
                failures.append(f"{address} 公式原文未确认")
        elif formula or (raw if raw is not None else cell.get("cell_text", "")) != value:
            failures.append(f"{address} 单元格值与预期不一致")
    return failures


def workbook_formulas(path, sheet_name, addresses):
    """Read the formula source from the requested sheet in an OOXML workbook."""
    relns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    with zipfile.ZipFile(path) as workbook:
        with workbook.open("xl/workbook.xml") as source:
            sheets = ET.parse(source).findall("./{*}sheets/{*}sheet")
        sheet = next((item for item in sheets if item.get("name") == sheet_name), None)
        if sheet is None:
            return {}
        with workbook.open("xl/_rels/workbook.xml.rels") as source:
            relationships = ET.parse(source).getroot()
        target = next((item.get("Target") for item in relationships
                       if item.get("Id") == sheet.get(f"{relns}id") and item.get("TargetMode") != "External"), None)
        if not target:
            return {}
        entry = target.lstrip("/") if target.startswith("/") else posixpath.normpath(f"xl/{target}")
        if not entry.startswith("xl/worksheets/"):
            return {}
        formulas = {}
        with workbook.open(entry) as source:
            for _, cell in ET.iterparse(source, events=("end",)):
                if cell.tag.rsplit("}", 1)[-1] == "row":
                    cell.clear()
                if cell.tag.rsplit("}", 1)[-1] != "c" or cell.get("r") not in addresses:
                    continue
                formula = cell.find("{*}f")
                if formula is not None and formula.text is not None:
                    formulas[cell.get("r")] = f"={formula.text}"
                if len(formulas) == len(addresses):
                    break
        return formulas


def workbook_formula_reader(prefix, sheet_id, file_id, drive_id, expected, timeout):
    """Allow the downloadable copy to catch up with the live cells."""
    if not drive_id:
        drive_id = _wps.drive_id_from_meta(_wps.meta_by_id(file_id, timeout))

    def read():
        response = _wps.client.api("GET", prefix, timeout=timeout, operation="确认公式所在工作表")
        name = next((item.get("name") for item in response.get("sheets", [])
                     if str(item.get("sheet_id", item.get("id"))) == str(sheet_id)), None)
        if not name:
            return {}
        for delay in (0, 1, 2, 4, 8, 15):
            if delay:
                time.sleep(delay)
            with tempfile.TemporaryDirectory(prefix="wps-agent-formula-") as directory:
                path = Path(directory) / "readback.xlsx"
                _wps.download_file(drive_id, file_id, path, timeout)
                formulas = workbook_formulas(path, name, expected)
                if formulas == expected:
                    return formulas
        return {}

    return read


def prepare_changes(area, values):
    if not isinstance(values, dict) or not 1 <= len(values) <= 1024:
        raise ValueError("写入文件应为 1 到 1024 项的单元格对象")
    targets = {}
    changes = []
    for address, value in values.items():
        point = cell_address(address)
        row, col = point
        if (point in targets or not area["row_from"] <= row <= area["row_to"]
                or not area["col_from"] <= col <= area["col_to"]):
            raise ValueError(f"写入地址重复或超出快照区域 {address}")
        if not isinstance(value, str) or len(value) > 1024:
            raise ValueError(f"{address} 应为不超过 1024 字符的输入文本")
        changes.append({**bounds(point, point), "op_type": "cell_operation_type_formula", "formula": value})
        targets[point] = (address, value)
    return changes, targets


def verify(route, area, targets, timeout, formula_reader=None):
    cells = read_cells(route, area, timeout)
    failures = verify_inputs(cells, targets)
    expected_formulas = {address.upper(): value for address, value in targets.values() if value.startswith("=")}
    if formula_reader and expected_formulas and any(message.endswith("公式原文未确认") for message in failures):
        try:
            read_formulas = formula_reader(expected_formulas)
            failures = verify_inputs(cells, targets, read_formulas())
        except (_wps.WpsError, OSError, ValueError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
            print(f"表格原件公式复核未完成：{exc}")
    if failures:
        raise ValueError(f"回读未通过：{'，'.join(failures)}。可执行 verify 继续回读，避免重复写入")
    print(f"已读回校验 {len(targets)} 个单元格")


def update(route, area, baseline, values, timeout, *, formula_reader=None):
    changes, targets = prepare_changes(area, values)
    previous = index_cells(baseline, targets)
    current = index_cells(read_cells(route, area, timeout), targets)
    if previous != current:
        changed = [address for point, (address, _) in targets.items() if previous.get(point) != current.get(point)]
        raise ValueError(f"目标单元格发生并发修改，已停止写入：{', '.join(changed)}")
    try:
        _wps.client.api("POST", f"{route}/range_data/batch_update", body={"range_data": changes},
                        timeout=timeout, operation="更新单元格")
    except _wps.WpsError as exc:
        if not exc.uncertain:
            raise
        print("写入响应异常，正在读取目标单元格确认结果")
    verify(route, area, targets, timeout, formula_reader)


def main():
    parser = argparse.ArgumentParser(description="读取或更新 WPS 在线表格单元格")
    parser.add_argument("action", choices=("worksheets", "get", "snapshot", "set", "verify"))
    parser.add_argument("--kind", required=True, choices=("sheets", "airsheet"))
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--drive-id", help="用于下载原件核对公式，缺省时按文件 ID 定位")
    parser.add_argument("--worksheet-id", type=int)
    parser.add_argument("--range", dest="cell_range", help="A1 区域，例如 B2:D5")
    parser.add_argument("--snapshot", type=Path, help="写入前的区域快照路径")
    parser.add_argument("--values", type=Path, help='JSON 文件，例如 {"B2": "备注", "C2": "=1+1"}')
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    prefix = f"/v7/{args.kind}/{_wps.segment(args.file_id)}/worksheets"
    if args.action == "worksheets":
        print(json.dumps(_wps.client.api("GET", prefix, timeout=args.timeout, operation="列出工作表"),
                         ensure_ascii=False, indent=2))
        return
    if args.worksheet_id is None or args.worksheet_id < 0 or not args.cell_range:
        parser.error("需要 --worksheet-id 和 --range")
    if args.action in {"snapshot", "set"} and not args.snapshot:
        parser.error("需要 --snapshot")
    if args.action in {"set", "verify"} and not args.values:
        parser.error("需要 --values")
    route = f"{prefix}/{args.worksheet_id}"
    area = parse_range(args.cell_range)
    identity = {"kind": args.kind, "file_id": args.file_id, "worksheet_id": args.worksheet_id, "area": area}
    if args.action == "get":
        print(json.dumps(read_cells(route, area, args.timeout), ensure_ascii=False, indent=2))
    elif args.action == "snapshot":
        snapshot = {**identity, "cells": read_cells(route, area, args.timeout)}
        args.snapshot.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"区域快照已保存到 {args.snapshot}")
    else:
        def formula_reader(expected):
            return workbook_formula_reader(prefix, args.worksheet_id, args.file_id, args.drive_id, expected, args.timeout)

        values = json.loads(args.values.read_text(encoding="utf-8"))
        if args.action == "verify":
            _, wanted = prepare_changes(area, values)
            verify(route, area, wanted, args.timeout, formula_reader)
            return
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("cells"), list)
                or any(snapshot.get(key) != value for key, value in identity.items())):
            raise ValueError("快照与本次文件、工作表或区域不匹配")
        update(route, area, snapshot["cells"], values, args.timeout, formula_reader=formula_reader)


if __name__ == "__main__":
    _wps.use_utf8_stdio()
    _wps.run(main)
