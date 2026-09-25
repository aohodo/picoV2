"""Lossless text decoding for repository files.

Repository text is not necessarily UTF-8.  Tooling must therefore carry the
encoding and BOM alongside decoded text so an edit can be written back without
silently changing the file's representation.
"""

from dataclasses import dataclass
from pathlib import Path

from .path_support import native_path


class TextDecodingError(UnicodeError):
    """Raised when a repository file cannot be decoded without data loss."""


@dataclass(frozen=True)
class TextDocument:
    text: str
    encoding: str
    bom: bytes = b""

    def to_bytes(self, text=None):
        value = self.text if text is None else str(text)
        return self.bom + value.encode(self.encoding, errors="strict")


_BOMS = (
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def decode_text_document(raw, path=""):
    raw = bytes(raw)
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            try:
                return TextDocument(raw[len(bom) :].decode(encoding, errors="strict"), encoding, bom)
            except UnicodeDecodeError as exc:
                raise TextDecodingError(f"could not decode {path or 'file'} as {encoding}") from exc

    if b"\x00" in raw:
        raise TextDecodingError(f"{path or 'file'} appears to be binary")

    for encoding in ("utf-8", "gb18030"):
        try:
            return TextDocument(raw.decode(encoding, errors="strict"), encoding)
        except UnicodeDecodeError:
            continue
    raise TextDecodingError(
        f"could not decode {path or 'file'} losslessly; supported encodings are UTF BOM, UTF-8, and GB18030"
    )


def read_text_document(path):
    path = Path(path)
    return decode_text_document(native_path(path).read_bytes(), path)


def write_text_document(path, text, document=None):
    path = Path(path)
    native_path(path.parent).mkdir(parents=True, exist_ok=True)
    document = document or TextDocument("", "utf-8")
    native_path(path).write_bytes(document.to_bytes(text))
