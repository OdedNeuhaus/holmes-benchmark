"""Simple, filterable Excel sheets with literal response text."""

import math
from pathlib import Path
import re
import unicodedata


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


def _char_width(char: str) -> int:
    if char == "\t":
        return 4
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _reading_chunks(value: str, width: int = 82, max_lines: int = 12):
    """Split at a readable row height, preserving every sanitized character."""
    for text in _chunks(value):
        start, lines, column = 0, 1, 0
        for index, char in enumerate(text):
            size = _char_width(char)
            wraps = char in "\r\n" or column + size > width
            if wraps and lines >= max_lines and index > start:
                yield text[start:index]
                start, lines, column = index, 1, 0
                wraps = char in "\r\n"
            if wraps:
                lines += 1
                column = 0
            if char not in "\r\n":
                column += size
        yield text[start:]


def _line_count(value, width: int) -> int:
    # Conservative estimate: leave room for proportional fonts and cell padding.
    return sum(max(1, math.ceil(sum(_char_width(c) for c in line) / width))
               for line in str(value).split("\n"))


def _preview(value: str | None) -> str | None:
    if value is None:
        return None
    compact = " ".join(value.split())
    return compact if len(compact) <= 160 else compact[:160] + "… [full text: Answers]"


def write_excel(report: dict, directory: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    book = Workbook()
    summary = book.active
    summary.title = "Summary"
    results = book.create_sheet("Results")
    answers = book.create_sheet("Answers")
    header_fill = PatternFill("solid", fgColor="0F172A")
    stripe_fill = PatternFill("solid", fgColor="F0FDFA")
    rule = Border(bottom=Side(style="hair", color="CBD5E1"))

    def append(sheet, values):
        row = sheet.max_row + 1 if sheet.cell(1, 1).value is not None else 1
        for column, value in enumerate(values, 1):
            value = "N/A" if value is None else value
            if isinstance(value, str):
                clean = INVALID_XML.sub("", value)
                value = next(_chunks(clean))
                if len(value) < len(clean):
                    value = value[:1500] + "… [full text in Answers / JSON]"
            cell = sheet.cell(row, column, value)
            if isinstance(value, str):
                cell.data_type = "s"  # Never interpret a response as a formula.
            cell.font = Font(name="Aptos", size=11, color="334155")
            cell.alignment = Alignment(horizontal="left" if isinstance(value, str) else "right",
                                       vertical="center", wrap_text=True, indent=1)
            cell.border = rule
            if isinstance(value, float):
                cell.number_format = "0.000"
            cell.fill = stripe_fill if row > 1 and row % 2 == 0 else PatternFill("solid", fgColor="FFFFFF")
        return row

    def format_sheet(sheet, widths, filters=False):
        sheet.freeze_panes = "A2"
        sheet.sheet_view.showGridLines = False
        sheet.sheet_view.zoomScale = 90
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.print_title_rows = "1:1"
        sheet.row_dimensions[1].height = 42
        for cell in sheet[1]:
            cell.font = Font(name="Aptos", size=11, bold=True, color="FFFFFF")
            cell.fill = header_fill
        for column, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(column)].width = width
        for row in sheet.iter_rows(min_row=2):
            lines = max(_line_count(cell.value or "", max(5, int(widths[cell.column - 1] * 0.82)))
                        for cell in row)
            sheet.row_dimensions[row[0].row].height = min(300, max(32, lines * 16 + 16))
        if filters:
            # A worksheet filter is sufficient; do not also define an Excel table.
            sheet.auto_filter.ref = sheet.dimensions

    stats = report["summary"]
    append(summary, ["HolmesGPT Benchmarking", "RUN REPORT"])
    append(summary, ["Run started (UTC)", report["started_at"]])
    sections = []

    def section(label):
        row = append(summary, [label, ""])
        sections.append(row)

    section("PERFORMANCE")
    metrics = [
        ("Total requests", stats["total_requests"]),
        ("Successful requests", stats["successful_requests"]),
        ("Unsuccessful requests", stats["unsuccessful_requests"]),
        ("Success rate", stats["success_rate"]),
        ("Batch duration (s)", stats["batch_duration_seconds"]),
        ("Successful requests / second", stats["successful_requests_per_second"]),
        ("Completed answer sample count", stats["completion_seconds"]["count"]),
        *((f"{key.title()} completed answer (s)", stats["completion_seconds"][key])
          for key in ("min", "mean", "median", "p95", "max")),
    ]
    for label, value in metrics:
        row = append(summary, [label, value])
        summary.cell(row, 2).font = Font(name="Aptos", size=12, bold=True, color="0F766E")
        if label == "Success rate":
            summary.cell(row, 2).number_format = "0.0%"
    section("RUN SETTINGS")
    for label, value in [
        ("Endpoint", report["settings"]["api_url"]),
        ("Requested model", report["settings"]["requested_model"] or "Server default"),
        ("Parallel requests", report["settings"]["parallel"]),
        ("Request deadline (s)", report["settings"]["timeout_seconds"]),
        ("Finished (UTC)", report["finished_at"]),
        ("Interrupted", report["interrupted"]),
    ]:
        append(summary, [label, value])
    section("READING THIS REPORT")
    for name, note in report["measurement_notes"].items():
        append(summary, [name.replace("_", " ").title(), note])
    append(summary, ["Long text", "Full questions, answers, and errors are in Answers, split across numbered reading sections. JSON preserves the original response."])
    format_sheet(summary, [44, 100])
    summary["A1"].font = Font(name="Aptos Display", size=19, bold=True, color="FFFFFF")
    summary["B1"].font = Font(name="Aptos", size=11, bold=True, color="5EEAD4")
    summary.row_dimensions[1].height = 56
    for row in sections:
        for cell in summary[row]:
            cell.fill = PatternFill("solid", fgColor="0F766E")
            cell.font = Font(name="Aptos", size=11, bold=True, color="FFFFFF")

    append(results, ["Request", "Question", "Outcome", "First text (s)", "Completed answer (s)",
                     "Elapsed (s)", "Tool calls", "Tool errors", "Last prompt tokens",
                     "Last output tokens", "Last total tokens", "Requested model", "HTTP status", "Error"])
    append(answers, ["Request", "Content", "Part", "Text"])
    for result in report["requests"]:
        usage = result["last_reported_usage"] or {}

        def token(name):
            value = usage.get(name)
            try:
                return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
            except OverflowError:
                return None

        row = append(results, [
            result["request_id"], _preview(result["question"]), result["outcome"],
            result["first_text_seconds"], result["completion_seconds"], result["elapsed_seconds"],
            result["tool_call_count"], result["tool_error_count"], token("prompt_tokens"),
            token("completion_tokens"), token("total_tokens"), result["requested_model"] or "Server default",
            result["http_status"], _preview(result["error"]),
        ])
        success = result["outcome"] == "success"
        results.cell(row, 3).font = Font(name="Aptos", size=11, bold=True,
                                       color="166534" if success else "BE123C")
        results.cell(row, 3).fill = PatternFill("solid", fgColor="DCFCE7" if success else "FFE4E6")
        results.cell(row, 5).fill = PatternFill("solid", fgColor="E0F2FE")
        results.cell(row, 5).font = Font(name="Aptos", size=11, bold=True, color="075985")
        for kind, text in (("Question", result["question"]), ("Answer", result["answer"]), ("Error", result["error"])):
            if text is None:
                continue
            for part, chunk in enumerate(_reading_chunks(text), 1):
                row = append(answers, [result["request_id"], kind, part, chunk])
                for cell in answers[row]:
                    cell.alignment = Alignment(vertical="top", wrap_text=True, indent=1)
                    if kind in ("Question", "Error"):
                        cell.fill = PatternFill("solid", fgColor="E0F2FE" if kind == "Question" else "FFE4E6")
                        cell.font = Font(name="Aptos", size=11, bold=True, color="0F172A")
    format_sheet(results, [9, 44, 23, 14, 17, 14, 11, 11, 15, 15, 15, 24, 13, 44], filters=True)
    format_sheet(answers, [10, 14, 10, 100], filters=True)
    for sheet, color in ((summary, "0F766E"), (results, "0284C7"), (answers, "7C3AED")):
        sheet.sheet_properties.tabColor = color

    destination = directory / "report.xlsx"
    temporary = directory / "report.tmp.xlsx"
    try:
        book.save(temporary)
        temporary.replace(destination)
    finally:
        book.close()
    return destination
