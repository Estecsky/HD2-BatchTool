"""随插件发布的参考骨架加载与场景导入。"""

from contextlib import contextmanager

import bpy

from .constants import (
    AXIS_INITIAL,
    BODY_AXIS_SPACE_PROP,
    FULL_REST_POSE_PROP,
    POSE_INITIAL,
    POSE_SPACE_PROP,
    POSE_SYSTEM_BODY_V1,
    POSE_SYSTEM_PROP,
    POSE_TPOSE,
    REFERENCE_GROUP_PROP,
    REFERENCE_GROUPS,
    RIG_KIND_PROP,
    SOURCE_BONE_PROP,
    SOURCE_REST_LOCAL_PROP,
    THIRD_PERSON,
    WORKING_RIG_PROP,
    reference_blend_path,
)
from .utils import HD2BTError


def _load_objects(names):
    asset_path = reference_blend_path()
    if not asset_path.is_file():
        raise HD2BTError(f"插件参考文件不存在：{asset_path}")
    with bpy.data.libraries.load(str(asset_path), link=False) as (data_from, data_to):
        missing = [name for name in names if name not in data_from.objects]
        if missing:
            raise HD2BTError(f"参考文件缺少对象：{', '.join(missing)}")
        data_to.objects = list(names)
    loaded = [obj for obj in data_to.objects if obj is not None]
    if len(loaded) != len(names):
        raise HD2BTError("参考对象载入不完整")
    return loaded


def import_reference_group(group_key):
    """Append 一个完整参考组；不使用 bpy.ops.wm.append，避免界面上下文依赖。"""
    root_name, child_names = REFERENCE_GROUPS[group_key]
    requested = (root_name,) + tuple(child_names)
    loaded = _load_objects(requested)
    root = loaded[0]

    collection = bpy.data.collections.new(f"HD2Batch_{group_key}")
    bpy.context.scene.collection.children.link(collection)
    collection.hide_viewport = False
    collection.hide_render = True

    kind = THIRD_PERSON
    pose_space = POSE_TPOSE if group_key.endswith("TPOSE") else POSE_INITIAL
    for obj in loaded:
        collection.objects.link(obj)
        obj[REFERENCE_GROUP_PROP] = group_key
        obj.hide_render = True
        obj[WORKING_RIG_PROP] = True
    root[RIG_KIND_PROP] = kind
    root[POSE_SPACE_PROP] = pose_space
    root[FULL_REST_POSE_PROP] = pose_space == POSE_INITIAL
    if kind == THIRD_PERSON and group_key == "TP_INITIAL":
        root[POSE_SYSTEM_PROP] = POSE_SYSTEM_BODY_V1
        root[BODY_AXIS_SPACE_PROP] = AXIS_INITIAL

    for selected in bpy.context.selected_objects:
        selected.select_set(False)
    root.hide_set(False)
    root.select_set(True)
    bpy.context.view_layer.objects.active = root
    return root, loaded, collection

@contextmanager
def temporary_reference_roots(*group_keys):
    """只载入数值计算所需的根骨架，退出时清理临时 datablock。"""
    objects = []
    roots = {}
    try:
        for group_key in group_keys:
            root_name = REFERENCE_GROUPS[group_key][0]
            root = _load_objects((root_name,))[0]
            objects.append(root)
            roots[group_key] = root
        yield roots
    finally:
        for obj in objects:
            data = obj.data if hasattr(obj, "data") else None
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and data.users == 0:
                if isinstance(data, bpy.types.Armature):
                    bpy.data.armatures.remove(data)
                elif isinstance(data, bpy.types.Mesh):
                    bpy.data.meshes.remove(data)


def _local_matrix(bone):
    return (
        bone.matrix_local
        if bone.parent is None
        else bone.parent.matrix_local.inverted_safe() @ bone.matrix_local
    )


def _matrix34_values(matrix):
    return tuple(
        float(matrix[row][column])
        for row in range(3)
        for column in range(4)
    )


def avatar_source_rest_locals(armature):
    """返回工作骨对应的原版 Avatar 局部 Rest，不读取已吸附后的矩阵。

    新工作骨架会把这份不可变基准写入每根骨的自定义属性；旧工程则从插件内置
    INITIAL 参考骨架只读恢复。AQSDK 可用同一接口迁移旧工程而不要求作者重做模型。
    """

    if armature is None or getattr(armature, "type", None) != "ARMATURE":
        raise HD2BTError("独立封包需要有效的潜兵工作骨架")
    source_bones = [
        bone for bone in armature.data.bones
        if bool(bone.get(SOURCE_BONE_PROP, False))
    ]
    if not source_bones:
        raise HD2BTError("工作骨架没有公共 Avatar 骨标记")

    embedded = {
        bone.name: tuple(float(value) for value in bone[SOURCE_REST_LOCAL_PROP])
        for bone in source_bones
        if SOURCE_REST_LOCAL_PROP in bone
    }
    if embedded:
        if len(embedded) != len(source_bones) or any(
            len(values) != 12 for values in embedded.values()
        ):
            raise HD2BTError("工作骨架内置的原版 Avatar Rest 数据不完整")
        return embedded

    with temporary_reference_roots("TP_INITIAL") as roots:
        reference = roots["TP_INITIAL"]
        result = {}
        for bone in source_bones:
            original = reference.data.bones.get(bone.name)
            if original is None:
                raise HD2BTError(f"原版 Avatar 参考骨架缺少：{bone.name}")
            expected_parent = bone.parent.name if bone.parent is not None else None
            original_parent = (
                original.parent.name if original.parent is not None else None
            )
            if expected_parent != original_parent:
                raise HD2BTError(
                    f"工作骨架 {bone.name} 的父级与原版 Avatar 不一致"
                )
            result[bone.name] = _matrix34_values(_local_matrix(original))
        return result
