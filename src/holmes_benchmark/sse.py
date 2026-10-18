"""Frame SSE across arbitrary network chunks without treating EOF as dispatch."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
import re


async def iter_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """Split only CR/LF boundaries, then strictly decode complete UTF-8 lines.

    str.splitlines (also used by HTTPX) splits valid JSON string characters such
    as U+2028. UTF-8 continuation bytes cannot contain CR or LF, so framing bytes
    first also handles multibyte characters split between network chunks.
    """
    fragments: list[bytes] = []
    skip_lf = False
    async for chunk in chunks:
        if not chunk:
            continue
        if skip_lf:
            chunk = chunk.removeprefix(b"\n")
            skip_lf = False
        start = 0
        for boundary in re.finditer(rb"\r\n|\r|\n", chunk):
            fragments.append(chunk[start:boundary.start()])
            yield b"".join(fragments).decode("utf-8")
            fragments = []
            start = boundary.end()
            skip_lf = boundary.group() == b"\r" and start == len(chunk)
        if start < len(chunk):
            fragments.append(chunk[start:])
    if fragments:
        yield b"".join(fragments).decode("utf-8")


@dataclass
class Event:
    event: str
    raw_data: str
    id: str | None
    raw_lines: list[str]
    complete: bool = True


async def iter_events(lines: AsyncIterator[str]) -> AsyncIterator[Event]:
    name, event_id = "message", None
    data: list[str] = []
    raw: list[str] = []
    first = True
    async for line in lines:
        if first:
            line = line.removeprefix("\ufeff")
            first = False
        if line == "":
            if data:
                yield Event(name, "\n".join(data), event_id, raw)
            name, event_id, data, raw = "message", None, [], []
            continue
        raw.append(line)
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name = value or "message"
        elif field == "data":
            data.append(value)
        elif field == "id" and "\x00" not in value:
            event_id = value
    if data:
        yield Event(name, "\n".join(data), event_id, raw, complete=False)
