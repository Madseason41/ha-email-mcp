"""Guards for Home Assistant's ICU translation formatter.

The frontend parses every string in strings.json / translations/en.json with
IntlMessageFormat. A `<tag>` without a closing tag becomes
"Translation error: UNCLOSED_TAG" on the options screen. `{name}` placeholders
are real ICU arguments and must stay.
"""

from __future__ import annotations

import json
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "email_mcp_bridge"
STRINGS_PATH = COMPONENT / "strings.json"
EN_PATH = COMPONENT / "translations" / "en.json"

URL_PLACEHOLDERS = ("{remote_ui_url}", "{cloudhook_url}", "{local_path}")


def _string_values(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            yield from _string_values(value, path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _string_values(value, f"{prefix}[{index}]")
    elif isinstance(node, str):
        yield prefix, node


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_strings_and_english_translation_match():
    assert _load(EN_PATH) == _load(STRINGS_PATH)


def test_translation_strings_have_no_angle_brackets():
    offenders = []
    for path in (STRINGS_PATH, EN_PATH):
        for key, text in _string_values(_load(path)):
            if "<" in text or ">" in text:
                offenders.append(f"{path.name}:{key}")
    assert offenders == []


def test_options_description_keeps_url_placeholders():
    description = _load(STRINGS_PATH)["options"]["step"]["init"]["description"]
    for placeholder in URL_PLACEHOLDERS:
        assert placeholder in description
