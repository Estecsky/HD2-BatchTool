"""AQSDK 独立保存的确定性权重阻断检查，而非制作规范检查。

先在独立 Mesh 副本运行与 PrepareMesh 相同的 Blender 权重操作，再判断
阈值筛选及最终归一化后能否保存。不把原始 >4 项、未归一化或微小权重
当成失败。目标 Unit 的原生骨表/stream 未知时返回待验证提示，不误选。
"""

import math

EXPORT_WEIGHT_THRESHOLD = 0.001
MAX_INFLUENCES = 4
METADATA_GROUPS = frozenset(("mmd_edge_scale", "mmd_vertex_order"))


def analyze_weights(memberships, group_names, bone_names=None):
    """检查已完成 Blender 预处理的权重；调用者负责面引用/骨表上下文。

    当前 SDK 对保留的正权重执行 normalize_half4，有限正数不论原总和
    多少均可闭合。未知骨名不在这里判错：参考 Unit 也可能提供该骨。
    bone_names 保留为兼容调用参数，不用于猜测最终 Unit 的骨表。
    """
    retained = []
    for index, weight in memberships:
        weight = float(weight)
        if not weight > EXPORT_WEIGHT_THRESHOLD:
            continue  # 与 SDK 一致：零、负数、NaN 不进入最终影响。
        if not 0 <= index < len(group_names):
            return ("失效顶点组索引",)
        retained.append(weight)
        if len(retained) == MAX_INFLUENCES:
            break
    if not retained:
        return ("SDK 预处理后无有效骨骼权重",)
    if not all(math.isfinite(w) for w in retained):
        return ("SDK 保留权重包含非有限值",)
    return ()


def _prepared_issues(obj, bone_names, warnings):
    names = tuple(group.name for group in obj.vertex_groups)
    surface = {i for face in obj.data.polygons for i in face.vertices}
    rows = [(v.index, tuple((g.group, g.weight) for g in v.groups))
            for v in obj.data.vertices]
    # 至少一个可确定映射、被面使用的影响才能证明生成蒙皮流。
    skinned = any(i in surface and any(
        w > EXPORT_WEIGHT_THRESHOLD and 0 <= g < len(names) and names[g] in bone_names
        for g, w in groups) for i, groups in rows)
    unknown = set()
    result = {}
    for index, groups in rows:
        kept = [(g, w) for g, w in groups if w > EXPORT_WEIGHT_THRESHOLD][:MAX_INFLUENCES]
        unresolved = [names[g] for g, _ in kept if 0 <= g < len(names) and names[g] not in bone_names]
        unknown.update(unresolved)
        reasons = analyze_weights(groups, names)
        if unresolved:
            continue  # 数字 hash / 参考 Unit 原生骨可能合法，不猜测 remap。
        if reasons == ("SDK 预处理后无有效骨骼权重",):
            if not skinned or index not in surface:
                continue  # 游离点不参与最终面校验；全静态流需目标上下文。
        if reasons:
            result[index] = reasons
    if unknown:
        warnings.append("以下骨组需目标 Unit 骨表验证，未作为错误点选择：" + "、".join(sorted(unknown)))
    if surface and not skinned and not unknown:
        warnings.append("无法确定目标是否为蒙皮流，未将全网格无权重当成顶点错误")
    return result


def mesh_weight_issues(obj, bone_names, *, context=None, warnings=None):
    """副本权重预处理后返回 {源顶点索引: 原因}，不改变源权重/拓扑。

    独立保存不烘焙 Armature。这里只复制权重处理，不应用修改器或拆边：
    支持范围内拆边/三角化只复制顶点权重，故源索引可直接定位。额外
    修改器/形态键需由调用者拒绝，不对生成顶点猜测原始索引。
    """
    import bpy
    context = context or bpy.context
    warnings = warnings if warnings is not None else []
    previous_active = context.view_layer.objects.active
    previous_selected = list(context.selected_objects)
    previous_mode = context.mode
    copy = None
    data = None
    try:
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        copy = obj.copy()
        data = obj.data.copy()
        copy.data = data
        copy.name = "__HD2BT_WEIGHT_CHECK__"
        copy.animation_data_clear()
        copy.modifiers.clear()
        context.scene.collection.objects.link(copy)
        copy.hide_viewport = False
        copy.hide_select = False
        copy.hide_set(False)
        for selected in context.selected_objects:
            selected.select_set(False)
        copy.select_set(True)
        context.view_layer.objects.active = copy
        for group in list(copy.vertex_groups):
            if group.name in METADATA_GROUPS and group.name not in bone_names:
                copy.vertex_groups.remove(group)
        # PrepareMesh 在处理权重前先在编辑模式选择所有可见点；保留隐藏
        # 状态、锁组与 paint mask，不用数学近似猜测 Blender 的行为。
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
        try:
            bpy.ops.object.vertex_group_limit_total(group_select_mode='ALL', limit=MAX_INFLUENCES)
            bpy.ops.object.vertex_group_normalize_all(lock_active=False)
        except Exception as exc:
            # SDK 同样捕获该块异常并继续；使用此时的真实副本权重。
            warnings.append("Blender 权重预处理未全部执行，按 SDK 的继续处理路径检查：" + str(exc))
        bpy.ops.object.mode_set(mode='OBJECT')
        return _prepared_issues(copy, bone_names, warnings)
    finally:
        if copy is not None:
            if context.object is copy and copy.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.data.objects.remove(copy, do_unlink=True)
        if data is not None and data.users == 0:
            bpy.data.meshes.remove(data)
        for selected in list(context.selected_objects):
            selected.select_set(False)
        for selected in previous_selected:
            selected.select_set(True)
        context.view_layer.objects.active = previous_active
        if previous_mode == 'EDIT_MESH' and previous_active is not None:
            bpy.ops.object.mode_set(mode='EDIT')
