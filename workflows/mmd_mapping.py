"""MMD armature mapping, snapping and vertex-group transfer.

The mapping is intentionally data-driven.  It accepts the common Japanese,
Chinese and English names emitted by PMX importers while always writing the
stable Helldivers 2 bone names expected by downstream HD2 export and runtime contracts.
"""

from __future__ import annotations

import bpy
from mathutils import Matrix, Vector

from ..utils.constants import (
    POSE_INITIAL,
    POSE_SPACE_PROP,
    POSE_TPOSE,
    REFERENCE_GROUP_PROP,
    THIRD_PERSON,
)
from ..presets.mapping_profiles import (
    alias_lookup as profile_alias_lookup,
    load_mapping_profile,
    normalize_source_name,
)
from ..utils.reference import temporary_reference_roots
from ..utils.utils import HD2BTError, detect_rig_kind
from .facial_controls import is_facial_micro_control


def _aliases(*values):
    return tuple(values)


ROLE_ALIASES = {
    "center": _aliases("センター", "center", "centre", "中心"),
    "lower_body": _aliases("下半身", "lower body", "lowerbody", "pelvis"),
    "upper_body": _aliases("上半身", "upper body", "upperbody"),
    "upper_body2": _aliases("上半身2", "上半身２", "upper body2", "upperbody2", "chest"),
    "upper_body3": _aliases("上半身3", "上半身３", "upper body3", "upperbody3"),
    "neck": _aliases("首", "neck"),
    "head": _aliases("頭", "head"),
    "l_shoulder": _aliases("左肩", "左肩P", "左肩p", "shoulder_l", "left shoulder", "l shoulder"),
    "r_shoulder": _aliases("右肩", "右肩P", "右肩p", "shoulder_r", "right shoulder", "r shoulder"),
    "l_arm": _aliases("左腕", "arm_l", "left arm", "l arm"),
    "r_arm": _aliases("右腕", "arm_r", "right arm", "r arm"),
    "l_arm_twist": _aliases("左腕捩", "左腕捻", "arm twist_l", "left arm twist"),
    "r_arm_twist": _aliases("右腕捩", "右腕捻", "arm twist_r", "right arm twist"),
    "l_elbow": _aliases("左ひじ", "左肘", "elbow_l", "left elbow", "l elbow"),
    "r_elbow": _aliases("右ひじ", "右肘", "elbow_r", "right elbow", "r elbow"),
    "l_wrist_twist": _aliases("左手捩", "左手捻", "wrist twist_l", "left wrist twist"),
    "r_wrist_twist": _aliases("右手捩", "右手捻", "wrist twist_r", "right wrist twist"),
    "l_wrist": _aliases("左手首", "wrist_l", "left wrist", "l wrist", "left hand"),
    "r_wrist": _aliases("右手首", "wrist_r", "right wrist", "r wrist", "right hand"),
    "l_leg": _aliases("左足", "leg_l", "left leg", "l leg", "left thigh"),
    "r_leg": _aliases("右足", "leg_r", "right leg", "r leg", "right thigh"),
    "l_knee": _aliases("左ひざ", "左膝", "knee_l", "left knee", "l knee"),
    "r_knee": _aliases("右ひざ", "右膝", "knee_r", "right knee", "r knee"),
    "l_ankle": _aliases("左足首", "ankle_l", "left ankle", "l ankle", "left foot"),
    "r_ankle": _aliases("右足首", "ankle_r", "right ankle", "r ankle", "right foot"),
    "l_toe": _aliases("左足先EX", "左足先ＥＸ", "左つま先", "toe_l", "left toe"),
    "r_toe": _aliases("右足先EX", "右足先ＥＸ", "右つま先", "toe_r", "right toe"),
}


FINGER_NAMES = {
    "thumb": (("親指",), "thumb"),
    "index": (("人指", "人差指"), "index"),
    "middle": (("中指",), "middle"),
    "ring": (("薬指",), "ring"),
    "pinky": (("小指",), "pinky"),
}


def _finger_aliases(side_jp, side_en, finger, segment):
    jp_names, en_name = FINGER_NAMES[finger]
    aliases = []
    # MMD thumbs are commonly numbered 0/1/2 while HD2 uses 1/2/3.
    mmd_segment = segment - 1 if finger == "thumb" else segment
    for jp_name in jp_names:
        aliases.extend((f"{side_jp}{jp_name}{mmd_segment}", f"{side_jp}{jp_name}{mmd_segment:d}"))
    aliases.extend((
        f"{side_en}_{en_name}_{mmd_segment}",
        f"{side_en} {en_name} {mmd_segment}",
        f"{en_name}{mmd_segment}_{side_en}",
    ))
    return tuple(aliases)


for _side, _jp, _en in (("l", "左", "left"), ("r", "右", "right")):
    for _finger in FINGER_NAMES:
        for _segment in (1, 2, 3):
            ROLE_ALIASES[f"{_side}_{_finger}_{_segment}"] = _finger_aliases(
                _jp, _en, _finger, _segment
            )


# Target HD2 head position <- source MMD semantic role.  Synthetic entries are
# handled below; keeping this table explicit makes it consumable by other agents.
HD2_BONE_ROLE_MAP = {
    "hips": "lower_body",
    "spine1": "upper_body",
    "spine2": "upper_body2",
    "chest": "upper_body3",
    "neck": "neck",
    "head": "head",
    "l_clavicle": "l_shoulder",
    "l_shoulder": "l_arm",
    "l_shoulder_twist": "l_arm_twist",
    "l_elbow": "l_elbow",
    "l_hand_twist": "l_wrist_twist",
    "l_hand": "l_wrist",
    "r_clavicle": "r_shoulder",
    "r_shoulder": "r_arm",
    "r_shoulder_twist": "r_arm_twist",
    "r_elbow": "r_elbow",
    "r_hand_twist": "r_wrist_twist",
    "r_hand": "r_wrist",
    "l_thigh": "l_leg",
    "l_knee": "l_knee",
    "l_foot": "l_ankle",
    "l_ball": "l_toe",
    "r_thigh": "r_leg",
    "r_knee": "r_knee",
    "r_foot": "r_ankle",
    "r_ball": "r_toe",
}

for _side in ("l", "r"):
    for _finger in FINGER_NAMES:
        for _segment in (1, 2, 3):
            HD2_BONE_ROLE_MAP[f"{_side}_{_finger}_finger{_segment}"] = (
                f"{_side}_{_finger}_{_segment}"
            )


# Skin weights deliberately map upper_body2 to chest.  HD2 spine2 remains an
# animation parent, while the visible upper torso follows chest as it does in a
# conventional two-segment MMD torso.  This avoids duplicating weights.
GROUP_ROLE_TARGET = {
    "lower_body": "hips",
    "upper_body": "spine1",
    "upper_body2": "chest",
    "upper_body3": "chest",
    "neck": "neck",
    "head": "head",
    "l_shoulder": "l_clavicle",
    "r_shoulder": "r_clavicle",
    "l_arm": "l_shoulder",
    "r_arm": "r_shoulder",
    "l_arm_twist": "l_shoulder_twist",
    "r_arm_twist": "r_shoulder_twist",
    "l_elbow": "l_elbow",
    "r_elbow": "r_elbow",
    "l_wrist_twist": "l_hand_twist",
    "r_wrist_twist": "r_hand_twist",
    "l_wrist": "l_hand",
    "r_wrist": "r_hand",
    "l_leg": "l_thigh",
    "r_leg": "r_thigh",
    "l_knee": "l_knee",
    "r_knee": "r_knee",
    "l_ankle": "l_foot",
    "r_ankle": "r_foot",
    "l_toe": "l_ball",
    "r_toe": "r_ball",
}
for _side in ("l", "r"):
    for _finger in FINGER_NAMES:
        for _segment in (1, 2, 3):
            GROUP_ROLE_TARGET[f"{_side}_{_finger}_{_segment}"] = (
                f"{_side}_{_finger}_finger{_segment}"
            )


def normalize_bone_name(value):
    return normalize_source_name(value)


def alias_lookup():
    result = {}
    for role, aliases in ROLE_ALIASES.items():
        for alias in aliases:
            result.setdefault(normalize_bone_name(alias), role)
    return result


ALIAS_TO_ROLE = alias_lookup()


def _property_value(owner, name):
    """Read a Blender RNA/ID property without assuming mmd_tools is enabled."""
    if owner is None:
        return None
    try:
        value = getattr(owner, name, None)
    except (AttributeError, RuntimeError):
        value = None
    if value:
        return value
    try:
        value = owner.get(name)
    except (AttributeError, KeyError, TypeError, RuntimeError):
        value = None
    return value or None


def _mmd_bone_names(armature, bone):
    """Yield display names plus Japanese/English names retained by mmd_tools.

    mmd_tools may rename Blender bones to its English display convention while
    keeping the PMX names on ``pose_bone.mmd_bone``.  Looking only at
    ``bone.name`` therefore rejects otherwise standard PMX rigs.
    """
    owners = [bone]
    pose_bone = armature.pose.bones.get(bone.name) if armature.pose else None
    if pose_bone is not None:
        owners.append(pose_bone)
    names = [bone.name]
    direct_keys = (
        "name_j", "name_e", "mmd_name_j", "mmd_name_e",
        "mmd_bone_name_j", "mmd_bone_name_e",
    )
    for owner in owners:
        for key in direct_keys:
            value = _property_value(owner, key)
            if value:
                names.append(value)
        mmd_bone = _property_value(owner, "mmd_bone")
        if mmd_bone is not None:
            for key in ("name_j", "name_e"):
                value = _property_value(mmd_bone, key)
                if value:
                    names.append(value)
    seen = set()
    for value in names:
        normalized = normalize_bone_name(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            yield normalized


def resolve_mapping_bones(armature, mapping_profile=None, *, field="snap_map"):
    if armature is None or armature.type != "ARMATURE":
        raise HD2BTError("来源骨架必须是 Armature")
    profile = mapping_profile or load_mapping_profile()
    rows = profile.get(field)
    if not isinstance(rows, dict):
        raise HD2BTError(f"骨骼映射字典缺少 {field}")
    candidates = {}
    for bone in armature.data.bones:
        for candidate in _mmd_bone_names(armature, bone):
            candidates.setdefault(candidate, bone)
    result = {}
    # Profile order is significant.  It lets a dictionary prefer weighted D
    # bones over controller bones without depending on Blender datablock order.
    for target_name, aliases in rows.items():
        for alias in aliases:
            bone = candidates.get(normalize_bone_name(alias))
            if bone is not None:
                result[target_name] = bone
                break
    return result


def resolve_mmd_roles(armature, mapping_profile=None):
    """Compatibility API returning semantic roles for body measurements."""
    mapped = resolve_mapping_bones(armature, mapping_profile)
    result = {}
    for target_name, role in HD2_BONE_ROLE_MAP.items():
        bone = mapped.get(target_name)
        if bone is not None and role not in result:
            result[role] = bone
    return result


def _missing_role_message(missing):
    labels = []
    for role in sorted(missing):
        aliases = ROLE_ALIASES.get(role, ())
        expected = aliases[0] if aliases else role
        labels.append(f"{expected}（{role}）")
    return "、".join(labels)


def measure_mmd_armature(armature, mapping_profile=None):
    mapped = resolve_mapping_bones(armature, mapping_profile)
    required = {
        "neck", "head", "l_shoulder", "r_shoulder", "l_elbow", "r_elbow",
        "l_hand", "r_hand", "l_thigh", "r_thigh", "l_knee", "r_knee",
        "l_foot", "r_foot",
    }
    missing = sorted(required - set(mapped))
    if missing:
        raise HD2BTError(
            "映射字典未识别到必要骨：" + "、".join(missing)
            + "。请指定正确来源骨架或修改映射 JSON"
        )
    positions = {
        target_name: armature.matrix_world @ bone.head_local
        for target_name, bone in mapped.items()
    }
    distance = lambda first, second: (positions[first] - positions[second]).length
    shoulder_width = distance("l_shoulder", "r_shoulder")
    upper_arm = (distance("l_shoulder", "l_elbow") + distance("r_shoulder", "r_elbow")) * 0.5
    forearm = (distance("l_elbow", "l_hand") + distance("r_elbow", "r_hand")) * 0.5
    thigh = (distance("l_thigh", "l_knee") + distance("r_thigh", "r_knee")) * 0.5
    shin = (distance("l_knee", "l_foot") + distance("r_knee", "r_foot")) * 0.5
    floor = (positions["l_foot"] + positions["r_foot"]) * 0.5
    hips = (positions["l_thigh"] + positions["r_thigh"]) * 0.5
    head_neck = distance("neck", "head")
    height = (positions["head"] - floor).length + head_neck
    if height <= 1.0e-6:
        raise HD2BTError("MMD 骨架高度退化，无法识别体型")
    torso = (positions["neck"] - hips).length
    full_arm = (
        (positions.get("l_clavicle", positions["l_shoulder"]) - positions["l_hand"]).length
        + (positions.get("r_clavicle", positions["r_shoulder"]) - positions["r_hand"]).length
    ) * 0.5
    return {
        "height": float(height),
        "foot_center_world": tuple(float(value) for value in floor),
        "ratios": {
            "shoulder_width_ratio": float(shoulder_width / height),
            "head_segment_ratio": float(head_neck / height),
            "torso_ratio": float(torso / height),
            "upper_arm_ratio": float(upper_arm / height),
            "forearm_ratio": float(forearm / height),
            "full_arm_ratio": float(full_arm / height),
            "thigh_ratio": float(thigh / height),
            "shin_ratio": float(shin / height),
            "leg_ratio": float((thigh + shin) / height),
        },
    }


def _source_transform_roots(source):
    """找出人物骨架与绑定网格的最上层对象，供整体头高对齐使用。"""

    objects = {source}
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if obj.parent is source or any(
            modifier.type == "ARMATURE" and modifier.object is source
            for modifier in obj.modifiers
        ):
            objects.add(obj)
    roots = set()
    for obj in objects:
        root = obj
        while root.parent is not None:
            root = root.parent
        roots.add(root)
    return roots


def _matrix_from_property(value):
    try:
        if len(value) != 16:
            return None
    except (TypeError, AttributeError):
        return None
    try:
        return Matrix(tuple(tuple(float(value[row * 4 + column]) for column in range(4)) for row in range(4)))
    except (TypeError, ValueError):
        return None


def _vector_from_property(value):
    try:
        if len(value) != 3:
            return None
        return Vector(float(item) for item in value)
    except (TypeError, ValueError, AttributeError):
        return None


def _reference_fit_locals(target, anchor_names):
    """始终从随包原版骨架恢复基准，修复旧工程累计写低的 head 属性。"""

    with temporary_reference_roots("TP_INITIAL") as roots:
        reference = roots["TP_INITIAL"]
        head = reference.data.bones.get("head")
        anchors = [reference.data.bones.get(name) for name in anchor_names]
        if head is None or any(bone is None for bone in anchors):
            raise HD2BTError("插件原版骨架缺少头部或脚部基准，无法计算人物接地缩放")
        head_local = head.head_local.copy()
        anchor_local = sum((bone.head_local for bone in anchors), Vector()) / len(anchors)
        feet = [reference.data.bones.get(name) for name in ("l_foot", "r_foot")]
        foot_local = sum((bone.head_local for bone in feet), Vector()) * 0.5
    # 中文注释：v14 重复吸附会把每次 -1.5cm 的结果重新保存为原始头位。这里主动
    # 覆盖旧值，而不是信任场景属性，才能让旧工程一次恢复到真正的潜兵基准。
    target["HD2BT_IndependentReferenceHeadLocal"] = tuple(head_local)
    target["HD2BT_IndependentReferenceFootLocal"] = tuple(foot_local)
    target["HD2BT_IndependentReferenceGroundBoneLocal"] = tuple(anchor_local)
    return head_local, anchor_local


FOOT_WEIGHT_TARGETS = {"l_foot", "r_foot", "l_ball", "r_ball", "l_toe", "r_toe"}


def _weight_target(name, profile):
    return profile_alias_lookup(profile, "weight_map").get(normalize_bone_name(name))


def _foot_ground_projection(meshes, profile, up_axis):
    samples = []
    for mesh in meshes:
        group_targets = {
            group.index: (
                group.name if group.name in FOOT_WEIGHT_TARGETS
                else _weight_target(group.name, profile)
            )
            for group in mesh.vertex_groups
        }
        for vertex in mesh.data.vertices:
            weight = sum(
                membership.weight for membership in vertex.groups
                if group_targets.get(membership.group) in FOOT_WEIGHT_TARGETS
            )
            if weight >= 0.20:
                samples.append(float((mesh.matrix_world @ vertex.co).dot(up_axis)))
    # 2% 低分位贴近真实鞋底，同时排除少量坏点、裙摆与未绑定附件。
    return _percentile(samples, 0.02) if len(samples) >= 8 else None


def _ground_anchor(anchor_world, meshes, profile, up_axis):
    projection = _foot_ground_projection(meshes, profile, up_axis)
    if projection is None:
        return anchor_world.copy(), "bone"
    return (
        anchor_world + up_axis * (projection - anchor_world.dot(up_axis)),
        "weighted_mesh",
    )


def _source_character_meshes(source):
    meshes = list(_bound_meshes(source))
    for obj in bpy.data.objects:
        if (
            obj.type == "MESH"
            and obj.get("HD2BT_SourceArmature") == source.name
            and obj not in meshes
        ):
            meshes.append(obj)
    return meshes


def _reference_character_meshes(target):
    return [
        obj for obj in _bound_meshes(target)
        if obj.name.startswith("content/fac_helldivers/cha_avatar/avatar_helldiver_lod0")
        and not obj.get("HD2BT_SourceArmature")
    ]


def _undo_previous_source_fit(source, target, roots):
    """撤销上一次插件施加的统一变换，保证重复点击不会累计缩放。"""

    if target.get("HD2BT_SourceArmature") != source.name:
        return
    previous = _matrix_from_property(target.get("HD2BT_SourceFitMatrixWorld"))
    if previous is not None and abs(previous.determinant()) > 1.0e-10:
        inverse = previous.inverted()
        for root in roots:
            root.matrix_world = inverse @ root.matrix_world
        bpy.context.view_layer.update()
        return
    # v14 及更早版本只做过平移，只保存了 HeadAlignmentDelta；兼容撤销旧结果。
    legacy_delta = _vector_from_property(target.get("HD2BT_HeadAlignmentDelta"))
    if legacy_delta is not None:
        inverse = Matrix.Translation(-legacy_delta)
        for root in roots:
            root.matrix_world = inverse @ root.matrix_world
        bpy.context.view_layer.update()


def align_independent_head(source, target, mapping_profile=None, *, offset=-0.015):
    """锁定可见脚底接地面，统一缩放人物并精确对齐头部。

    这里只进行保持人物体型比例的统一缩放，不套用任何平均体型。头部负偏移通过
    略微减小整体缩放实现，脚底仍固定在原潜兵接地面，不再把整个人物向下平移。
    """

    profile = mapping_profile or load_mapping_profile()
    mapped = resolve_mapping_bones(source, profile)
    source_head = mapped.get("head")
    source_balls = [mapped.get(name) for name in ("l_ball", "r_ball")]
    source_feet = [mapped.get(name) for name in ("l_foot", "r_foot")]
    if source_head is None or any(bone is None for bone in source_feet):
        raise HD2BTError("接地缩放需要来源骨架存在 head、l_foot 与 r_foot 映射")
    if not any(bone is None for bone in source_balls):
        source_anchors = source_balls
        reference_anchor_names = ("l_ball", "r_ball")
    else:
        source_anchors = source_feet
        reference_anchor_names = ("l_foot", "r_foot")
    reference_head_local, reference_anchor_local = _reference_fit_locals(
        target, reference_anchor_names
    )

    roots = _source_transform_roots(source)
    if any(root is target for root in roots):
        raise HD2BTError("来源人物与潜兵工作骨架不能共用同一个父级根对象")
    _undo_previous_source_fit(source, target, roots)

    local_z_world = target.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))
    if local_z_world.length <= 1.0e-8:
        raise HD2BTError("潜兵骨架对象变换退化，无法计算头部对齐方向")
    local_z_world.normalize()
    reference_head_world = target.matrix_world @ reference_head_local
    reference_anchor_world = target.matrix_world @ reference_anchor_local
    reference_ground_world, reference_ground_method = _ground_anchor(
        reference_anchor_world,
        _reference_character_meshes(target),
        profile,
        local_z_world,
    )
    source_head_world = source.matrix_world @ source_head.head_local
    source_anchor_world = sum(
        (source.matrix_world @ bone.head_local for bone in source_anchors), Vector()
    ) / len(source_anchors)
    source_ground_world, source_ground_method = _ground_anchor(
        source_anchor_world,
        _source_character_meshes(source),
        profile,
        local_z_world,
    )
    desired_head_world = reference_head_world + local_z_world * float(offset)
    source_height = (source_head_world - source_ground_world).dot(local_z_world)
    desired_height = (desired_head_world - reference_ground_world).dot(local_z_world)
    if source_height <= 1.0e-5 or desired_height <= 1.0e-5:
        raise HD2BTError("来源或潜兵头—脚底高度无效，无法计算接地缩放")
    scale_factor = desired_height / source_height
    if not 0.25 <= scale_factor <= 4.0:
        raise HD2BTError(f"人物统一缩放倍数异常：{scale_factor:.4f}")

    # 中文注释：负头偏移已经计入缩放倍数；围绕真实脚底缩放后，头部对齐平移的
    # Z 分量会同时把来源脚底精确放回原潜兵接地面，不会再把双脚整体压低。
    scale_transform = (
        Matrix.Translation(source_ground_world)
        @ Matrix.Scale(scale_factor, 4)
        @ Matrix.Translation(-source_ground_world)
    )
    for root in roots:
        root.matrix_world = scale_transform @ root.matrix_world
    bpy.context.view_layer.update()
    scaled_head_world = source.matrix_world @ source_head.head_local
    delta = desired_head_world - scaled_head_world
    translation = Matrix.Translation(delta)
    for root in roots:
        root.matrix_world = translation @ root.matrix_world
    bpy.context.view_layer.update()

    fit_transform = translation @ scale_transform
    target["HD2BT_SourceScale"] = float(scale_factor)
    target["HD2BT_SourceGroundMethod"] = source_ground_method
    target["HD2BT_ReferenceGroundMethod"] = reference_ground_method
    target["HD2BT_SourceGroundWorld"] = tuple(float(value) for value in source_ground_world)
    target["HD2BT_ReferenceGroundWorld"] = tuple(float(value) for value in reference_ground_world)
    target["HD2BT_SourceFitMatrixWorld"] = tuple(
        float(fit_transform[row][column]) for row in range(4) for column in range(4)
    )
    return delta


def selected_mmd_and_hd2(context, explicit_source=None, rig_kind_override="AUTO"):
    # 1.2.0：潜兵骨架有稳定的 BonesID/HD2BT 属性，直接自动识别，不再要求最后选择。
    from ..utils.utils import is_hd2_armature

    selected_armatures = [obj for obj in context.selected_objects if obj.type == "ARMATURE"]
    candidates = [obj for obj in selected_armatures if obj is not explicit_source and is_hd2_armature(obj)]
    if context.active_object in candidates:
        target = context.active_object
    elif len(candidates) == 1:
        target = candidates[0]
    else:
        target = None
    if target is None:
        raise HD2BTError("请同时选择来源骨架和潜兵骨架；插件会按 BonesID/HD2BT 属性自动识别潜兵骨架")
    if detect_rig_kind(target, rig_kind_override) != THIRD_PERSON:
        raise HD2BTError("骨骼吸附当前只支持第三人称潜兵工作骨架")
    source = explicit_source
    if source is None:
        candidates = [obj for obj in selected_armatures if obj is not target]
        if len(candidates) != 1:
            raise HD2BTError("自动识别到潜兵骨架，但无法唯一确定来源骨架；请只选择这两套骨架")
        source = candidates[0]
    if source is target or source.type != "ARMATURE":
        raise HD2BTError("来源骨架无效")
    return source, target


def _target_local_head(source, target, source_bone):
    return target.matrix_world.inverted() @ (source.matrix_world @ source_bone.head_local)


def _interpolate(first, second, factor):
    return first.lerp(second, factor)


def build_snap_heads(source, target, mapping_profile=None):
    mapping_profile = mapping_profile or load_mapping_profile()
    unknown_targets = sorted(
        (set(mapping_profile["snap_map"]) | set(mapping_profile["weight_map"]))
        - set(target.data.bones.keys())
    )
    if unknown_targets:
        raise HD2BTError(
            "映射字典包含目标骨架不存在的骨名：" + "、".join(unknown_targets[:12])
        )
    mapped = resolve_mapping_bones(source, mapping_profile)
    required = {
        "hips", "neck", "head", "l_clavicle", "r_clavicle",
        "l_shoulder", "r_shoulder", "l_elbow", "r_elbow", "l_hand", "r_hand",
        "l_thigh", "r_thigh", "l_knee", "r_knee", "l_foot", "r_foot",
    }
    missing = sorted(required - set(mapped))
    if not ({"spine1", "spine2"} & set(mapped)):
        missing.append("spine1/spine2")
    if missing:
        raise HD2BTError(
            "吸附字典未识别到必要骨：" + "、".join(missing)
            + "。请检查来源骨架或导入正确的映射 JSON"
        )
    heads = {
        target_name: _target_local_head(source, target, bone)
        for target_name, bone in mapped.items()
        if target.data.bones.get(target_name) is not None
    }
    # Two-spine rigs are common outside MMD too.  Fill the HD2-only segment
    # deterministically while still allowing a custom dictionary to provide it.
    if "spine1" not in heads and "spine2" in heads:
        heads["spine1"] = _interpolate(heads["hips"], heads["spine2"], 0.50)
    if "spine2" not in heads:
        heads["spine2"] = _interpolate(heads["spine1"], heads["neck"], 0.50)
    if "chest" not in heads:
        heads["chest"] = heads["spine2"].copy()
    for side in ("l", "r"):
        arm = heads[f"{side}_shoulder"]
        elbow = heads[f"{side}_elbow"]
        wrist = heads[f"{side}_hand"]
        heads.setdefault(f"{side}_shoulder_twist", _interpolate(arm, elbow, 0.42))
        heads.setdefault(f"{side}_hand_twist", wrist.copy())
        if target.data.bones.get(f"{side}_shoulderarmour") is not None:
            heads[f"{side}_shoulderarmour"] = _interpolate(
                heads[f"{side}_clavicle"], arm, 0.90
            )
        toe = heads.get(f"{side}_ball")
        if toe is not None and target.data.bones.get(f"{side}_toe") is not None:
            ankle = heads[f"{side}_foot"]
            heads[f"{side}_toe"] = toe + (toe - ankle) * 0.35
    head_vector = heads["head"] - heads["neck"]
    if target.data.bones.get("head_tip") is not None:
        heads["head_tip"] = heads["head"] + head_vector
    return heads


def _reference_hand_rest_frames(target):
    """从对应原版姿态模板读取手部骨骼的完整静止轴向。"""

    pose_space = str(target.get(POSE_SPACE_PROP, "")).strip()
    # 中文注释：REFERENCE_GROUP 记录骨架最初从哪个资产组导入，姿态转换后不会
    # 跟着变化；吸附时必须优先依据当前 PoseSpace，否则 T-pose 会误套初态手轴，
    # 右手在转回初态时便会翻反。旧工程缺少 PoseSpace 时才回退到导入组。
    if pose_space == POSE_TPOSE:
        role = "TP_TPOSE"
    elif pose_space == POSE_INITIAL:
        role = "TP_INITIAL"
    else:
        role = str(target.get(REFERENCE_GROUP_PROP, "")).strip()
        if role not in {"TP_INITIAL", "TP_TPOSE"}:
            role = "TP_INITIAL"
    with temporary_reference_roots(role) as roots:
        reference = roots[role]
        return {
            bone.name: {
                "offset": (bone.tail_local - bone.head_local).copy(),
                # 中文注释：head→tail 只定义骨骼局部 Y 轴；还必须保存局部 Z 轴，
                # 否则旧工程里已翻转的 roll 会继续让手指动画沿错误方向弯曲。
                "roll_axis": bone.matrix_local.to_3x3().col[2].copy(),
            }
            for bone in reference.data.bones
            if bone.name in {"l_hand", "r_hand", "l_hand_twist", "r_hand_twist"}
            or "_finger" in bone.name
        }


def _body_frame(heads, forward_hint=None):
    """Return a stable target-local torso frame and its dimensions.

    HD2 uses +X toward the character's left, +Y toward the front and +Z up in
    the reference rig.  Deriving the frame from landmarks keeps equipment
    placement valid when the imported MMD object is rotated in the scene.
    """
    hips = heads["hips"]
    neck = heads["neck"]
    left = heads.get("l_clavicle", heads["l_shoulder"])
    right = heads.get("r_clavicle", heads["r_shoulder"])
    x_axis = left - right
    z_axis = neck - hips
    if x_axis.length <= 1.0e-6 or z_axis.length <= 1.0e-6:
        raise HD2BTError("胸腔骨骼退化，无法计算背包装配坐标系")
    x_axis.normalize()
    z_axis -= x_axis * z_axis.dot(x_axis)
    if z_axis.length <= 1.0e-6:
        raise HD2BTError("胸腔骨骼共线，无法计算背包装配坐标系")
    z_axis.normalize()
    y_axis = z_axis.cross(x_axis)
    y_axis.normalize()
    center = heads.get("chest", hips.lerp(neck, 0.68))
    # MMD importers can use the opposite left/right object-space sign from the
    # HD2 reference.  Keep equipment depth oriented like the original rig even
    # when that makes this positional basis mirrored; otherwise a backpack can
    # jump through the torso to its front.
    if forward_hint is not None:
        if y_axis.dot(forward_hint) < 0.0:
            y_axis.negate()
    elif "backpack" in heads and (heads["backpack"] - center).dot(y_axis) > 0.0:
        # The reference backpack landmark is an unambiguous indication of the
        # rear side; define the opposite direction as torso-forward.
        y_axis.negate()
    # Matrix constructor takes rows, hence the transpose for basis columns.
    basis = Matrix((x_axis, y_axis, z_axis)).transposed()
    return {
        "center": center.copy(),
        "basis": basis,
        "shoulder_width": float((left - right).length),
        "torso_length": float((neck - hips).length),
    }


def _target_bone_heads(target):
    return {
        bone.name: bone.head_local.copy()
        for bone in target.data.bones
    }


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def _bound_meshes(source):
    return [
        obj for obj in bpy.data.objects
        if obj.type == "MESH" and (
            obj.parent is source
            or any(mod.type == "ARMATURE" and mod.object is source for mod in obj.modifiers)
        )
    ]


def _estimate_mmd_back_surface(source, target, frame, backpack_local):
    """Estimate the rear torso surface from vertices weighted to torso bones.

    Hair, skirts and accessories are common in MMD files and make a raw mesh
    bounding box unusable.  Prefer vertices influenced by MMD torso groups and
    use a robust percentile rather than the most extreme rear vertex.
    """
    torso_roles = {"upper_body", "upper_body2", "upper_body3", "lower_body"}
    basis_inverse = frame["basis"].inverted()
    target_inverse = target.matrix_world.inverted()
    shoulder = max(frame["shoulder_width"], 1.0e-6)
    torso = max(frame["torso_length"], 1.0e-6)
    candidates = []
    fallback = []
    max_samples = 120000
    meshes = _bound_meshes(source)
    total_vertices = sum(len(mesh.data.vertices) for mesh in meshes)
    stride = max(1, total_vertices // max_samples)
    for mesh in meshes:
        torso_group_indices = {
            group.index for group in mesh.vertex_groups
            if ALIAS_TO_ROLE.get(normalize_bone_name(group.name)) in torso_roles
            or group.name in {"hips", "spine1", "spine2", "chest"}
        }
        for vertex_index, vertex in enumerate(mesh.data.vertices):
            if vertex_index % stride:
                continue
            target_local = target_inverse @ (mesh.matrix_world @ vertex.co)
            local = basis_inverse @ (target_local - frame["center"])
            if abs(local.x) > shoulder * 0.38:
                continue
            if abs(local.z - backpack_local.z) > torso * 0.24:
                continue
            fallback.append(float(local.y))
            if torso_group_indices:
                weight = sum(
                    membership.weight for membership in vertex.groups
                    if membership.group in torso_group_indices
                )
                if weight >= 0.20:
                    candidates.append(float(local.y))
    values = candidates if len(candidates) >= 12 else fallback
    if len(values) < 8:
        return None, "anatomical"
    # Rear is negative local Y.  12% rejects isolated hair/accessory vertices
    # while remaining close to the actual back surface.
    return _percentile(values, 0.12), "weighted_mesh" if candidates else "mesh"


def build_equipment_heads(source, target, body_heads):
    """Transfer backpack attachment bones into the fitted MMD torso frame."""
    old_heads = _target_bone_heads(target)
    needed = {"hips", "neck", "l_shoulder", "r_shoulder"}
    if not needed.issubset(old_heads) or not needed.issubset({**old_heads, **body_heads}):
        return {}, {"method": "unavailable", "forward": 0.0, "down": 0.0}
    new_heads = dict(old_heads)
    new_heads.update(body_heads)
    old_frame = _body_frame(old_heads)
    new_frame = _body_frame(new_heads, Vector(old_frame["basis"].col[1]))
    old_inverse = old_frame["basis"].inverted()
    shoulder_scale = new_frame["shoulder_width"] / max(old_frame["shoulder_width"], 1.0e-6)
    torso_scale = new_frame["torso_length"] / max(old_frame["torso_length"], 1.0e-6)
    depth_scale = (shoulder_scale * torso_scale) ** 0.5
    dimension_scale = Vector((shoulder_scale, depth_scale, torso_scale))

    transferred = {}
    for name in ("backpack", "support", "support_mg"):
        point = old_heads.get(name)
        if point is None:
            continue
        local = old_inverse @ (point - old_frame["center"])
        local = Vector((
            local.x * dimension_scale.x,
            local.y * dimension_scale.y,
            local.z * dimension_scale.z,
        ))
        transferred[name] = new_frame["center"] + new_frame["basis"] @ local
    if "backpack" not in transferred:
        return transferred, {"method": "missing_backpack", "forward": 0.0, "down": 0.0}

    backpack_local = new_frame["basis"].inverted() @ (
        transferred["backpack"] - new_frame["center"]
    )
    original_local = backpack_local.copy()
    back_surface, method = _estimate_mmd_back_surface(
        source, target, new_frame, backpack_local
    )
    if back_surface is not None:
        # Keep the attachment a small, body-proportional clearance behind the
        # visible back.  The pack mesh's own offset from this attachment remains
        # unchanged, so this is not a per-character centimetre tweak.
        clearance = new_frame["shoulder_width"] * 0.055
        backpack_local.y = back_surface - clearance
    else:
        # Conservative fallback: close 22% of the anatomical chest-to-pack gap.
        backpack_local.y *= 0.78
    # Lower the attachment by 6% of hip-to-neck length, matching the lower
    # thoracic load position instead of tying it to a fixed world distance.
    backpack_local.z -= new_frame["torso_length"] * 0.06
    corrected = new_frame["center"] + new_frame["basis"] @ backpack_local
    rigid_delta = corrected - transferred["backpack"]
    for name in tuple(transferred):
        transferred[name] += rigid_delta
    forward = float(backpack_local.y - original_local.y)
    down = float(original_local.z - backpack_local.z)
    return transferred, {"method": method, "forward": forward, "down": down}


def snap_independent_to_hd2(
    source,
    target,
    *,
    head_offset=-0.015,
    auto_scale=True,
    mapping_profile=None,
):
    """把第三人称工作骨架直接吸附到当前人物自己的关节。

    不分类、不引用任何平均体型。先锁定可见脚底做保持比例的统一缩放，再让每根
    潜兵语义骨采用该人物映射骨的真实位置。
    """

    mapping_profile = mapping_profile or load_mapping_profile()
    if auto_scale:
        head_delta = align_independent_head(
            source, target, mapping_profile, offset=head_offset,
        )
    else:
        # Do not undo an earlier fit or move the source at all. The current
        # author-selected size/position becomes the baseline for future fits.
        head_delta = Vector()
        target["HD2BT_SourceScale"] = 1.0
        target["HD2BT_SourceGroundMethod"] = "disabled_keep_source"
        target["HD2BT_ReferenceGroundMethod"] = "disabled_keep_source"
        target["HD2BT_SourceFitMatrixWorld"] = tuple(
            float(row == column) for row in range(4) for column in range(4)
        )
        for key in ("HD2BT_SourceGroundWorld", "HD2BT_ReferenceGroundWorld"):
            if key in target:
                del target[key]
    heads = build_snap_heads(source, target, mapping_profile)
    reference_hand_frames = _reference_hand_rest_frames(target)
    equipment_heads, equipment_stats = build_equipment_heads(source, target, heads)
    heads.update(equipment_heads)
    previous_mode = target.mode
    previous_active = bpy.context.view_layer.objects.active
    selected = list(bpy.context.selected_objects)
    try:
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        target.select_set(True)
        bpy.context.view_layer.objects.active = target
        bpy.ops.object.mode_set(mode="EDIT")
        changed = 0
        for name, new_head in heads.items():
            edit_bone = target.data.edit_bones.get(name)
            if edit_bone is None:
                continue
            # 中文注释：v17 曾错误地把来源手掌/手指方向写进目标 Rest Pose。
            # 每次吸附先取对应原版 INITIAL/TPOSE 模板的轴向，旧受影响工程无需
            # 手工逐骨回滚；其他身体骨仍保持当前工作骨架自己的原方向。
            reference_frame = reference_hand_frames.get(name)
            offset = (
                reference_frame["offset"]
                if reference_frame is not None
                else edit_bone.tail - edit_bone.head
            )
            if offset.length <= 1.0e-6:
                offset = Vector((0.0, 0.05, 0.0))
            edit_bone.head = new_head
            edit_bone.tail = new_head + offset
            if reference_frame is not None:
                # 中文注释：恢复参考 Z 轴会同时恢复 EditBone.roll，补齐上次只修
                # head/tail 方向而没有修弯曲轴的问题。
                edit_bone.align_roll(reference_frame["roll_axis"])
            changed += 1
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        if target.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for obj in selected:
            if obj.name in bpy.data.objects:
                obj.select_set(True)
        if previous_active and previous_active.name in bpy.data.objects:
            bpy.context.view_layer.objects.active = previous_active
        if previous_active is target and previous_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=previous_mode)
    target["HD2BT_MMDSource"] = source.name
    target["HD2BT_SourceArmature"] = source.name
    target["HD2BT_MappingName"] = mapping_profile["name"]
    target["HD2BT_MappingPath"] = mapping_profile.get("source_path", "")
    target["HD2BT_IndependentRig"] = True
    target["HD2BT_AutoScaleCharacter"] = bool(auto_scale)
    target["HD2BT_HeadAlignmentOffset"] = float(head_offset) if auto_scale else 0.0
    target["HD2BT_HeadAlignmentDelta"] = tuple(float(value) for value in head_delta)
    # 清理旧工程曾保存的通用体型痕迹，避免后续工具误判为平均骨架路线。
    for key in (
        "HD2BT_MMDBodyProfile",
        "HD2BT_MMDBodyProfileConfidence",
        "HD2BT_MMDScaleFactor",
    ):
        if key in target:
            del target[key]
    target["HD2BT_BackpackFitMethod"] = equipment_stats["method"]
    target["HD2BT_BackpackForward"] = float(equipment_stats["forward"])
    target["HD2BT_BackpackDown"] = float(equipment_stats["down"])
    return changed, head_delta, equipment_stats


def _apply_group_remap(mesh, pending):
    """单次扫描网格，批量合并大量来源组，避免组数×顶点数的重复遍历。"""

    if not pending:
        return 0, 0
    rows_by_index = {
        group.index: (group, target_name)
        for group, target_name, _collapsed in pending
    }
    plans_by_target = {}
    for row in pending:
        plans_by_target.setdefault(row[1], []).append(row)
    existing_targets = {
        target_name: mesh.vertex_groups.get(target_name)
        for target_name in plans_by_target
    }
    existing_indices = {
        group.index: target_name
        for target_name, group in existing_targets.items()
        if group is not None
    }
    contributions = {}
    existing_weights = {}
    for vertex in mesh.data.vertices:
        for membership in vertex.groups:
            existing_target = existing_indices.get(membership.group)
            if existing_target is not None and membership.weight > 1.0e-8:
                existing_weights.setdefault(existing_target, {})[vertex.index] = float(
                    membership.weight
                )
            row = rows_by_index.get(membership.group)
            if row is None or membership.weight <= 1.0e-8:
                continue
            _group, target_name = row
            target_rows = contributions.setdefault(target_name, {})
            target_rows[vertex.index] = (
                target_rows.get(vertex.index, 0.0) + float(membership.weight)
            )

    renamed = merged = 0
    for target_name, plans in plans_by_target.items():
        target_group = existing_targets[target_name]
        if target_group is None:
            target_group = mesh.vertex_groups.new(name=target_name)
            # 逻辑上第一项是重命名，其余项并入它；实现上统一新建后批量写权重。
            renamed += 1
            merged += max(0, len(plans) - 1)
        else:
            merged += len(plans)
        for vertex_index, added_weight in contributions.get(target_name, {}).items():
            current_weight = existing_weights.get(target_name, {}).get(vertex_index, 0.0)
            target_group.add(
                [vertex_index], current_weight + added_weight, "REPLACE"
            )

    # 所有权重写入完成后再删除来源组，避免 Blender 重排组索引破坏扫描结果。
    for group, _target_name, _collapsed in pending:
        mesh.vertex_groups.remove(group)
    return renamed, merged


def _weighted_group_totals(meshes):
    totals = {}
    counts = {}
    for mesh in meshes:
        if mesh.type != "MESH":
            continue
        names = {group.index: group.name for group in mesh.vertex_groups}
        seen_per_group = {}
        for vertex in mesh.data.vertices:
            for membership in vertex.groups:
                if membership.weight <= 1.0e-8:
                    continue
                name = names.get(membership.group)
                if name is None:
                    continue
                totals[name] = totals.get(name, 0.0) + float(membership.weight)
                seen_per_group[name] = seen_per_group.get(name, 0) + 1
        for name, count in seen_per_group.items():
            counts[name] = counts.get(name, 0) + count
    return totals, counts


def _source_bone_targets(source, profile):
    lookup = profile_alias_lookup(profile, "weight_map")
    result = {}
    for bone in source.data.bones:
        for candidate in _mmd_bone_names(source, bone):
            target_name = lookup.get(candidate)
            if target_name is not None:
                result[bone.name] = target_name
                break
    return result


def _source_group_bones(source):
    """把顶点组可能使用的显示名/PMX 原名解析回来源骨。"""

    result = {}
    for bone in source.data.bones:
        result.setdefault(normalize_bone_name(bone.name), bone)
        for candidate in _mmd_bone_names(source, bone):
            result.setdefault(candidate, bone)
    return result


def _nearest_destination(source_bone, source_targets, selected_custom, fallback):
    current = source_bone
    while current is not None:
        target_name = source_targets.get(current.name)
        if target_name is not None:
            return target_name
        if current.name in selected_custom:
            return current.name
        current = current.parent
    return fallback


def _copy_custom_bones(source, target, names, source_targets, fallback):
    selected_set = set(names)
    names = [name for name in names if target.data.bones.get(name) is None]
    if not names:
        return 0
    previous_mode = target.mode
    previous_active = bpy.context.view_layer.objects.active
    selected_objects = list(bpy.context.selected_objects)
    try:
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        target.select_set(True)
        bpy.context.view_layer.objects.active = target
        bpy.ops.object.mode_set(mode="EDIT")
        target_inverse = target.matrix_world.inverted()
        direction_matrix = target_inverse.to_3x3() @ source.matrix_world.to_3x3()
        created = {}
        for name in names:
            source_bone = source.data.bones[name]
            edit_bone = target.data.edit_bones.new(name)
            edit_bone.head = target_inverse @ (source.matrix_world @ source_bone.head_local)
            edit_bone.tail = target_inverse @ (source.matrix_world @ source_bone.tail_local)
            if (edit_bone.tail - edit_bone.head).length <= 1.0e-6:
                edit_bone.tail = edit_bone.head + Vector((0.0, 0.01, 0.0))
            roll_axis = direction_matrix @ source_bone.matrix_local.to_3x3().col[2]
            if roll_axis.length > 1.0e-6:
                edit_bone.align_roll(roll_axis)
            edit_bone.use_deform = bool(source_bone.use_deform)
            edit_bone["HD2BT_CustomBone"] = True
            edit_bone["Animated"] = False
            created[name] = edit_bone
        for name, edit_bone in created.items():
            source_parent = source.data.bones[name].parent
            parent_name = _nearest_destination(
                source_parent, source_targets, selected_set, fallback
            ) if source_parent is not None else fallback
            parent = target.data.edit_bones.get(parent_name) if parent_name else None
            if parent is not None and parent is not edit_bone:
                edit_bone.parent = parent
                edit_bone.use_connect = False
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        if target.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for obj in selected_objects:
            if obj.name in bpy.data.objects:
                obj.select_set(True)
        if previous_active and previous_active.name in bpy.data.objects:
            bpy.context.view_layer.objects.active = previous_active
        if previous_active is target and previous_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=previous_mode)
    return len(names)


def _discard_copied_bones(target, names):
    """撤销本次转移新增的骨；不删除任何预先存在的作者骨。"""
    names = [name for name in names if name in target.data.bones]
    if not names:
        return
    active = bpy.context.view_layer.objects.active
    mode = active.mode if active else "OBJECT"
    selected = list(bpy.context.selected_objects)
    try:
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        target.select_set(True)
        bpy.context.view_layer.objects.active = target
        bpy.ops.object.mode_set(mode="EDIT")
        for name in names:
            bone = target.data.edit_bones.get(name)
            if bone is not None:
                target.data.edit_bones.remove(bone)
    finally:
        if target.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for obj in selected:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = active
        if active and mode != "OBJECT":
            bpy.ops.object.mode_set(mode=mode)


def _normalize_and_limit_weights(mesh, target_bone_names, fallback):
    groups_by_index = {group.index: group for group in mesh.vertex_groups}
    fallback_group = mesh.vertex_groups.get(fallback)
    if fallback_group is None:
        fallback_group = mesh.vertex_groups.new(name=fallback)
    unbound = limited = 0
    for vertex in mesh.data.vertices:
        influences = []
        for membership in vertex.groups:
            group = groups_by_index.get(membership.group)
            if (
                group is not None
                and group.name in target_bone_names
                and membership.weight > 1.0e-8
            ):
                influences.append((float(membership.weight), group))
        if not influences:
            fallback_group.add([vertex.index], 1.0, "REPLACE")
            unbound += 1
            continue
        influences.sort(key=lambda item: item[0], reverse=True)
        kept = influences[:4]
        dropped = influences[4:]
        if dropped:
            limited += 1
            for _weight, group in dropped:
                group.add([vertex.index], 0.0, "REPLACE")
        total = sum(weight for weight, _group in kept)
        if total <= 1.0e-8:
            fallback_group.add([vertex.index], 1.0, "REPLACE")
            unbound += 1
            continue
        for weight, group in kept:
            group.add([vertex.index], weight / total, "REPLACE")
    return unbound, limited


def _weighted_group_names(mesh):
    names = {group.index: group.name for group in mesh.vertex_groups}
    return {
        names[membership.group]
        for vertex in mesh.data.vertices
        for membership in vertex.groups
        if membership.weight > 1.0e-8 and membership.group in names
    }


# 中文注释：这些控制点由用户确认可正常进游戏的“无标题8.blend”左右手臂取样后
# 对称平均得到。横坐标 0..1 是肩到肘，1..2 是肘到腕；四列依次代表上臂、上臂
# 扭转、肘、前臂扭转。锁骨、手掌和全部手指不在本表中，算法绝不改它们。
HD2_ARM_WEIGHT_PROFILE = (
    (0.20, (0.907, 0.093, 0.000, 0.000)),
    (0.30, (0.798, 0.202, 0.000, 0.000)),
    (0.40, (0.679, 0.321, 0.000, 0.000)),
    (0.50, (0.580, 0.420, 0.000, 0.000)),
    (0.60, (0.461, 0.539, 0.000, 0.000)),
    (0.70, (0.334, 0.666, 0.000, 0.000)),
    (0.80, (0.242, 0.758, 0.000, 0.000)),
    (0.90, (0.207, 0.738, 0.055, 0.000)),
    (1.00, (0.107, 0.424, 0.469, 0.000)),
    (1.10, (0.000, 0.014, 0.919, 0.067)),
    (1.20, (0.000, 0.000, 0.828, 0.172)),
    (1.30, (0.000, 0.000, 0.726, 0.274)),
    (1.40, (0.000, 0.000, 0.616, 0.384)),
    (1.50, (0.000, 0.000, 0.504, 0.496)),
    (1.60, (0.000, 0.000, 0.394, 0.606)),
    (1.70, (0.000, 0.000, 0.286, 0.714)),
    (1.80, (0.000, 0.000, 0.237, 0.763)),
    (1.90, (0.000, 0.000, 0.213, 0.787)),
    (2.00, (0.000, 0.000, 0.200, 0.800)),
    (2.10, (0.000, 0.000, 0.000, 1.000)),
)


def _sample_hd2_arm_weight_profile(axial):
    if axial <= HD2_ARM_WEIGHT_PROFILE[0][0]:
        return HD2_ARM_WEIGHT_PROFILE[0][1]
    for (left_x, left), (right_x, right) in zip(
        HD2_ARM_WEIGHT_PROFILE,
        HD2_ARM_WEIGHT_PROFILE[1:],
    ):
        if axial <= right_x:
            factor = (axial - left_x) / max(right_x - left_x, 1.0e-8)
            values = tuple(
                left[index] * (1.0 - factor) + right[index] * factor
                for index in range(4)
            )
            total = sum(values)
            return tuple(value / total for value in values)
    return HD2_ARM_WEIGHT_PROFILE[-1][1]


def _arm_axial_position(point, shoulder, elbow, hand):
    """返回离手臂折线最近的轴向位置与距离。"""

    rows = []
    for offset, first, second in (
        (0.0, shoulder, elbow),
        (1.0, elbow, hand),
    ):
        segment = second - first
        if segment.length_squared <= 1.0e-12:
            continue
        raw = (point - first).dot(segment) / segment.length_squared
        clamped = max(0.0, min(1.0, raw))
        distance = (point - first.lerp(second, clamped)).length
        rows.append((distance, offset + raw))
    return min(rows) if rows else (float("inf"), 0.0)


def rebalance_hd2_arm_weights(target, meshes):
    """按原潜兵手臂过渡重排四个手臂组，保持其他所有权重原值。"""

    changed_vertices = 0
    for mesh in meshes:
        if mesh.type != "MESH":
            continue
        target_inverse = target.matrix_world.inverted()
        mesh_to_target = target_inverse @ mesh.matrix_world
        for side in ("l", "r"):
            names = (
                f"{side}_shoulder",
                f"{side}_shoulder_twist",
                f"{side}_elbow",
                f"{side}_hand_twist",
            )
            bones = [target.data.bones.get(name) for name in names]
            hand_bone = target.data.bones.get(f"{side}_hand")
            if any(bone is None for bone in bones) or hand_bone is None:
                continue
            existing = {
                group.name: group
                for group in mesh.vertex_groups
                if group.name in names
            }
            if not existing:
                continue
            groups_by_index = {group.index: group.name for group in existing.values()}
            shoulder = bones[0].head_local
            elbow = bones[2].head_local
            hand = hand_bone.head_local
            corridor = max((elbow - shoulder).length, (hand - elbow).length) * 0.60
            writes = {name: [] for name in names}
            for vertex in mesh.data.vertices:
                arm_total = sum(
                    float(membership.weight)
                    for membership in vertex.groups
                    if groups_by_index.get(membership.group) in names
                )
                if arm_total <= 1.0e-8:
                    continue
                distance, axial = _arm_axial_position(
                    mesh_to_target @ vertex.co,
                    shoulder,
                    elbow,
                    hand,
                )
                # 肩窝以及腕后手掌保持用户原权重；远离骨轴的披风/饰件也不套用
                # 圆柱手臂模板，避免为了修手臂而破坏独立附件。
                if axial < 0.20 or axial > 2.10 or distance > corridor:
                    continue
                factors = _sample_hd2_arm_weight_profile(axial)
                for name, factor in zip(names, factors):
                    writes[name].append((vertex.index, arm_total * factor))
                changed_vertices += 1
            for name, rows in writes.items():
                if not rows:
                    continue
                group = existing.get(name)
                if group is None:
                    # 只有曲线确实会写入有效权重时才建立缺失组，避免在非手臂
                    # 网格上凭空留下 shoulder_twist/elbow 等空顶点组。
                    if not any(weight > 1.0e-8 for _index, weight in rows):
                        continue
                    group = mesh.vertex_groups.new(name=name)
                for vertex_index, weight in rows:
                    group.add([vertex_index], weight, "REPLACE")
    return changed_vertices


def rename_vertex_groups(
    source,
    target,
    meshes,
    mapping_profile=None,
    *,
    unmapped_mode="KEEP",
):
    """转换字典权重，并按用户选择保留或父链合并未映射骨权重。

    ``COLLAPSE_TO_PARENT`` 只处理名称能解析到来源骨架的顶点组；沿来源骨父链找到
    最近的已映射骨后合并。非骨骼辅助组和没有已映射祖先的骨组仍原样保留。
    """

    profile = mapping_profile or load_mapping_profile()
    if unmapped_mode not in {"KEEP", "COLLAPSE_TO_PARENT"}:
        raise HD2BTError(f"未知未映射权重处理模式：{unmapped_mode}")
    renamed = merged = rebound = untouched = collapsed = utility_removed = 0
    arm_vertices_rebalanced = 0
    weight_lookup = profile_alias_lookup(profile, "weight_map")
    collapse_unmapped = unmapped_mode == "COLLAPSE_TO_PARENT"
    source_targets = _source_bone_targets(source, profile)
    # 中文注释：即使选择“保留未映射权重”，也要能识别并清理完全没有权重的
    # MMD 控制骨组；真正承载权重的未映射组仍严格保留给用户手修。
    source_group_bones = _source_group_bones(source)
    custom_bones_added = 0
    physics_transfer = None
    physics_transfer_report = None
    source_project = getattr(source.data, "hd2pb_project", None)
    if source_project is not None and source_project.schema_version and source_project.chains:
        if collapse_unmapped:
            raise HD2BTError("当前源骨架有物理项目，请选择保留未映射骨；父链合并会丢失物理自由度")
        from HD2PhysBoneTool.physbone.blender import transfer as physics_transfer
        target_project = target.data.hd2pb_project
        if target_project.schema_version:
            raise HD2BTError("目标骨架已有物理项目；请使用空的工作骨架，避免覆盖作者数据")
    if not collapse_unmapped:
        needed = set(physics_transfer.required_bones(source, for_transfer=True)) if physics_transfer else set()
        physics_required = set(needed)
        facial_merged = []
        for bone in source.data.bones:
            if bone.name in source_targets or bone.name in physics_required:
                continue
            if not any(is_facial_micro_control(name) for name in _mmd_bone_names(source, bone)):
                continue
            # Only collapse within a positively resolved head hierarchy. Never
            # guess from a name alone, delete source bones, or remove a physical
            # chain/collider's required attachment. Weighted controls map to the
            # head instead of generating useless output-only expression bones.
            destination = _nearest_destination(bone.parent, source_targets, set(), None)
            if destination == 'head' and target.data.bones.get(destination) is not None:
                source_targets[bone.name] = destination
                facial_merged.append(bone.name)
        for mesh in meshes:
            if mesh.type != "MESH":
                continue
            for name in _weighted_group_names(mesh):
                bone = source_group_bones.get(normalize_bone_name(name))
                if bone is not None:
                    needed.add(bone.name)
        custom = set()
        for name in needed:
            bone = source.data.bones.get(name)
            while bone is not None and bone.name not in source_targets:
                if bone.name in target.data.bones and not target.data.bones[bone.name].get("HD2BT_CustomBone"):
                    raise HD2BTError(f"来源自定义骨与工作骨架同名且语义不明：{bone.name}")
                custom.add(bone.name)
                bone = bone.parent
        # Copy the full required ancestry, not unrelated MMD control bones.
        added_names = custom - set(target.data.bones.keys())
        try:
            custom_bones_added = _copy_custom_bones(source, target, sorted(custom), source_targets, None)
            if physics_transfer:
                physics_transfer_report = physics_transfer.transfer_project(source, target,
                    {**source_targets, **{name: name for name in custom}})
        except Exception as exc:
            _discard_copied_bones(target, added_names)
            raise HD2BTError(f"自定义骨/物理项目转移失败，已撤销新增骨且尚未重绑网格：{exc}") from exc
    for mesh in meshes:
        if mesh.type != "MESH":
            continue
        # 默认仍只处理顶点组字典明确列出的组；只有用户主动选择时才沿来源骨父链合并。
        pending = []
        untouched_names = set()
        for group in list(mesh.vertex_groups):
            normalized_name = normalize_bone_name(group.name)
            target_name = weight_lookup.get(normalized_name)
            if target_name is None and not collapse_unmapped:
                source_bone = source_group_bones.get(normalized_name)
                if source_bone is not None:
                    candidate = source_targets.get(source_bone.name, source_bone.name)
                    if candidate in target.data.bones:
                        target_name = candidate
            collapsed_to_parent = False
            if target_name is None and collapse_unmapped:
                source_bone = source_group_bones.get(normalized_name)
                if source_bone is not None:
                    # mmd_tools 可能把可见骨名改为英文 DisplayName；若骨本身有字典
                    # 映射，仍视为普通重命名。只有向父级回溯才计入“父链合并”。
                    target_name = source_targets.get(source_bone.name)
                    if target_name is None and source_bone.parent is not None:
                        target_name = _nearest_destination(
                            source_bone.parent,
                            source_targets,
                            set(),
                            None,
                        )
                        collapsed_to_parent = target_name is not None
            if target_name is None:
                untouched += 1
                untouched_names.add(group.name)
                continue
            if target.data.bones.get(target_name) is None:
                raise HD2BTError(f"顶点组字典目标骨在潜兵骨架中不存在：{target_name}")
            if group.name != target_name:
                pending.append((group, target_name, collapsed_to_parent))
        mesh_renamed, mesh_merged = _apply_group_remap(mesh, pending)
        renamed += mesh_renamed
        merged += mesh_merged
        collapsed += sum(1 for _group, _target, value in pending if value)
        weighted_names = _weighted_group_names(mesh)
        for group in list(mesh.vertex_groups):
            if group.name in target.data.bones or group.name in weighted_names:
                continue
            if normalize_bone_name(group.name) not in source_group_bones:
                continue
            if group.name in untouched_names:
                untouched -= 1
            mesh.vertex_groups.remove(group)
            utility_removed += 1
        # 中文注释：重命名完成后目标组才稳定存在；此时只重新分配四个手臂组的
        # 原总权重，手掌、手指、锁骨以及其他自定义组逐项保持不变。
        arm_vertices_rebalanced += rebalance_hd2_arm_weights(target, [mesh])
        for modifier in mesh.modifiers:
            if modifier.type == "ARMATURE" and modifier.object is source:
                modifier.object = target
                rebound += 1
        if mesh.parent is source:
            world = mesh.matrix_world.copy()
            mesh.parent = target
            mesh.matrix_world = world
        mesh["HD2BT_SourceArmature"] = source.name
        mesh["HD2BT_MappingName"] = profile["name"]
    return {
        "renamed": renamed,
        "merged": merged,
        "rebound": rebound,
        "meshes": len(meshes),
        "untouched": untouched,
        "custom_bones_added": custom_bones_added,
        "physics_transfer": physics_transfer_report,
        "facial_controls_merged": facial_merged if not collapse_unmapped else [],
        "custom_groups_collapsed": collapsed,
        "utility_groups_removed": utility_removed,
        "arm_vertices_rebalanced": arm_vertices_rebalanced,
        "fallback_vertices": 0,
        "limited_vertices": 0,
    }


def inherit_game_export_metadata(target, meshes):
    """Copy Unit/Lod routing metadata from one imported HD2 template mesh.

    The usual workflow ends with one prepared MMD mesh.  When
    that mesh was newly imported rather than created by editing the HD2 mesh,
    it lacks the three routing properties required by the Unit codec.
    """
    keys = ("Z_ObjectID", "MeshInfoIndex", "BoneInfoIndex")
    missing = [mesh for mesh in meshes if any(mesh.get(key) is None for key in keys)]
    if not missing:
        return 0, None
    candidates = []
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj in meshes:
            continue
        bound = obj.parent is target or any(
            modifier.type == "ARMATURE" and modifier.object is target
            for modifier in obj.modifiers
        )
        if bound and all(obj.get(key) is not None for key in keys):
            candidates.append(obj)
    if len(missing) != 1 or len(candidates) != 1:
        return 0, None
    source = candidates[0]
    for key in keys:
        missing[0][key] = source[key]
    source["HD2BT_ExportTemplate"] = True
    return 1, source.name


def find_bound_meshes(source, context):
    selected = [
        obj for obj in context.selected_objects
        if obj.type == "MESH"
        and not obj.get("HD2BT_ReferenceGroup")
        and not obj.get("HD2BT_ExportTemplate")
    ]
    if selected:
        return selected
    result = []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if (
            obj.parent is source
            or any(mod.type == "ARMATURE" and mod.object is source for mod in obj.modifiers)
            or obj.get("HD2BT_SourceArmature") == source.name
        ):
            result.append(obj)
    if not result:
        raise HD2BTError("找不到来源骨架对应的网格；请直接选择需要转换的人物网格")
    return result
