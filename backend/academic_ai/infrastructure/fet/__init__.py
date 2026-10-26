"""FET (Free Timetabling Software) file import.

Infrastructure, not domain: this is where XML is allowed. What it produces is
an ordinary `BaseSnapshot` plus diagnostics, identical in kind to what the
database path will produce, so nothing downstream can tell where an instance
came from.
"""

from academic_ai.infrastructure.fet.reader import FetImport, read_fet, read_fet_file

__all__ = ["FetImport", "read_fet", "read_fet_file"]
