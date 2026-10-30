"""Compile ``.po`` files into ``.mo`` files without GNU gettext.

Django's built-in ``compilemessages`` command depends on the external
``msgfmt`` executable, which is not installed on every machine (notably
Windows). This command implements the small subset of the GNU MO file format
that the project needs, using only the Python standard library.

Usage::

    python manage.py compiletranslations
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    '"': '"',
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "'": "'",
}

_TOKEN_RE = re.compile(r'^(msgctxt|msgid|msgstr|msgid_plural)(?:\[(\d+)\])?\s+"(.*)"\s*$')


def _unescape(text: str) -> str:
    result = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            result.append(ESCAPES.get(following, following))
            index += 2
        else:
            result.append(char)
            index += 1
    return "".join(result)


def _append(current: dict, target, text: str) -> None:
    if current is None or target is None:
        return
    if target == "msgctxt":
        current["msgctxt"] += text
    elif target == "msgid":
        current["msgid"] = (current["msgid"] or "") + text
    elif target == "msgid_plural":
        current["msgid_plural"] = (current["msgid_plural"] or "") + text
    else:  # ("msgstr", index)
        _, msgstr_index = target
        current["msgstrs"][msgstr_index] = current["msgstrs"].get(msgstr_index, "") + text


def parse_po(path: Path) -> list[dict]:
    """Parse a ``.po`` file into a list of message dictionaries."""
    entries: list[dict] = []
    current: dict | None = None
    target = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()

        if not line.strip():
            if current is not None:
                entries.append(current)
                current = None
                target = None
            continue

        if line.lstrip().startswith("#"):
            continue

        stripped = line.strip()

        if stripped.startswith('"'):
            text = stripped[1:-1] if stripped.endswith('"') else stripped[1:]
            _append(current, target, _unescape(text))
            continue

        match = _TOKEN_RE.match(stripped)
        if not match:
            continue

        keyword, index, text = match.group(1), match.group(2), match.group(3)
        if current is None:
            current = {"msgctxt": "", "msgid": None, "msgid_plural": None, "msgstrs": {}}

        value = _unescape(text)
        if keyword == "msgctxt":
            current["msgctxt"] = value
            target = "msgctxt"
        elif keyword == "msgid":
            current["msgid"] = value
            target = "msgid"
        elif keyword == "msgid_plural":
            current["msgid_plural"] = value
            target = "msgid_plural"
        else:  # msgstr / msgstr[n]
            msgstr_index = int(index) if index is not None else 0
            current["msgstrs"][msgstr_index] = value
            target = ("msgstr", msgstr_index)

    if current is not None:
        entries.append(current)

    return entries


def build_catalog(entries: list[dict]) -> dict[str, str]:
    """Turn parsed entries into a ``{key: translation}`` mapping for the MO file."""
    catalog: dict[str, str] = {}

    for entry in entries:
        msgid = entry.get("msgid")
        if msgid is None:
            continue

        context = entry.get("msgctxt", "")
        key = f"{context}\x04{msgid}" if context else msgid

        msgstrs = entry["msgstrs"]
        plural = entry.get("msgid_plural")

        if plural is not None:
            key = f"{key}\x00{plural}"
            indexes = sorted(msgstrs)
            if indexes:
                value = "\x00".join(
                    msgstrs.get(i, "") for i in range(indexes[-1] + 1)
                )
            else:
                value = ""
        else:
            value = msgstrs.get(0, "")

        catalog[key] = value

    return catalog


def write_mo(catalog: dict[str, str], output_path: Path) -> None:
    """Write a compiled MO file from a catalog mapping."""
    messages = sorted(catalog.items(), key=lambda item: item[0].encode("utf-8"))

    ids_blob = b""
    strings_blob = b""
    id_offsets: list[tuple[int, int]] = []
    string_offsets: list[tuple[int, int]] = []

    for key, _value in messages:
        key_bytes = key.encode("utf-8")
        id_offsets.append((len(key_bytes), len(ids_blob)))
        ids_blob += key_bytes + b"\x00"

    for _key, value in messages:
        value_bytes = value.encode("utf-8")
        string_offsets.append((len(value_bytes), len(strings_blob)))
        strings_blob += value_bytes + b"\x00"

    count = len(messages)
    id_table_offset = 7 * 4
    string_table_offset = id_table_offset + 8 * count
    id_blob_offset = string_table_offset + 8 * count
    string_blob_offset = id_blob_offset + len(ids_blob)

    output = bytearray()
    output += struct.pack(
        "<7I",
        0x950412DE,          # magic number
        0,                   # format revision
        count,               # number of strings
        id_table_offset,     # offset of original-string table
        string_table_offset, # offset of translation-string table
        0,                   # hash table size
        0,                   # hash table offset
    )

    for length, offset in id_offsets:
        output += struct.pack("<2I", length, offset + id_blob_offset)
    for length, offset in string_offsets:
        output += struct.pack("<2I", length, offset + string_blob_offset)

    output += ids_blob
    output += strings_blob

    output_path.write_bytes(bytes(output))


class Command(BaseCommand):
    help = "Compile .po translation files into .mo files (no gettext required)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--locale-dir",
            action="append",
            default=[],
            help="Directory containing <language>/LC_MESSAGES/*.po. "
                 "May be repeated. Defaults to settings.LOCALE_PATHS.",
        )
        parser.add_argument(
            "--language",
            action="append",
            default=[],
            help="Only compile the given language code. May be repeated.",
        )

    def handle(self, *args, **options):
        locale_dirs = [Path(p) for p in options["locale_dir"]] or [
            Path(p) for p in settings.LOCALE_PATHS
        ]
        wanted = set(options["language"])

        compiled = 0
        for locale_dir in locale_dirs:
            if not locale_dir.exists():
                continue

            for po_file in sorted(locale_dir.glob("*/LC_MESSAGES/*.po")):
                language = po_file.parent.parent.name
                if wanted and language not in wanted:
                    continue

                entries = parse_po(po_file)
                catalog = build_catalog(entries)
                mo_file = po_file.with_suffix(".mo")
                write_mo(catalog, mo_file)

                self.stdout.write(self.style.SUCCESS(f"compiled {po_file} -> {mo_file}"))
                compiled += 1

        if not compiled:
            self.stdout.write(self.style.WARNING("No .po files found to compile."))
