import codecs

from ssh_mcp.errors import ToolError


class OutputBuffer:
    """Raw-byte hard cap; byte cursors, UTF-8 replacement with no split valid characters."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.data = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        available = max(0, self.capacity - len(self.data))
        self.data.extend(chunk[:available])
        self.truncated |= len(chunk) > available

    def read(self, cursor: int, limit: int, complete: bool) -> tuple[str, int]:
        if cursor < 0 or cursor > len(self.data) or limit < 4 or limit > 65536:
            raise ToolError("INVALID_CURSOR", "游标须在已保留字节范围内，limit 为 4..65536 字节")
        # Reject positions inside a valid UTF-8 codepoint, but permit arbitrary invalid bytes.
        for start in range(max(0, cursor - 3), cursor):
            first = self.data[start]
            size = (
                2
                if 0xC2 <= first <= 0xDF
                else 3
                if 0xE0 <= first <= 0xEF
                else 4
                if 0xF0 <= first <= 0xF4
                else 0
            )
            if start < cursor < start + size <= len(self.data):
                try:
                    bytes(self.data[start : start + size]).decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    continue
                raise ToolError(
                    "INVALID_CURSOR", "游标不可位于 UTF-8 字符中间，请使用返回的下一游标"
                )
        end = min(cursor + limit, len(self.data))
        final = (complete or self.truncated) and end == len(self.data)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        text = decoder.decode(bytes(self.data[cursor:end]), final=final)
        pending, _ = decoder.getstate()
        return text, end - len(pending)
