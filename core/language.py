from __future__ import annotations

import re


_HANGUL_RE = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_language(text: str, configured: str = "auto") -> str:
    if configured and configured != "auto":
        return configured

    hangul = len(_HANGUL_RE.findall(text))
    kana = len(_KANA_RE.findall(text))
    cjk = len(_CJK_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))

    if kana:
        return "ja"
    if hangul and hangul >= cjk * 2 and hangul >= latin:
        return "ko"
    if cjk:
        return "zh"
    if latin:
        return "en"
    if hangul:
        return "ko"
    return "unknown"
