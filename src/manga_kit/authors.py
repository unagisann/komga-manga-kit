"""Author proposals need review; explicit names retain punctuation and spacing."""
import re
import unicodedata

from .config import ConfigError

ROLE_LABELS = {"writer": "原作・著", "penciller": "作画", "inker": "ペン入れ", "colorist": "彩色",
               "letterer": "文字", "cover": "表紙", "editor": "編集", "translator": "翻訳"}
_PLACEHOLDER = re.compile(r"^(他|ほか|その他|等|不明|作者不明|原作不明|アンソロ|アンソロジー|オムニバス|unknown|and others)$", re.I)
_ROLES = {"原作": "writer", "著": "writer", "脚本": "writer", "作画": "penciller", "漫画": "penciller",
          "画": "penciller", "翻訳": "translator", "編集": "editor"}
# A full-width x next to a Japanese name joins names (山田ｘ田中, ONEｘ村田).
# Keep it in Latin words, including their edges and mixed-width spellings.
# Ambiguous Latin-only pairs can use × or a spaced x as an explicit separator.
_JAPANESE_NAME_CHAR = r"\u3005\u3007\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uff66-\uff9d"
_FULLWIDTH_X = re.compile(rf"(?<=[{_JAPANESE_NAME_CHAR}])[ｘＸ]|[ｘＸ](?=[{_JAPANESE_NAME_CHAR}])")
_SEPARATOR = re.compile(r"\s*[×✕&/、,;+]\s*|\s+[xX]\s+|(?<=[^\x00-\x7f])\s*[xX]\s*(?=[^\x00-\x7f])"
                        r"|\s+(?=(?:原作|作画|漫画|脚本|翻訳|編集|著|画)\s*[:：])")


def author_values(values, *, deduplicate=True):
    if not isinstance(values, list) or len(values) > 100:
        raise ConfigError("著者は100件以内の名前・役割の一覧で指定してください。")
    result, seen = [], set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {"name", "role"}:
            raise ConfigError("著者には name と role を指定してください。")
        for key, limit in (("name", 300), ("role", 100)):
            value = item[key]
            if not isinstance(value, str) or not 1 <= len(value) <= limit or any(ord(c) < 32 for c in value):
                raise ConfigError("著者名は1〜300文字、役割は1〜100文字で指定してください。")
        name, role = item["name"].strip(), item["role"].strip().lower()
        if not name or not role or _PLACEHOLDER.fullmatch(unicodedata.normalize("NFKC", name)):
            raise ConfigError("空の値や「他」「作者不明」等は著者に指定できません。")
        if len(role) > 100:
            raise ConfigError("正規化後の役割が長すぎます。")
        key = (name, role)
        if not deduplicate or key not in seen:
            result.append({"name": name, "role": role})
            seen.add(key)
    return result


def author_candidates(text):
    if not isinstance(text, str) or len(text) > 300:
        raise ConfigError("著者候補の元の表記は300文字以内で指定してください。")
    # Full-width separators are decided before NFKC turns them into ASCII x.
    text = unicodedata.normalize("NFKC", _FULLWIDTH_X.sub("×", text)).strip()
    # Spaces and middle dots are parts of names, not generic separators.
    parts = _SEPARATOR.split(text)
    candidates = []
    for part in parts:
        role = "writer"
        match = re.match(r"^(原作|作画|漫画|脚本|翻訳|編集|著|画)(?:\s*[:：]\s*|\s+)", part)
        if match:
            role, part = _ROLES[match[1]], part[match.end():]
        part = re.sub(r"\([^)]*\)", "", part).strip()
        if part and not _PLACEHOLDER.fullmatch(part):
            candidates.append({"name": part, "role": role})
    return author_values(candidates)
