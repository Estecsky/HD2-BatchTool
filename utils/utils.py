"""骨架识别与无 UI 错误类型。"""

from .constants import (
    AUTO,
    FIRST_PERSON,
    FIRST_PERSON_RIG_ID,
    RIG_KIND_PROP,
    THIRD_PERSON,
    THIRD_PERSON_RIG_ID,
)


class HD2BTError(RuntimeError):
    """可直接显示给用户的预期错误。"""


def parse_int_property(obj, key):
    value = obj.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def detect_rig_kind(obj, override=AUTO):
    """识别第三/第一人称，绝不依赖可被用户重命名的对象名称。"""
    if obj is None or obj.type != "ARMATURE":
        raise HD2BTError("请先激活一个骨架对象")
    if override in {THIRD_PERSON, FIRST_PERSON}:
        return override

    tagged = obj.get(RIG_KIND_PROP)
    if tagged in {THIRD_PERSON, FIRST_PERSON}:
        return tagged

    ids = {
        value
        for value in (
            parse_int_property(obj, "BonesID"),
            parse_int_property(obj, "StateMachineID"),
        )
        if value is not None
    }
    if THIRD_PERSON_RIG_ID in ids and FIRST_PERSON_RIG_ID in ids:
        raise HD2BTError("BonesID 与 StateMachineID 指向不同视角骨架，请先修正自定义属性")
    if THIRD_PERSON_RIG_ID in ids:
        return THIRD_PERSON
    if FIRST_PERSON_RIG_ID in ids:
        return FIRST_PERSON

    names = set(obj.data.bones.keys())
    if {"hips", "l_clavicle", "r_clavicle", "attach_hand_r", "weapon_aim"} <= names:
        return THIRD_PERSON
    if {"recoil_offset", "camera", "l_shoulder", "r_shoulder"} <= names and "hips" not in names:
        return FIRST_PERSON
    raise HD2BTError("无法可靠识别骨架视角；请在面板中手动指定第三或第一人称")


def is_hd2_armature(obj):
    """按潜兵特有属性/骨表识别游戏骨架，不依赖对象名或用户选择顺序。"""

    if obj is None or obj.type != "ARMATURE":
        return False
    if obj.get("HD2BT_WorkingRig") or obj.get("HD2BT_RigKind") in {THIRD_PERSON, FIRST_PERSON}:
        return True
    if obj.get("HD2BT_PoseSystem") == "BODY_POSE_V1":
        return True
    ids = {
        value for value in (
            parse_int_property(obj, "BonesID"),
            parse_int_property(obj, "StateMachineID"),
        ) if value is not None
    }
    if ids & {THIRD_PERSON_RIG_ID, FIRST_PERSON_RIG_ID}:
        return True
    names = set(obj.data.bones.keys())
    return (
        {"hips", "l_clavicle", "r_clavicle", "attach_hand_r"} <= names
        or {"recoil_offset", "camera", "l_shoulder", "r_shoulder"} <= names
    )
