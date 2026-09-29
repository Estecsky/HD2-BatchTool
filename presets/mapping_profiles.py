"""Human-editable source-armature to HD2 mapping profiles.

The public JSON format intentionally mirrors the two simple tables used by
Modder's Batch Tool: one table places bones and the other routes vertex
groups.  Values are lists so several source deform groups can be merged into
one HD2 bone without relying on Blender object order.
"""

from __future__ import annotations

import json
import unicodedata
from copy import deepcopy
from pathlib import Path

from ..utils.constants import addon_root
from ..utils.utils import HD2BTError


SCHEMA_VERSION = 1
BUILTIN_MMD_FILE = addon_root() / "assets" / "mapping_mmd.json"


def normalize_source_name(value):
    """Normalize Unicode width, case and separators for robust bone matching."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    for token in ("_", ".", "-", " ", "\t", "\r", "\n"):
        text = text.replace(token, "")
    return text


def _list_of_strings(value, *, field, target):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise HD2BTError(f"映射字典 {field}.{target} 必须是非空字符串列表")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise HD2BTError(f"映射字典 {field}.{target} 含空名称或非字符串")
        if item not in result:
            result.append(item)
    return result


def validate_mapping_profile(payload):
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise HD2BTError(f"骨骼映射字典必须使用 schema_version={SCHEMA_VERSION}")
    snap_map = payload.get("snap_map")
    if not isinstance(snap_map, dict) or not snap_map:
        raise HD2BTError("骨骼映射字典缺少非空 snap_map")
    weight_map = payload.get("weight_map", snap_map)
    if not isinstance(weight_map, dict) or not weight_map:
        raise HD2BTError("骨骼映射字典的 weight_map 必须是对象")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "name": str(payload.get("name") or "未命名映射"),
        "description": str(payload.get("description") or ""),
        "snap_map": {},
        "weight_map": {},
        "options": {},
    }
    for field, rows in (("snap_map", snap_map), ("weight_map", weight_map)):
        for target, aliases in rows.items():
            if not isinstance(target, str) or not target.strip():
                raise HD2BTError(f"映射字典 {field} 含无效目标骨名")
            normalized[field][target] = _list_of_strings(
                aliases, field=field, target=target
            )

    options = payload.get("options") or {}
    if not isinstance(options, dict):
        raise HD2BTError("骨骼映射字典 options 必须是对象")
    max_total = int(options.get("max_total_bones", 255))
    if max_total < 107 or max_total > 256:
        raise HD2BTError("max_total_bones 必须位于 107 到 256 之间")
    normalized["options"] = {
        "copy_unmapped_deform_bones": bool(
            options.get("copy_unmapped_deform_bones", True)
        ),
        "max_total_bones": max_total,
        "fallback_bone": str(options.get("fallback_bone") or "hips"),
    }
    return normalized


def load_mapping_profile(path=None):
    source = Path(path).expanduser().resolve() if path else BUILTIN_MMD_FILE
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HD2BTError(f"无法读取骨骼映射字典 {source}：{exc}") from exc
    result = validate_mapping_profile(payload)
    result["source_path"] = str(source)
    return result


def save_mapping_profile(path, payload):
    normalized = validate_mapping_profile(deepcopy(payload))
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def alias_lookup(profile, field):
    lookup = {}
    for target, aliases in profile[field].items():
        for alias in aliases:
            key = normalize_source_name(alias)
            previous = lookup.get(key)
            if previous is not None and previous != target:
                raise HD2BTError(
                    f"映射字典中来源名 {alias!r} 同时指向 {previous} 和 {target}"
                )
            lookup[key] = target
    return lookup
