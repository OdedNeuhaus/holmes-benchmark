"""Readable Excel workbooks; response text is always literal, never a formula."""

import math
from pathlib import Path
import re


# XML 1.0 excludes these even when openpyxl's own control check allows them.
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _chunks(value: str):
    text = INVALID_XML.sub("", value)
    start, units = 0, 0
    for index, char in enumerate(text):
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > 32000:
            yield text[start:index]
            start, units = index, 0
        units += size
    yield text[start:]


def write_excel(report: dict, directory: Path) -> Path:
    # Lazy imports let JSON survive a broken/missing optional Excel environment.
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    navy, teal, pale = "17324D", "007F86", "E7F3F4"
    book = Workbook()
    summary = book.active
    summary.title = "Summary"
    results = book.create_sheet("Results")
    answers = book.create_sheet("Answers")

    def put(sheet, row, column, value):
        if value is None:
            value = "N/A"
        if isinstance(value, str):
            # Tables use short excerpts; the Answers sheet holds every chunk.
            clean = INVALID_XML.sub("", value)
            chunks = _chunks(clean)
            first = next(chunks)
            if len(first) < len(clean):
                first = first[:1500] + "… [continued in Answers / JSON]"
            cell = sheet.cell(row, column, first)
            cell.data_type = "s"
        else:
            cell = sheet.cell(row, column, value)
        cell.alignment = Alignment(vertical="top", wrap_text=True)
        if isinstance(value, float):
            cell.number_format = "0.000"
        return cell

    def table(sheet, headers, name):
        for col, header in enumerate(headers, 1):
            cell = put(sheet, 1, col, header)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=navy)
        sheet.freeze_panes = "A2"
        sheet.row_dimensions[1].height = 34
        sheet.sheet_view.showGridLines = False
        if sheet.max_row > 1:
            tab = Table(displayName=name, ref=f"A1:{get_column_letter(len(headers))}{sheet.max_row}")
            tab.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
            sheet.add_table(tab)
            sheet.auto_filter.ref = tab.ref

    headers = ["Request", "Question", "Requested model", "Outcome", "HTTP status",
               "First text (s)", "Completed answer (s)", "Elapsed (s)", "Tool calls", "Tool errors",
               "Last prompt tokens", "Last output tokens", "Last total tokens", "Started (UTC)",
               "Finished (UTC)", "Error"]
    for row, result in enumerate(report["requests"], 2):
        usage = result["last_reported_usage"] or {}

        def token(name):
            value = usage.get(name)
            return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None

        values = [result["request_id"], result["question"], result["requested_model"] or "Server default",
                  result["outcome"], result["http_status"], result["first_text_seconds"],
                  result["completion_seconds"], result["elapsed_seconds"], result["tool_call_count"],
                  result["tool_error_count"], token("prompt_tokens"), token("completion_tokens"),
                  token("total_tokens"), result["started_at"], result["finished_at"], result["error"]]
        for col, value in enumerate(values, 1):
            put(results, row, col, value)
        cell = results.cell(row, 4)
        cell.fill = PatternFill("solid", fgColor="D9EEDC" if result["outcome"] == "success" else "FCE1DE")
        cell.font = Font(bold=True, color="1F6335" if result["outcome"] == "success" else "9B2828")
        results.row_dimensions[row].height = 42
        for kind, text in (("Question", result["question"]), ("Answer", result["answer"]), ("Error", result["error"])):
            if text is None:
                continue
            for part, chunk in enumerate(_chunks(text), 1):
                target = max(2, answers.max_row + 1)
                for col, value in enumerate((result["request_id"], kind, part, chunk), 1):
                    put(answers, target, col, value)
                answers.row_dimensions[target].height = 90

    table(results, headers, "BenchmarkResults")
    table(answers, ["Request", "Content", "Part", "Text"], "BenchmarkAnswers")
    for col in range(1, len(headers) + 1):
        results.column_dimensions[get_column_letter(col)].width = 19
    for col, width in {"A": 11, "B": 55, "C": 25, "D": 24, "N": 29, "O": 29, "P": 55}.items():
        results.column_dimensions[col].width = width
    for col, width in {"A": 12, "B": 14, "C": 10, "D": 110}.items():
        answers.column_dimensions[col].width = width
    if results.max_row > 1:
        results.conditional_formatting.add(f"F2:H{results.max_row}", ColorScaleRule(
            start_type="min", start_color="DFF2EC", end_type="max", end_color="F5B7A6"))

    summary.sheet_view.showGridLines = False
    summary.freeze_panes = "A5"
    summary.merge_cells("A1:E2")
    title = put(summary, 1, 1, "HOLMESGPT  /  BENCHMARK REPORT")
    title.font = Font(size=20, bold=True, color="FFFFFF")
    title.fill = PatternFill("solid", fgColor=navy)
    summary.merge_cells("A3:E3")
    put(summary, 3, 1, "Concurrent conversations · Client-observed timings · Seconds")
    stats = report["summary"]
    metrics = [
        ("Total requests", stats["total_requests"]), ("Successful requests", stats["successful_requests"]),
        ("Unsuccessful requests", stats["unsuccessful_requests"]), ("Success rate", stats["success_rate"]),
        ("Batch duration (s)", stats["batch_duration_seconds"]),
        ("Successful requests / second", stats["successful_requests_per_second"]),
        ("Completed answer sample count", stats["completion_seconds"]["count"]),
        *((f"{key.title()} completed answer (s)", stats["completion_seconds"][key])
          for key in ("min", "mean", "median", "p95", "max")),
        ("Endpoint", report["settings"]["api_url"]),
        ("Requested model", report["settings"]["requested_model"] or "Server default"),
        ("Parallel requests", report["settings"]["parallel"]),
        ("Request deadline (s)", report["settings"]["timeout_seconds"]),
        ("Started (UTC)", report["started_at"]), ("Finished (UTC)", report["finished_at"]),
        ("Interrupted", report["interrupted"]),
    ]
    for row, (label, value) in enumerate(metrics, 5):
        put(summary, row, 1, label).font = Font(bold=True, color=navy)
        summary.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        cell = put(summary, row, 2, value)
        cell.fill = PatternFill("solid", fgColor=pale if row % 2 else "FFFFFF")
        if label == "Success rate":
            cell.number_format = "0.0%"
        summary.row_dimensions[row].height = 30
    note_row = len(metrics) + 7
    for offset, note in enumerate(report["measurement_notes"].values()):
        summary.merge_cells(start_row=note_row + offset, start_column=1, end_row=note_row + offset, end_column=5)
        cell = put(summary, note_row + offset, 1, note)
        cell.font = Font(size=10, italic=True, color=teal)
        summary.row_dimensions[note_row + offset].height = 30
    summary.column_dimensions["A"].width = 35
    for col in "BCDE":
        summary.column_dimensions[col].width = 14
    if results.max_row > 1:
        chart = BarChart()
        chart.title = "Completed answer time by request"
        chart.y_axis.title = "Seconds"
        chart.x_axis.title = "Request ID (failed requests have no bar)"
        chart.add_data(Reference(results, min_col=7, min_row=1, max_row=results.max_row), titles_from_data=True)
        chart.set_categories(Reference(results, min_col=1, min_row=2, max_row=results.max_row))
        chart.legend = None
        chart.style, chart.width, chart.height = 10, 24, 12
        summary.add_chart(chart, "G5")
    destination = directory / "report.xlsx"
    temporary = directory / "report.tmp.xlsx"
    try:
        book.save(temporary)
        temporary.replace(destination)
    finally:
        book.close()
    return destination
