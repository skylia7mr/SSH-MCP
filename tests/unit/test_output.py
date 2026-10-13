import pytest

from ssh_mcp.errors import ToolError
from ssh_mcp.jobs.output import OutputBuffer


def test_multibyte_arrives_across_chunks() -> None:
    b = OutputBuffer(100)
    raw = "中文🙂".encode()
    b.append(raw[:4])
    text, cursor = b.read(0, 4, False)
    assert (text, cursor) == ("中", 3)
    b.append(raw[4:])
    next_text, next_cursor = b.read(cursor, 4, False)
    assert (next_text, next_cursor) == ("文", 6)
    assert b.read(next_cursor, 4, True) == ("🙂", len(raw))
    with pytest.raises(ToolError):
        b.read(1, 4, True)


def test_invalid_bytes_and_hard_cap() -> None:
    b = OutputBuffer(7)
    b.append(b"a\xff" + "中文".encode())
    b.append(b"x" * 1000)
    assert len(b.data) == 7 and b.truncated
    text, cursor = b.read(0, 100, True)
    assert text == "a�中�" and cursor == 7
    assert b.read(0, 100, True) == (text, cursor)


def test_empty_and_bad_cursor() -> None:
    b = OutputBuffer(8)
    assert b.read(0, 4, True) == ("", 0)
    for cursor in (-1, 1):
        with pytest.raises(ToolError):
            b.read(cursor, 4, True)
