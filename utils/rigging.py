"""静止姿态烘焙与持握骨求解。

逐骨纯旋转先在 Pose 层计算目标，再把结果写入编辑模式静止骨位和绑定网格。
T-pose 使用对称轴；回初态时先烘焙逆向姿态，再恢复原版初始骨轴。
"""

import json
import math
from contextlib import nullcontext
from functools import lru_cache

import bpy
from mathutils import Matrix, Quaternion, Vector

from .constants import (
    AXIS_INITIAL,
    AXIS_SYMMETRIC,
    BODY_AXIS_SPACE_PROP,
    FIRST_PERSON,
    FULL_REST_POSE_PROP,
    POSE_INITIAL,
    POSE_SPACE_PROP,
    POSE_SYSTEM_BODY_V1,
    POSE_SYSTEM_PROP,
    POSE_TPOSE,
    PROFILE_SCHEMA_VERSION,
    RIG_KIND_PROP,
    THIRD_PERSON,
    body_pose_profile_path,
)
from .reference import temporary_reference_roots
from .utils import HD2BTError, detect_rig_kind


def _unit_object_transform_required(obj):
    scale_error = max(abs(float(value) - 1.0) for value in obj.scale)
    if scale_error > 1e-5:
        raise HD2BTError("骨架对象存在未应用的缩放，请先 Ctrl+A 应用缩放")


def _save_context():
    active = bpy.context.view_layer.objects.active
    selected = list(bpy.context.selected_objects)
    mode = active.mode if active is not None else "OBJECT"
    return active, selected, mode


def _restore_context(state):
    active, selected, mode = state
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for obj in selected:
        if obj.name in bpy.data.objects:
            obj.select_set(True)
    if active is not None and active.name in bpy.data.objects:
        bpy.context.view_layer.objects.active = active
        if mode != "OBJECT" and active.type == "ARMATURE":
            try:
                bpy.ops.object.mode_set(mode=mode)
            except RuntimeError:
                pass


def _layer_collection_paths(layer_collection, target_collections, path=()):
    current_path = (*path, layer_collection)
    paths = []
    if layer_collection.collection in target_collections:
        paths.append(current_path)
    for child in layer_collection.children:
        paths.extend(_layer_collection_paths(child, target_collections, current_path))
    return paths


def _make_object_mode_accessible(obj):
    """临时解除对象及其集合在当前 View Layer 中的隐藏/排除状态。"""
    view_layer = bpy.context.view_layer
    if obj.name not in view_layer.objects:
        raise HD2BTError(f"对象不在当前视图层中，无法处理：{obj.name}")
    collection_states = []
    for collection in obj.users_collection:
        collection_states.append((collection, collection.hide_viewport))
        collection.hide_viewport = False
    layer_states = []
    seen = set()
    for path in _layer_collection_paths(
        view_layer.layer_collection, set(obj.users_collection)
    ):
        for layer_collection in path:
            pointer = layer_collection.as_pointer()
            if pointer in seen:
                continue
            seen.add(pointer)
            layer_states.append(
                (layer_collection, layer_collection.exclude, layer_collection.hide_viewport)
            )
            layer_collection.exclude = False
            layer_collection.hide_viewport = False
    object_state = (obj.hide_viewport, obj.hide_get(view_layer=view_layer))
    obj.hide_viewport = False
    obj.hide_set(False, view_layer=view_layer)
    view_layer.update()
    return collection_states, layer_states, object_state


def _restore_object_visibility(obj, state):
    collection_states, layer_states, object_state = state
    obj.hide_viewport = object_state[0]
    obj.hide_set(object_state[1], view_layer=bpy.context.view_layer)
    for layer_collection, excluded, hidden in reversed(layer_states):
        layer_collection.exclude = excluded
        layer_collection.hide_viewport = hidden
    for collection, hidden in collection_states:
        collection.hide_viewport = hidden
    bpy.context.view_layer.update()


def _bone_depth(bone):
    value = 0
    parent = bone.parent
    while parent is not None:
        value += 1
        parent = parent.parent
    return value


def _capture_deformed_mesh_coordinates(armature, desired_matrices=None):
    """用目标骨架姿态精确求值蒙皮，并返回可写回静止网格/形态键的坐标。"""
    bound_meshes = [
        obj
        for obj in bpy.data.objects
        if obj.type == "MESH"
        and obj.name in bpy.context.view_layer.objects
        and any(
            modifier.type == "ARMATURE" and modifier.object == armature
            for modifier in obj.modifiers
        )
    ]
    if not bound_meshes:
        return {}

    shared_data = {}
    for mesh_obj in bound_meshes:
        owner = shared_data.setdefault(mesh_obj.data.as_pointer(), mesh_obj)
        if owner is not mesh_obj:
            raise HD2BTError(
                f"多个绑定对象共享同一网格数据，无法安全烘焙静止姿态：{owner.name}、{mesh_obj.name}"
            )

    pose_position = armature.data.pose_position
    pose_basis = {
        pose_bone.name: pose_bone.matrix_basis.copy()
        for pose_bone in armature.pose.bones
    }
    captured = {}
    mesh_states = []
    try:
        armature.data.pose_position = "POSE"
        if desired_matrices is not None:
            for bone in sorted(armature.data.bones, key=_bone_depth):
                matrix = desired_matrices.get(bone.name)
                if matrix is not None:
                    parent_matrix = (
                        desired_matrices.get(
                            bone.parent.name,
                            armature.pose.bones[bone.parent.name].matrix,
                        )
                        if bone.parent is not None
                        else Matrix.Identity(4)
                    )
                    parent_matrix_local = (
                        bone.parent.matrix_local
                        if bone.parent is not None
                        else Matrix.Identity(4)
                    )
                    armature.pose.bones[bone.name].matrix_basis = bone.convert_local_to_pose(
                        matrix,
                        bone.matrix_local,
                        parent_matrix=parent_matrix,
                        parent_matrix_local=parent_matrix_local,
                        invert=True,
                    )
        bpy.context.view_layer.update()

        for mesh_obj in bound_meshes:
            visibility_state = _make_object_mode_accessible(mesh_obj)
            modifier_states = [
                (modifier, modifier.show_viewport) for modifier in mesh_obj.modifiers
            ]
            shape_keys = mesh_obj.data.shape_keys
            shape_state = None
            if shape_keys is not None:
                shape_state = (
                    mesh_obj.active_shape_key_index,
                    mesh_obj.show_only_shape_key,
                    [float(block.value) for block in shape_keys.key_blocks],
                )
            mesh_states.append(
                (mesh_obj, visibility_state, modifier_states, shape_state)
            )

            for modifier in mesh_obj.modifiers:
                modifier.show_viewport = bool(
                    modifier.type == "ARMATURE" and modifier.object == armature
                )

            key_coordinates = []
            key_count = len(shape_keys.key_blocks) if shape_keys is not None else 1
            for key_index in range(key_count):
                if shape_keys is not None:
                    mesh_obj.active_shape_key_index = key_index
                    mesh_obj.show_only_shape_key = True
                bpy.context.view_layer.update()
                depsgraph = bpy.context.evaluated_depsgraph_get()
                evaluated_object = mesh_obj.evaluated_get(depsgraph)
                evaluated_mesh = evaluated_object.to_mesh()
                try:
                    if len(evaluated_mesh.vertices) != len(mesh_obj.data.vertices):
                        raise HD2BTError(
                            f"蒙皮求值改变了网格拓扑，无法烘焙静止姿态：{mesh_obj.name}"
                        )
                    key_coordinates.append(
                        [vertex.co.copy() for vertex in evaluated_mesh.vertices]
                    )
                finally:
                    evaluated_object.to_mesh_clear()
            captured[mesh_obj] = key_coordinates
    finally:
        for mesh_obj, visibility_state, modifier_states, shape_state in reversed(mesh_states):
            for modifier, show_viewport in modifier_states:
                modifier.show_viewport = show_viewport
            if shape_state is not None:
                active_index, show_only, values = shape_state
                mesh_obj.active_shape_key_index = active_index
                mesh_obj.show_only_shape_key = show_only
                for block, value in zip(mesh_obj.data.shape_keys.key_blocks, values):
                    block.value = value
            _restore_object_visibility(mesh_obj, visibility_state)
        for pose_bone in armature.pose.bones:
            pose_bone.matrix_basis = pose_basis[pose_bone.name]
        armature.data.pose_position = pose_position
        bpy.context.view_layer.update()
    return captured


def _write_deformed_mesh_coordinates(captured):
    for mesh_obj, key_coordinates in captured.items():
        shape_keys = mesh_obj.data.shape_keys
        if shape_keys is None:
            for vertex, coordinate in zip(mesh_obj.data.vertices, key_coordinates[0]):
                vertex.co = coordinate
        else:
            if len(shape_keys.key_blocks) != len(key_coordinates):
                raise HD2BTError(f"形态键数量在姿态转换期间发生变化：{mesh_obj.name}")
            for key_block, coordinates in zip(shape_keys.key_blocks, key_coordinates):
                for point, coordinate in zip(key_block.data, coordinates):
                    point.co = coordinate
        mesh_obj.data.update()


def apply_bone_object_matrices(obj, desired_matrices, *, preserve_lengths=True):
    """一次进入 Edit Mode 后事务式应用，避免逐骨切换模式导致上下文损坏。"""
    if obj.type != "ARMATURE":
        raise HD2BTError("只能修改骨架对象")
    state = _save_context()
    visibility_state = None
    desired_heads = {
        name: matrix.translation.copy()
        for name, matrix in desired_matrices.items()
        if name in obj.data.bones
    }
    lengths = {name: float(obj.data.bones[name].length) for name in desired_matrices if name in obj.data.bones}
    try:
        if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for selected in bpy.context.selected_objects:
            selected.select_set(False)
        visibility_state = _make_object_mode_accessible(obj)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        # 用户可能在手工吸附时开启过“X 轴镜像”。若保持开启，下面逐骨写入
        # Edit Bone 矩阵会被 Blender 再镜像一次，导致另一侧手臂骨轴被覆盖。
        if obj.data.use_mirror_x:
            obj.data.use_mirror_x = False
        with bpy.context.temp_override(
            active_object=obj,
            object=obj,
            selected_objects=[obj],
            selected_editable_objects=[obj],
        ):
            result = bpy.ops.object.mode_set(mode="EDIT")
        if "FINISHED" not in result:
            raise HD2BTError(f"无法进入骨架编辑模式：{obj.name}")
        for name, matrix in desired_matrices.items():
            edit_bone = obj.data.edit_bones.get(name)
            if edit_bone is None:
                continue
            length = lengths[name]
            edit_bone.matrix = matrix
            if preserve_lengths:
                edit_bone.length = length
        with bpy.context.temp_override(
            active_object=obj,
            object=obj,
            selected_objects=[obj],
            selected_editable_objects=[obj],
        ):
            bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        if visibility_state is not None and obj.mode != "OBJECT":
            try:
                with bpy.context.temp_override(
                    active_object=obj,
                    object=obj,
                    selected_objects=[obj],
                    selected_editable_objects=[obj],
                ):
                    bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError:
                pass
        if visibility_state is not None:
            _restore_object_visibility(obj, visibility_state)
        _restore_context(state)

    # 验证 Edit Bone 实际位置与请求矩阵一致。
    max_head_error = 0.0
    for name, desired_head in desired_heads.items():
        max_head_error = max(
            max_head_error,
            float((obj.data.bones[name].head_local - desired_head).length),
        )
    return max_head_error


@lru_cache(maxsize=1)
def load_body_pose_profile():
    path = body_pose_profile_path()
    if not path.is_file():
        raise HD2BTError(f"身体姿态旋转表不存在：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HD2BTError(f"身体姿态旋转表无法读取：{exc}") from exc
    if payload.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise HD2BTError("身体姿态旋转表版本无效")
    if (
        not payload.get("body_bones")
        or not payload.get("tpose_basis_rotations_wxyz")
        or not payload.get("initial_basis_rotations_from_baked_tpose_wxyz")
    ):
        raise HD2BTError("身体姿态旋转表内容不完整")
    return payload


def _rotation_error(first, second):
    dot = abs(float(first.normalized().dot(second.normalized())))
    dot = max(-1.0, min(1.0, dot))
    return 2.0 * __import__("math").acos(dot)


def _local_bone_rotation(bone):
    matrix = (
        bone.parent.matrix_local.inverted() @ bone.matrix_local
        if bone.parent else bone.matrix_local.copy()
    )
    return matrix.to_quaternion().normalized()


def _restore_reference_hand_axes(obj, role):
    """按当前姿态模板恢复手掌、手腕扭转与手指的完整静止轴。"""

    if role not in {"TP_INITIAL", "TP_TPOSE"}:
        raise HD2BTError(f"未知手部参考姿态：{role}")
    desired = {}
    with temporary_reference_roots(role) as references:
        reference = references[role]
        for bone in obj.data.bones:
            if (
                bone.name not in {"l_hand", "r_hand", "l_hand_twist", "r_hand_twist"}
                and "_finger" not in bone.name
            ):
                continue
            reference_bone = reference.data.bones.get(bone.name)
            if reference_bone is None:
                continue
            reference_rotation = reference_bone.matrix_local.to_quaternion().normalized()
            current_rotation = bone.matrix_local.to_quaternion().normalized()
            if _rotation_error(current_rotation, reference_rotation) <= 1.0e-6:
                continue
            # 中文注释：姿态旋转表是在原模板父子坐标上采样的；独立骨架移动过
            # 各关节 head 后，直接往返会留下最多十余度手轴误差。姿态烘焙完成后
            # 保留人物自己的 head/骨长，只把游戏动画依赖的绝对静止轴收敛回模板。
            desired[bone.name] = Matrix.LocRotScale(
                bone.head_local.copy(),
                reference_rotation,
                Vector((1.0, 1.0, 1.0)),
            )
    if not desired:
        return 0, 0.0
    error = apply_bone_object_matrices(obj, desired, preserve_lengths=True)
    if error > 1.0e-5:
        raise HD2BTError(f"恢复手部静止轴后 head 漂移过大：{error:.6f}")
    return len(desired), error


def detect_body_pose_space(obj):
    if (
        obj.get(POSE_SYSTEM_PROP) == POSE_SYSTEM_BODY_V1
        and obj.get(POSE_SPACE_PROP) in {POSE_INITIAL, POSE_TPOSE}
    ):
        return obj.get(POSE_SPACE_PROP)
    profile = load_body_pose_profile()
    rotations = profile["tpose_basis_rotations_wxyz"]
    initial_error = 0.0
    tpose_error = 0.0
    compared = 0
    identity = Quaternion()
    for name in profile["body_bones"]:
        pose_bone = obj.pose.bones.get(name)
        if pose_bone is None:
            continue
        current = pose_bone.matrix_basis.to_quaternion().normalized()
        expected = Quaternion(rotations[name]) if name in rotations else identity
        initial_error += _rotation_error(current, identity)
        tpose_error += _rotation_error(current, expected)
        compared += 1
    if not compared:
        raise HD2BTError("活动骨架缺少身体姿态旋转表中的骨骼")
    return POSE_TPOSE if tpose_error < initial_error else POSE_INITIAL


def detect_body_axis_space(obj):
    if (
        obj.get(POSE_SYSTEM_PROP) == POSE_SYSTEM_BODY_V1
        and obj.get(BODY_AXIS_SPACE_PROP) in {AXIS_INITIAL, AXIS_SYMMETRIC}
    ):
        return obj.get(BODY_AXIS_SPACE_PROP)
    profile = load_body_pose_profile()
    deltas = profile["symmetric_axis_deltas_wxyz"]
    initial_error = 0.0
    symmetric_error = 0.0
    compared = 0
    with temporary_reference_roots("TP_INITIAL") as refs:
        initial = refs["TP_INITIAL"]
        for name in profile["body_bones"]:
            bone = obj.data.bones.get(name)
            source = initial.data.bones.get(name)
            if bone is None or source is None:
                continue
            current = _local_bone_rotation(bone)
            source_rotation = _local_bone_rotation(source)
            delta = Quaternion(deltas[name]) if name in deltas else Quaternion()
            expected_symmetric = source_rotation @ delta
            expected_symmetric.normalize()
            initial_error += _rotation_error(current, source_rotation)
            symmetric_error += _rotation_error(current, expected_symmetric)
            compared += 1
    if not compared:
        raise HD2BTError("活动骨架缺少身体骨轴旋转表中的骨骼")
    return AXIS_SYMMETRIC if symmetric_error < initial_error else AXIS_INITIAL


def convert_body_axis_space(obj, target_space):
    if target_space not in {AXIS_INITIAL, AXIS_SYMMETRIC}:
        raise HD2BTError(f"未知身体骨轴空间：{target_space}")
    current_space = detect_body_axis_space(obj)
    obj[POSE_SYSTEM_PROP] = POSE_SYSTEM_BODY_V1
    if current_space == target_space:
        obj[BODY_AXIS_SPACE_PROP] = target_space
        return 0, 0.0

    profile = load_body_pose_profile()
    deltas = profile["symmetric_axis_deltas_wxyz"]
    body_names = set(profile["body_bones"])
    desired = {}
    new_object_rotations = {}
    changed = 0
    for bone in sorted(
        (bone for bone in obj.data.bones if bone.name in body_names),
        key=_bone_depth,
    ):
        current_object_rotation = bone.matrix_local.to_quaternion().normalized()
        if bone.parent is None:
            current_local_rotation = current_object_rotation.copy()
        else:
            parent_current = bone.parent.matrix_local.to_quaternion().normalized()
            current_local_rotation = parent_current.inverted() @ current_object_rotation
            current_local_rotation.normalize()
        delta = Quaternion(deltas[bone.name]) if bone.name in deltas else Quaternion()
        if target_space == AXIS_INITIAL:
            delta.invert()
        new_local_rotation = current_local_rotation @ delta
        new_local_rotation.normalize()
        if bone.parent is not None and bone.parent.name in new_object_rotations:
            new_object_rotation = new_object_rotations[bone.parent.name] @ new_local_rotation
        elif bone.parent is not None:
            new_object_rotation = (
                bone.parent.matrix_local.to_quaternion().normalized() @ new_local_rotation
            )
        else:
            new_object_rotation = new_local_rotation
        new_object_rotation.normalize()
        new_object_rotations[bone.name] = new_object_rotation
        desired[bone.name] = Matrix.LocRotScale(
            bone.head_local.copy(), new_object_rotation, Vector((1.0, 1.0, 1.0))
        )
        if delta.angle > 1e-5:
            changed += 1
    error = apply_bone_object_matrices(obj, desired, preserve_lengths=True)
    if error > 1e-5:
        raise HD2BTError(f"身体骨轴转换后 head 漂移过大：{error:.6f}")
    obj[BODY_AXIS_SPACE_PROP] = target_space
    obj[POSE_SYSTEM_PROP] = POSE_SYSTEM_BODY_V1
    return changed, error


def _body_pose_basis_is_identity(obj, body_names, tolerance=1e-5):
    identity = Quaternion()
    for name in body_names:
        pose_bone = obj.pose.bones.get(name)
        if pose_bone is None:
            continue
        if _rotation_error(pose_bone.matrix_basis.to_quaternion(), identity) > tolerance:
            return False
        if pose_bone.matrix_basis.translation.length > tolerance:
            return False
        if max(abs(float(value) - 1.0) for value in pose_bone.matrix_basis.to_scale()) > tolerance:
            return False
    return True


def _clear_body_pose(obj, body_names):
    for name in body_names:
        pose_bone = obj.pose.bones.get(name)
        if pose_bone is not None:
            pose_bone.matrix_basis = Matrix.Identity(4)


def _apply_body_pose_rotations(obj, body_names, rotations):
    _clear_body_pose(obj, body_names)
    changed = 0
    for name, values in rotations.items():
        pose_bone = obj.pose.bones.get(name)
        if pose_bone is None or name not in body_names:
            continue
        pose_bone.matrix_basis = Quaternion(values).to_matrix().to_4x4()
        changed += 1
    obj.data.pose_position = "POSE"
    bpy.context.view_layer.update()
    return changed


def _bake_body_pose_to_rest(obj, body_names, *, extra_names=(), transport=None):
    """把身体 Pose 烘焙为编辑模式静止骨位，并同步烘焙所有绑定网格。

    非身体骨的 matrix_basis 在求值期间临时清零，避免 attach/weapon/camera 等
    用户控制骨被重复烘焙；操作完成后逐项恢复。
    """
    body_names = set(body_names)
    control_pose = {
        pose_bone.name: pose_bone.matrix_basis.copy()
        for pose_bone in obj.pose.bones
        if pose_bone.name not in body_names
    }
    captured = None
    desired_matrices = None
    error = 0.0
    try:
        for name in control_pose:
            obj.pose.bones[name].matrix_basis = Matrix.Identity(4)
        obj.data.pose_position = "POSE"
        bpy.context.view_layer.update()

        # 网格求值包含额外骨随身体父骨的运动；它们必须用同一次求值
        # 写入 Rest，不能仅烘焙网格而把袖子/头发/辅助末端留在旧位置。
        bake_names = body_names | set(extra_names)
        desired_matrices = {
            name: obj.pose.bones[name].matrix.copy()
            for name in bake_names
            if obj.pose.bones.get(name) is not None
        }
        if transport is not None:
            for bone in obj.data.bones:
                # 去掉烘焙前的纯骨轴调整，只传递真正的姿态运动。
                transport[bone.name] = (
                    obj.pose.bones[bone.name].matrix
                    @ bone.matrix_local.inverted() @ transport[bone.name]
                )
        for name, matrix in desired_matrices.items():
            if not all(math.isfinite(v) for row in matrix for v in row):
                raise HD2BTError(f"姿态烘焙产生非有限骨矩阵：{name}")
        captured = _capture_deformed_mesh_coordinates(obj)
        error = apply_bone_object_matrices(
            obj,
            desired_matrices,
            preserve_lengths=True,
        )
        _clear_body_pose(obj, body_names)
    finally:
        for name, matrix in control_pose.items():
            pose_bone = obj.pose.bones.get(name)
            if pose_bone is not None:
                pose_bone.matrix_basis = matrix
        obj.data.pose_position = "POSE"
        bpy.context.view_layer.update()

    if captured is not None:
        _write_deformed_mesh_coordinates(captured)
    bpy.context.view_layer.update()
    return len(desired_matrices or ()), error


def _convert_pose_space(obj, direction, kind=None, *, extra_names=(), transport=None):
    """用逐骨纯旋转驱动静止骨架/网格的破坏性姿态烘焙。"""
    kind = kind or detect_rig_kind(obj)
    if kind == FIRST_PERSON:
        raise HD2BTError("第一人称不使用第三人称身体 Pose 旋转表")
    if direction not in {"TO_TPOSE", "TO_INITIAL"}:
        raise HD2BTError(f"未知姿态转换方向：{direction}")
    profile = load_body_pose_profile()
    body_names = set(profile["body_bones"])
    current_pose = detect_body_pose_space(obj)
    current_axis = detect_body_axis_space(obj)
    body_pose_is_identity = _body_pose_basis_is_identity(obj, body_names)
    changed = 0
    error = 0.0

    if direction == "TO_TPOSE":
        if current_pose == POSE_TPOSE and body_pose_is_identity:
            return _restore_reference_hand_axes(obj, "TP_TPOSE")
        if current_pose == POSE_TPOSE:
            # 迁移 0.2.x 保存的“对称初态静止骨架 + 临时 T-pose Pose”。
            changed, error = _bake_body_pose_to_rest(obj, body_names, extra_names=extra_names, transport=transport)
        else:
            _clear_body_pose(obj, body_names)
            convert_body_axis_space(obj, AXIS_SYMMETRIC)
            _apply_body_pose_rotations(
                obj,
                body_names,
                profile["tpose_basis_rotations_wxyz"],
            )
            changed, error = _bake_body_pose_to_rest(obj, body_names, extra_names=extra_names, transport=transport)
        obj[POSE_SPACE_PROP] = POSE_TPOSE
        obj[BODY_AXIS_SPACE_PROP] = AXIS_SYMMETRIC
        obj[FULL_REST_POSE_PROP] = False
    else:
        if current_pose == POSE_INITIAL and current_axis == AXIS_INITIAL and body_pose_is_identity:
            return _restore_reference_hand_axes(obj, "TP_INITIAL")
        if current_pose == POSE_TPOSE and body_pose_is_identity:
            _apply_body_pose_rotations(
                obj,
                body_names,
                profile["initial_basis_rotations_from_baked_tpose_wxyz"],
            )
            changed, error = _bake_body_pose_to_rest(obj, body_names, extra_names=extra_names, transport=transport)
            obj[BODY_AXIS_SPACE_PROP] = AXIS_SYMMETRIC
        else:
            # 0.2.x 的临时 Pose 或已清 Pose/仍保留对称轴的工程无需再次烘焙网格。
            _clear_body_pose(obj, body_names)
            obj[BODY_AXIS_SPACE_PROP] = current_axis
        axis_changed, axis_error = convert_body_axis_space(obj, AXIS_INITIAL)
        changed += axis_changed
        error = max(error, axis_error)
        obj[POSE_SPACE_PROP] = POSE_INITIAL
        obj[BODY_AXIS_SPACE_PROP] = AXIS_INITIAL
        obj[FULL_REST_POSE_PROP] = True

    # 中文注释：无论从哪条兼容分支完成转换，最终都按“转换后的当前姿态”收敛
    # 手部绝对静止轴；这一步也让 INITIAL→TPOSE→INITIAL 往返保持可逆。
    hand_role = "TP_TPOSE" if direction == "TO_TPOSE" else "TP_INITIAL"
    hand_changed, hand_error = _restore_reference_hand_axes(obj, hand_role)
    changed += hand_changed
    error = max(error, hand_error)

    obj[POSE_SYSTEM_PROP] = POSE_SYSTEM_BODY_V1
    obj[RIG_KIND_PROP] = kind
    bpy.context.view_layer.update()
    return changed, error


def convert_pose_space(obj, direction, kind=None):
    """Bake body + authored extra bones; rebase physics and roll back on failure."""
    if direction == "TO_INITIAL":
        # 在任何骨架、网格或物理状态改写前检查全部绑定模型；隐藏、未选中、
        # 被排除的集合也不能漏检。值为 0 或仅剩 Basis 仍代表未应用形态键。
        keyed_meshes = sorted(
            mesh.name for mesh in bpy.data.objects
            if mesh.type == 'MESH' and mesh.data.shape_keys is not None
            and (mesh.parent == obj or any(
                modifier.type == 'ARMATURE' and modifier.object == obj
                for modifier in mesh.modifiers
            ))
        )
        if keyed_meshes:
            raise HD2BTError(
                "转回初态 + 原始骨轴已取消：绑定模型存在未应用的形态键："
                + "、".join(f"“{name}”" for name in keyed_meshes)
                + "。请先应用或移除形态键后重试（包括 Basis、值为 0 的形态键）。"
            )
    kind = kind or detect_rig_kind(obj)
    if kind == FIRST_PERSON:
        raise HD2BTError("第一人称不使用第三人称身体 Pose 旋转表")
    if direction not in {"TO_TPOSE", "TO_INITIAL"}:
        raise HD2BTError(f"未知姿态转换方向：{direction}")
    if obj.data.users != 1:
        raise HD2BTError("姿态转换需要单用户骨架数据，请先在副本中独立化骨架")
    physics = None
    project = getattr(obj.data, "hd2pb_project", None)
    if project is not None and project.schema_version:
        from .addon_bridge import companion_module
        rest_pose = companion_module("PHYSBONE", "physbone.blender.rest_pose", error_type=HD2BTError)
        physics = rest_pose.RestPoseEdit(obj)
    body_names = set(load_body_pose_profile()['body_bones'])
    extra_names = {b.name for b in obj.data.bones if b.get('HD2BT_CustomBone')}
    if physics:
        extra_names.update(physics.required_bones)
    meshes = [mesh for mesh in bpy.data.objects if mesh.type == 'MESH'
              and mesh.name in bpy.context.view_layer.objects
              and any(m.type == 'ARMATURE' and m.object == obj for m in mesh.modifiers)]
    # 用户手工新增但未打标签的加权骨也必须同步；控制骨不无差别烘焙。
    for mesh in meshes:
        group_names = {g.index: g.name for g in mesh.vertex_groups}
        extra_names.update(group_names[g.group] for v in mesh.data.vertices for g in v.groups
                           if g.weight > 1e-8 and group_names[g.group] in obj.data.bones)
    for name in tuple(extra_names):
        bone = obj.data.bones.get(name)
        while bone is not None and bone.name not in body_names:
            extra_names.add(bone.name)
            bone = bone.parent
    extra_names.difference_update(body_names)
    rest = {b.name: b.matrix_local.copy() for b in obj.data.bones}
    basis = {b.name: b.matrix_basis.copy() for b in obj.pose.bones}
    pose_position, mirror = obj.data.pose_position, obj.data.use_mirror_x
    keys = (POSE_SPACE_PROP, BODY_AXIS_SPACE_PROP, FULL_REST_POSE_PROP, POSE_SYSTEM_PROP, RIG_KIND_PROP)
    props = {key: obj[key] for key in keys if key in obj}
    coordinates = {mesh: [[v.co.copy() for v in block.data] for block in mesh.data.shape_keys.key_blocks]
                   if mesh.data.shape_keys else [[v.co.copy() for v in mesh.data.vertices]] for mesh in meshes}
    transport = {name: matrix.copy() for name, matrix in rest.items()}
    with physics.suspend_updates() if physics else nullcontext():
        try:
            result = _convert_pose_space(obj, direction, kind, extra_names=extra_names, transport=transport)
            if physics:
                physics.apply(transport)
            return result
        except Exception:
            apply_bone_object_matrices(obj, rest, preserve_lengths=True)
            _write_deformed_mesh_coordinates(coordinates)
            for name, matrix in basis.items():
                obj.pose.bones[name].matrix_basis = matrix
            for key in keys:
                if key in props:
                    obj[key] = props[key]
                elif key in obj:
                    del obj[key]
            obj.data.pose_position = pose_position
            obj.data.use_mirror_x = mirror
            if physics:
                physics.restore()
            bpy.context.view_layer.update()
            raise


def _palm_frame(rig, side):
    hand_name = f"{side}_hand"
    landmark_names = [
        f"{side}_index_finger1",
        f"{side}_middle_finger1",
        f"{side}_ring_finger1",
        f"{side}_pinky_finger1",
    ]
    hand = rig.data.bones.get(hand_name)
    landmarks = [rig.data.bones.get(name) for name in landmark_names]
    if hand is None or any(bone is None for bone in landmarks):
        raise HD2BTError(f"{hand_name} 缺少手掌定位骨，无法计算持握骨位置")
    origin = hand.head_local.copy()
    center = sum((bone.head_local for bone in landmarks), Vector()) / len(landmarks)
    forward = center - origin
    width = landmarks[0].head_local - landmarks[-1].head_local
    forward_length = forward.length
    width_length = width.length
    if forward_length <= 1e-6 or width_length <= 1e-6:
        raise HD2BTError(f"{hand_name} 手掌定位骨退化")
    forward.normalize()
    width -= forward * width.dot(forward)
    width.normalize()
    normal = forward.cross(width)
    normal.normalize()
    width = normal.cross(forward)
    width.normalize()
    rotation = Matrix((forward, width, normal)).transposed()
    return origin, rotation, forward_length, width_length


def solve_grip_bones(obj, *, adjust_rotation=False):
    """根据手掌局部坐标与比例传递第三人称持握骨，返回每根骨的位移。"""
    if detect_rig_kind(obj) != THIRD_PERSON:
        raise HD2BTError("第一人称 attach_hand 骨是控制骨而非手骨子级，持握骨修正仅支持第三人称")
    with temporary_reference_roots("TP_INITIAL") as refs:
        reference = refs["TP_INITIAL"]
        desired = {}
        displacements = {}
        desired_attach_r = None
        for side in ("l", "r"):
            attach_name = f"attach_hand_{side}"
            ref_attach = reference.data.bones.get(attach_name)
            current_attach = obj.data.bones.get(attach_name)
            if ref_attach is None or current_attach is None:
                raise HD2BTError(f"缺少骨骼：{attach_name}")
            ref_origin, ref_frame, ref_forward, ref_width = _palm_frame(reference, side)
            dst_origin, dst_frame, dst_forward, dst_width = _palm_frame(obj, side)
            ref_offset = ref_frame.transposed() @ (ref_attach.head_local - ref_origin)
            scales = Vector((
                dst_forward / ref_forward,
                dst_width / ref_width,
                (dst_forward * dst_width / (ref_forward * ref_width)) ** 0.5,
            ))
            scaled_offset = Vector((
                ref_offset.x * scales.x,
                ref_offset.y * scales.y,
                ref_offset.z * scales.z,
            ))
            new_head = dst_origin + dst_frame @ scaled_offset
            if adjust_rotation:
                ref_rotation = ref_attach.matrix_local.to_3x3()
                relative_rotation = ref_frame.transposed() @ ref_rotation
                new_rotation = (dst_frame @ relative_rotation).to_quaternion()
            else:
                new_rotation = current_attach.matrix_local.to_quaternion()
            new_matrix = Matrix.LocRotScale(new_head, new_rotation, Vector((1.0, 1.0, 1.0)))
            desired[attach_name] = new_matrix
            displacements[attach_name] = float((new_head - current_attach.head_local).length)
            if side == "r":
                desired_attach_r = new_matrix

        ref_attach_r = reference.data.bones["attach_hand_r"]
        ref_aim = reference.data.bones.get("weapon_aim")
        current_aim = obj.data.bones.get("weapon_aim")
        # 精简工作骨架按作者示例删除了 weapon_aim。它存在时继续保持旧行为；
        # 任一侧不存在时只修正双手挂点，不因可选辅助骨中止整条工作流。
        if ref_aim is not None and current_aim is not None:
            aim_local = ref_attach_r.matrix_local.inverted() @ ref_aim.matrix_local
            aim_full = desired_attach_r @ aim_local
            aim_rotation = (
                aim_full.to_quaternion()
                if adjust_rotation
                else current_aim.matrix_local.to_quaternion()
            )
            aim_matrix = Matrix.LocRotScale(
                aim_full.translation, aim_rotation, Vector((1.0, 1.0, 1.0))
            )
            desired["weapon_aim"] = aim_matrix
            displacements["weapon_aim"] = float(
                (aim_full.translation - current_aim.head_local).length
            )

    apply_bone_object_matrices(obj, desired, preserve_lengths=True)
    return displacements
