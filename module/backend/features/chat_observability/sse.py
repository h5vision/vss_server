"""Minimal incremental parser for the VSS Server-Sent Events contract."""

from __future__ import annotations

import json
from typing import Any


class VssSseParser:
    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[tuple[str, dict[str, Any]]]:
        self._buffer += chunk
        events: list[tuple[str, dict[str, Any]]] = []
        while True:
            boundary = self._frame_boundary(self._buffer)
            if boundary is None:
                break
            raw_frame, consumed = boundary
            self._buffer = self._buffer[consumed:]
            parsed = self._parse_frame(raw_frame)
            if parsed is not None:
                events.append(parsed)
        return events

    @staticmethod
    def _frame_boundary(buffer: bytes) -> tuple[bytes, int] | None:
        candidates = []
        for marker in (b"\n\n", b"\r\n\r\n"):
            index = buffer.find(marker)
            if index >= 0:
                candidates.append((index, marker))
        if not candidates:
            return None
        index, marker = min(candidates, key=lambda item: item[0])
        return buffer[:index], index + len(marker)

    @staticmethod
    def _parse_frame(frame: bytes) -> tuple[str, dict[str, Any]] | None:
        try:
            text = frame.decode("utf-8")
        except UnicodeDecodeError:
            return None
        event = "message"
        data_lines: list[str] = []
        for line in text.replace("\r\n", "\n").split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return None
        try:
            payload = json.loads("\n".join(data_lines))
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return event, payload
