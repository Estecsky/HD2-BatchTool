"""人物身体七部位切割与部位指定。

自动切割不是无限平面切割：先看真实顶点权重，再限制在对应肢体附近，最后
按网格连通块纠正头发、裙子和宽大衣服。这能避免大头被“手臂平面”误切，
也避免一件独立的大衣在腰线处只切掉一半。头/躯干混合的连续皮肤会在颈部
切线上真正二等分，不再沿原三角面投票生成锯齿。自动切割会直接指定七部位；手动
模块只接收用户已经切好的网格并写入同一套部位标记，不存储也不封包。
"""

from __future__ import annotations

import re
import math
from collections import Counter, defaultdict, deque

import bmesh
import bpy
from mathutils import Vector

from ..utils.utils import HD2BTError
from ..core.normal_policy import DEFAULT_WARNING, DEFAULT_LIMIT, validate_thresholds, classify_error


VISIBLE_PARTS = ("Head", "LeftArm", "RightArm", "Torso", "Hips", "LeftLeg", "RightLeg")
ALL_PARTS = VISIBLE_PARTS
# 手臂切点取肩关节到肘关节的 22%。使用骨段比例而非固定米数，能自动适配不同体型；
# 肩窝与腋下过渡面保留在躯干，避免旧版仅按权重多数把切口吃进胸肩。
ARM_CUT_PROGRESS = 0.22
# 头部切线位于 neck -> head 骨段的 30%。颈根保留给上躯干，脸、头发与帽饰归入
# 独立头部槽位；使用骨段比例可以适配不同身高与头身比，不依赖固定世界坐标。
HEAD_CUT_PROGRESS = 0.30
# 独立披风、长饰带常带少量肩骨权重，但并不沿手臂延伸。只有 90% 顶点仍落在手臂
# 骨轴附近的连通块才允许整体归手臂，避免披风随 LeftArm/RightArm 部件被切走。
ARM_ACCESSORY_RADIUS_FACTOR = 0.65
# 旧 0.001 阻断值现在作为建议警告值，较小量化误差不再取消整个切割。
# 单位向量差长度为 2*sin(夹角/2)，不是角度或长度单位。
CUT_NORMAL_ERROR_TOLERANCE = DEFAULT_LIMIT
PART_LABELS = {
    "Head": "头部（头盔）",
    "LeftArm": "左臂",
    "RightArm": "右臂",
    "Torso": "上躯干",
    "Hips": "下躯干",
    "LeftLeg": "左腿（含膝）",
    "RightLeg": "右腿（含膝）",
}

_ARM_TERMS = (
    "arm", "shoulder", "elbow", "hand", "wrist", "finger", "thumb", "pinky",
    "clavicle", "forearm", "腕", "肘", "肩", "手", "指", "腕", "ひじ",
)
_LEG_TERMS = (
    "leg", "thigh", "knee", "ankle", "foot", "toe", "足", "腿", "膝", "ひざ",
)
_DISTAL_LEG_TERMS = ("knee", "ankle", "foot", "toe", "膝", "ひざ", "足首")
_HEAD_TERMS = (
    "head", "face", "jaw", "eye", "hair", "forehead", "帽", "頭", "头", "脸",
    "顎", "あご", "目", "髪", "发", "髮",
)
_TORSO_TERMS = (
    "neck", "chest", "spine", "upperbody", "upper_body", "首", "上半身", "胸", "脊",
)
_HIP_TERMS = ("hips", "hip", "pelvis", "lowerbody", "lower_body", "下半身", "腰", "裙")


def _normalized(name):
    return re.sub(r"[\s.\-]+", "_", str(name).strip().casefold())


def _side(name):
    raw = str(name).strip()
    key = _normalized(raw)
    if raw.startswith(("左", "左")) or key.startswith("l_") or key.endswith("_l") or "left" in key:
        return "L"
    if raw.startswith(("右", "右")) or key.startswith("r_") or key.endswith("_r") or "right" in key:
        return "R"
    return None


def _group_class(name):
    key = _normalized(name)
    side = _side(name)
    if any(term in key for term in _ARM_TERMS) and side:
        return f"{side}_ARM"
    if any(term in key for term in _LEG_TERMS) and side:
        return f"{side}_LEG"
    if any(term in key for term in _HEAD_TERMS):
        return "HEAD"
    if any(term in key for term in _TORSO_TERMS):
        return "TORSO"
    if any(term in key for term in _HIP_TERMS):
        return "HIPS"
    return None


def _is_distal_leg_group(name):
    key = _normalized(name)
    return any(term in key for term in _DISTAL_LEG_TERMS)


def _bone_point_local(target, mesh, names, fallback=None):
    for name in names:
        bone = target.data.bones.get(name)
        if bone is not None:
            return mesh.matrix_world.inverted() @ (target.matrix_world @ bone.head_local)
    return fallback


def _skeleton_guides(target, mesh):
    hips = _bone_point_local(target, mesh, ("hips", "Hip"), Vector((0.0, 0.0, 0.0)))
    spine = _bone_point_local(target, mesh, ("spine1", "spine2", "Spine"), hips + Vector((0, 0, 1)))
    body_axis = spine - hips
    if body_axis.length_squared <= 1.0e-12:
        body_axis = Vector((0.0, 0.0, 1.0))
    neck = _bone_point_local(target, mesh, ("neck", "Neck"), spine + body_axis * 0.45)
    head = _bone_point_local(target, mesh, ("head", "Head"), neck + body_axis * 0.35)
    guides = {"hips": hips, "spine": spine, "neck": neck, "head": head}
    for side, prefix in (("L", "l"), ("R", "r")):
        guides[f"{side}_shoulder"] = _bone_point_local(
            target, mesh, (f"{prefix}_shoulder", f"{prefix}_clavicle"), spine
        )
        guides[f"{side}_hand"] = _bone_point_local(
            target, mesh, (f"{prefix}_hand",), guides[f"{side}_shoulder"]
        )
        guides[f"{side}_elbow"] = _bone_point_local(
            target,
            mesh,
            (f"{prefix}_elbow",),
            guides[f"{side}_shoulder"]
            + (guides[f"{side}_hand"] - guides[f"{side}_shoulder"]) * 0.52,
        )
        guides[f"{side}_thigh"] = _bone_point_local(
            target, mesh, (f"{prefix}_thigh", f"{prefix}_leg"), hips
        )
        guides[f"{side}_knee"] = _bone_point_local(
            target, mesh, (f"{prefix}_knee",), guides[f"{side}_thigh"] + Vector((0, 0, -1))
        )
    return guides


def _vertex_scores(mesh, vertex):
    groups = {group.index: group.name for group in mesh.vertex_groups}
    scores = defaultdict(float)
    distal = {"L": 0.0, "R": 0.0}
    total = 0.0
    for membership in vertex.groups:
        if membership.weight <= 1.0e-8:
            continue
        name = groups.get(membership.group)
        if name is None:
            continue
        total += float(membership.weight)
        category = _group_class(name)
        if category:
            scores[category] += float(membership.weight)
        side = _side(name)
        if side and _is_distal_leg_group(name):
            distal[side] += float(membership.weight)
    return scores, distal, total


def _leg_progress(position, thigh, knee):
    axis = knee - thigh
    length_squared = axis.length_squared
    if length_squared <= 1.0e-12:
        return 1.0
    return (position - thigh).dot(axis) / length_squared


def _arm_progress(position, shoulder, elbow):
    axis = elbow - shoulder
    length_squared = axis.length_squared
    if length_squared <= 1.0e-12:
        return 1.0
    return (position - shoulder).dot(axis) / length_squared


def _head_progress(position, neck, head):
    axis = head - neck
    length_squared = axis.length_squared
    if length_squared <= 1.0e-12:
        return 1.0
    return (position - neck).dot(axis) / length_squared


def _distance_to_axis(position, start, end):
    axis = end - start
    if axis.length_squared <= 1.0e-12:
        return (position - start).length
    progress = (position - start).dot(axis) / axis.length_squared
    return (position - (start + axis * progress)).length


def _initial_vertex_labels(mesh, target):
    guides = _skeleton_guides(target, mesh)
    waist_axis = guides["spine"] - guides["hips"]
    waist_length = max(waist_axis.length, 1.0e-8)
    waist_normal = waist_axis / waist_length
    labels = []
    confidences = []
    for vertex in mesh.data.vertices:
        scores, distal, total = _vertex_scores(mesh, vertex)
        best = max(scores, key=scores.get) if scores else None
        confidence = scores.get(best, 0.0) / max(total, 1.0e-8) if best else 0.0
        if best == "L_ARM":
            progress = _arm_progress(vertex.co, guides["L_shoulder"], guides["L_elbow"])
            label = "LeftArm" if progress >= ARM_CUT_PROGRESS else "Torso"
        elif best == "R_ARM":
            progress = _arm_progress(vertex.co, guides["R_shoulder"], guides["R_elbow"])
            label = "RightArm" if progress >= ARM_CUT_PROGRESS else "Torso"
        elif best in {"L_LEG", "R_LEG"}:
            side = best[0]
            progress = _leg_progress(
                vertex.co, guides[f"{side}_thigh"], guides[f"{side}_knee"]
            )
            # 大腿中部偏下一点：沿大腿到膝盖方向 58% 处进入腿 Unit。
            label = f"{'Left' if side == 'L' else 'Right'}Leg" if (
                progress >= 0.58 or distal[side] >= 0.20
            ) else "Hips"
        elif best == "HEAD":
            label = "Head"
        elif best == "TORSO":
            label = "Torso"
        elif best == "HIPS":
            label = "Hips"
        else:
            # 没有可识别权重的头发、帽子也可按颈骨到头骨的相对高度进入头盔槽；
            # 先判头部再判腰线，避免大头或高发型继续被合并进胸部。
            if _head_progress(vertex.co, guides["neck"], guides["head"]) >= HEAD_CUT_PROGRESS:
                label = "Head"
            else:
                height = (vertex.co - guides["hips"]).dot(waist_normal) / waist_length
                label = "Torso" if height >= 0.28 else "Hips"
            confidence = 0.05
        labels.append(label)
        confidences.append(confidence)
    return labels, confidences


def _connected_components(mesh):
    neighbors = [[] for _ in mesh.data.vertices]
    for edge in mesh.data.edges:
        first, second = edge.vertices
        neighbors[first].append(second)
        neighbors[second].append(first)
    remaining = set(range(len(neighbors)))
    components = []
    while remaining:
        start = remaining.pop()
        queue = deque([start])
        component = [start]
        while queue:
            current = queue.popleft()
            for neighbor in neighbors[current]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    queue.append(neighbor)
                    component.append(neighbor)
        components.append(component)
    return components


def _stabilize_accessory_components(mesh, labels, confidences, guides):
    """独立衣物/头发整体归槽，避免在腰或手臂边界被切成碎片。"""

    vertex_count = max(1, len(mesh.data.vertices))
    for component in _connected_components(mesh):
        # 最大连通块通常是身体本体，保留逐点切割；其余配件按可靠权重多数整体归槽。
        if len(component) >= vertex_count * 0.55:
            continue
        votes = Counter()
        for index in component:
            votes[labels[index]] += max(0.10, confidences[index])
        if not votes:
            continue
        winner, score = votes.most_common(1)[0]
        total = sum(votes.values())
        if score / max(total, 1.0e-8) >= 0.52:
            if winner in {"LeftArm", "RightArm"}:
                side = "L" if winner == "LeftArm" else "R"
                shoulder = guides[f"{side}_shoulder"]
                hand = guides[f"{side}_hand"]
                arm_length = max((hand - shoulder).length, 1.0e-8)
                distances = sorted(
                    _distance_to_axis(mesh.data.vertices[index].co, shoulder, hand)
                    for index in component
                )
                p90 = distances[int((len(distances) - 1) * 0.90)]
                if p90 > arm_length * ARM_ACCESSORY_RADIUS_FACTOR:
                    winner = "Torso"
            for index in component:
                labels[index] = winner


def _smooth_face_labels(mesh, labels):
    """只消除被邻面包围的单面尖刺，不移动主体切线。"""

    edge_faces = defaultdict(list)
    for polygon in mesh.data.polygons:
        for edge_key in polygon.edge_keys:
            edge_faces[tuple(sorted(edge_key))].append(polygon.index)
    neighbors = [set() for _polygon in mesh.data.polygons]
    for faces in edge_faces.values():
        if len(faces) == 2:
            first, second = faces
            neighbors[first].add(second)
            neighbors[second].add(first)
    result = list(labels)
    for _pass in range(2):
        updated = list(result)
        for index, adjacent in enumerate(neighbors):
            if len(adjacent) < 2:
                continue
            votes = Counter(result[neighbor] for neighbor in adjacent)
            winner, count = votes.most_common(1)[0]
            if winner != result[index] and count >= max(2, len(adjacent) - 1):
                updated[index] = winner
        result = updated
    return result


def _prepare_flat_head_cut(mesh, target, face_labels):
    """整理头/躯干连通片，并记录必须在颈部切线上二等分的原面。"""

    labels = list(face_labels)
    guides = _skeleton_guides(target, mesh)
    axis = guides["head"] - guides["neck"]
    if axis.length_squared <= 1.0e-12:
        return labels, None
    plane_co = guides["neck"] + axis * HEAD_CUT_PROGRESS
    plane_no = axis.normalized()
    distance_epsilon = max(axis.length * 1.0e-5, 1.0e-7)

    components = _connected_components(mesh)
    component_by_vertex = {}
    for component_index, vertices in enumerate(components):
        for vertex_index in vertices:
            component_by_vertex[vertex_index] = component_index
    faces_by_component = defaultdict(list)
    for polygon in mesh.data.polygons:
        faces_by_component[component_by_vertex[polygon.vertices[0]]].append(polygon.index)

    planar_face_indices = set()
    for component_index, face_indices in faces_by_component.items():
        relevant = {
            labels[index]
            for index in face_indices
            if labels[index] in {"Head", "Torso"}
        }
        if relevant != {"Head", "Torso"}:
            # 中文注释：全头发、全领饰等已有稳定归属的配件保持整块，不因穿过
            # 几何平面就被横切；只处理权重投票已经把同一表面撕成两类的情况。
            continue
        distances = [
            (mesh.data.vertices[index].co - plane_co).dot(plane_no)
            for index in components[component_index]
        ]
        if min(distances) >= -distance_epsilon:
            # 整个连通片都在切线上方时，权重多数造成的内部锯齿没有几何意义。
            for face_index in face_indices:
                if labels[face_index] in {"Head", "Torso"}:
                    labels[face_index] = "Head"
        elif max(distances) <= distance_epsilon:
            for face_index in face_indices:
                if labels[face_index] in {"Head", "Torso"}:
                    labels[face_index] = "Torso"
        else:
            # 真正跨过切线的连续表面稍后在 BMesh 中切开。记录完整连通片，保证
            # bisect_plane 拿到闭合邻接；非 Head/Torso 面仍按原标签保留。
            planar_face_indices.update(face_indices)

    if not planar_face_indices:
        return labels, None
    return labels, {
        "plane_co": plane_co,
        "plane_no": plane_no,
        "distance_epsilon": distance_epsilon,
        "face_indices": frozenset(planar_face_indices),
    }


def _face_labels(mesh, target):
    labels, confidences = _initial_vertex_labels(mesh, target)
    guides = _skeleton_guides(target, mesh)
    _stabilize_accessory_components(mesh, labels, confidences, guides)
    result = []
    for polygon in mesh.data.polygons:
        votes = Counter(labels[index] for index in polygon.vertices)
        winner = votes.most_common(1)[0][0]
        if winner in {"LeftArm", "RightArm"}:
            side = "L" if winner == "LeftArm" else "R"
            center = sum(
                (mesh.data.vertices[index].co for index in polygon.vertices),
                Vector((0.0, 0.0, 0.0)),
            ) / len(polygon.vertices)
            # 连通块稳定器可能把宽袖整块归到手臂；最终仍以面中心做一次骨段硬约束，
            # 防止袖口/肩饰的整体投票把切线重新拖回肩窝甚至胸部。
            arm_length = max(
                (guides[f"{side}_hand"] - guides[f"{side}_shoulder"]).length,
                1.0e-8,
            )
            if _arm_progress(
                center,
                guides[f"{side}_shoulder"],
                guides[f"{side}_elbow"],
            ) < ARM_CUT_PROGRESS or _distance_to_axis(
                center,
                guides[f"{side}_shoulder"],
                guides[f"{side}_hand"],
            ) > arm_length * 0.75:
                winner = "Torso"
        result.append(winner)
    return _smooth_face_labels(mesh, result)


_CUT_NORMAL_ATTRIBUTE = "__HD2BT_CutCornerNormal"


def _store_corner_normals(mesh):
    """把当前每个面角的法向写进可随 BMesh 删除操作传递的 CORNER 属性。"""

    # Blender 4.0 会惰性计算 split normals；不先计算时 MeshLoop.normal 可能全为零。
    calculator = getattr(mesh, "calc_normals_split", None)
    if calculator is not None:
        calculator()
    existing = mesh.attributes.get(_CUT_NORMAL_ATTRIBUTE)
    if existing is not None:
        mesh.attributes.remove(existing)
    attribute = mesh.attributes.new(
        name=_CUT_NORMAL_ATTRIBUTE,
        type="FLOAT_VECTOR",
        domain="CORNER",
    )
    if len(attribute.data) != len(mesh.loops):
        raise HD2BTError("无法建立切口法向暂存层")
    for index, loop in enumerate(mesh.loops):
        if not all(math.isfinite(v) for v in loop.normal) or loop.normal.length_squared <= 1.0e-12:
            raise HD2BTError("源网格存在非有限或零长度面角法向；不能通过调高阈值忽略")
        attribute.data[index].vector = loop.normal


def _restore_corner_normals(mesh, error_tolerance=CUT_NORMAL_ERROR_TOLERANCE):
    """在最终合并后的网格回写幸存面角法向，并返回最大验证误差。"""

    attribute = mesh.attributes.get(_CUT_NORMAL_ATTRIBUTE)
    if attribute is None or len(attribute.data) != len(mesh.loops):
        raise HD2BTError("切割后法向暂存层丢失；已停止，避免生成带接缝的部件")
    expected = []
    for row in attribute.data:
        value = Vector(row.vector)
        # 中文注释：平面二等分产生的新面角会线性插值两端法向，方向正确但长度
        # 可能小于 1。Blender 写入 custom normals 时必然单位化，验证前同步单位化
        # 才是在比较方向；这不会重算或改变原有切口法向。
        if not all(math.isfinite(v) for v in value) or value.length_squared <= 1.0e-12:
            raise HD2BTError("切割后出现非有限或零长度面角法向")
        value.normalize()
        expected.append(value)
    setter = getattr(mesh, "normals_split_custom_set", None)
    if setter is None:
        raise HD2BTError("当前 Blender 不支持写入自定义分裂法向")
    # 分离后的边界已不再共享顶点。强制启用自动平滑并写入原面角法向，才能让切口
    # 两侧继续使用切割前的法向，而不是各自按新边界重新计算。
    if hasattr(mesh, "use_auto_smooth"):
        mesh.use_auto_smooth = True
    setter([tuple(value) for value in expected])
    mesh.update()
    calculator = getattr(mesh, "calc_normals_split", None)
    if calculator is not None:
        calculator()
    # 写入 custom normals 会重建底层 CustomData，旧 attribute RNA 引用可能失效；
    # 必须按名称重新取得，不能继续删除先前保存的引用。
    temporary_attribute = mesh.attributes.get(_CUT_NORMAL_ATTRIBUTE)
    if temporary_attribute is not None:
        mesh.attributes.remove(temporary_attribute)
    mesh.update()
    if calculator is not None:
        calculator()
    actual = [Vector(row.vector) for row in mesh.corner_normals]
    if len(actual) != len(expected):
        raise HD2BTError("切割后最终面角法向数量不一致")
    if any(not all(math.isfinite(v) for v in n) or n.length_squared <= 1.0e-12 for n in actual):
        raise HD2BTError("最终面角法向包含非有限或零长度数据")
    max_error = max(
        ((left - right).length for left, right in zip(actual, expected)),
        default=0.0,
    )
    if not math.isfinite(error_tolerance) or not 1.0e-6 <= error_tolerance <= 2.0:
        raise HD2BTError("法向阻断值无效")
    if max_error > error_tolerance:
        raise HD2BTError(f"切口法向回写误差 {max_error:.6f} 超过阻断值 {error_tolerance:.6f}；可检查模型或调整法向阈值后重试")
    return max_error


def _duplicate_region(source, face_labels, region, collection, head_cut_plan=None):
    # 先复制包含完整拓扑和全部自定义数据的原网格，再删除不属于本区的面。这等价于
    # “先拆开再分离”，不会像按坐标重建新网格那样在切口重新计算顶点法向。
    duplicate = source.copy()
    duplicate.data = source.data.copy()
    duplicate.name = f"HD2BT_{region}_{source.name}"
    try:
        collection.objects.link(duplicate)
        _store_corner_normals(duplicate.data)
        bm = bmesh.new()
        try:
            bm.from_mesh(duplicate.data)
            region_layer = None
            planar_layer = None
            bm.faces.ensure_lookup_table()
            if head_cut_plan is not None and region in {"Head", "Torso"}:
                # 中文注释：用临时面层保存原分类和“需平切”标记。BMesh 新分出的面
                # 会继承这些层，因此切割后不再依赖已变化的 face.index。
                region_layer = bm.faces.layers.int.new("__HD2BT_RegionCode")
                planar_layer = bm.faces.layers.int.new("__HD2BT_PlanarHeadCut")
                label_codes = {
                    label: index + 1
                    for index, label in enumerate(VISIBLE_PARTS)
                }
                code_labels = {value: key for key, value in label_codes.items()}
                planar_indices = head_cut_plan["face_indices"]
                for face in bm.faces:
                    label = face_labels[face.index] if face.index < len(face_labels) else ""
                    face[region_layer] = label_codes.get(label, 0)
                    face[planar_layer] = int(face.index in planar_indices)
                cut_faces = [face for face in bm.faces if face[planar_layer]]
                cut_edges = {edge for face in cut_faces for edge in face.edges}
                cut_vertices = {vertex for face in cut_faces for vertex in face.verts}
                if cut_faces:
                    bmesh.ops.bisect_plane(
                        bm,
                        geom=[*cut_vertices, *cut_edges, *cut_faces],
                        dist=head_cut_plan["distance_epsilon"],
                        plane_co=head_cut_plan["plane_co"],
                        plane_no=head_cut_plan["plane_no"],
                        use_snap_center=False,
                        clear_outer=False,
                        clear_inner=False,
                    )
                bm.faces.ensure_lookup_table()
                remove = []
                for face in bm.faces:
                    label = code_labels.get(face[region_layer], "")
                    if face[planar_layer] and label in {"Head", "Torso"}:
                        signed_distance = (
                            face.calc_center_median() - head_cut_plan["plane_co"]
                        ).dot(head_cut_plan["plane_no"])
                        label = "Head" if signed_distance >= 0.0 else "Torso"
                    if label != region:
                        remove.append(face)
            else:
                remove = [
                    face
                    for face in bm.faces
                    if face.index >= len(face_labels) or face_labels[face.index] != region
                ]
            bmesh.ops.delete(bm, geom=remove, context="FACES")
            # 中文注释：先删除面、再删除临时层。BMesh 删除 CustomData 层可能重建
            # Python 面包装对象，若顺序相反，待删面会变成“BMFace has been removed”。
            if planar_layer is not None:
                bm.faces.layers.int.remove(planar_layer)
            if region_layer is not None:
                bm.faces.layers.int.remove(region_layer)
            loose = [vertex for vertex in bm.verts if not vertex.link_faces]
            if loose:
                bmesh.ops.delete(bm, geom=loose, context="VERTS")
            bm.to_mesh(duplicate.data)
        finally:
            bm.free()
    except Exception:
        if bpy.data.objects.get(duplicate.name) is duplicate:
            bpy.data.objects.remove(duplicate, do_unlink=True)
        raise
    if not duplicate.data.polygons:
        bpy.data.objects.remove(duplicate, do_unlink=True)
        return None
    duplicate["HD2BT_PartSlot"] = region
    return duplicate


def _join_region_objects(objects, region, error_tolerance=CUT_NORMAL_ERROR_TOLERANCE):
    if not objects:
        raise HD2BTError(f"自动切割没有生成 {PART_LABELS[region]}；请撤销后在 Blender 中自行切割")
    if len(objects) == 1:
        result = objects[0]
    else:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in objects:
            obj.hide_set(False)
            obj.select_set(True)
        bpy.context.view_layer.objects.active = objects[0]
        if "FINISHED" not in bpy.ops.object.join():
            raise HD2BTError(f"无法合并 {PART_LABELS[region]} 网格")
        result = objects[0]
    # 同一槽位可能来自身体、衣服等多个源网格。把暂存层保留到 Join 完成后再回写，
    # 因而验证的是最终七部位对象，而不是合并前的中间副本。
    normal_error = _restore_corner_normals(result.data, error_tolerance)
    result.name = f"HD2BT_{region}"
    result["HD2BT_PartSlot"] = region
    result["HD2BT_CutNormalsPreserved"] = True
    result["HD2BT_CutNormalMaxError"] = float(normal_error)
    return result


def _selected_meshes(context, target):
    selected = [
        obj
        for obj in context.selected_objects
        if obj.type == "MESH" and obj.get("HD2BT_PartSlot") not in VISIBLE_PARTS
    ]
    if selected:
        return selected
    previous_sources = [
        obj
        for obj in bpy.data.objects
        if obj.type == "MESH" and bool(obj.get("HD2BT_AutoSplitSource"))
    ]
    if previous_sources:
        return previous_sources
    bound = []
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.get("HD2BT_ExportTemplate"):
            continue
        if obj.get("HD2BT_PartSlot") in VISIBLE_PARTS:
            continue
        if obj.name.startswith("content/fac_helldivers/cha_avatar/"):
            continue
        if obj.parent is target or any(
            modifier.type == "ARMATURE" and modifier.object is target for modifier in obj.modifiers
        ):
            bound.append(obj)
    if not bound:
        raise HD2BTError("请选择要切割的人物网格，或先把网格绑定到潜兵骨架")
    return bound


def _ensure_target_binding(obj, target):
    modifier = next((row for row in obj.modifiers if row.type == "ARMATURE"), None)
    if modifier is None:
        modifier = obj.modifiers.new(name="HD2BT_Armature", type="ARMATURE")
    modifier.object = target
    world = obj.matrix_world.copy()
    obj.parent = target
    obj.matrix_world = world


def remove_empty_vertex_groups(mesh, epsilon=1.0e-8):
    """删除一个最终部位中完全没有有效权重的顶点组。"""

    weighted_indices = {
        membership.group
        for vertex in mesh.data.vertices
        for membership in vertex.groups
        if membership.weight > epsilon
    }
    # 中文注释：先保存组对象再统一删除，避免 Blender 删除一个组后重排索引，
    # 进而把后续仍有权重的组误判为空组。
    empty_groups = [
        group for group in mesh.vertex_groups
        if group.index not in weighted_indices
    ]
    for group in empty_groups:
        mesh.vertex_groups.remove(group)
    mesh["HD2BT_EmptyVertexGroupsRemoved"] = len(empty_groups)
    return len(empty_groups)


def _physics_face_plans(sources, target, plans):
    project = getattr(target.data, 'hd2pb_project', None)
    if project is None or not project.schema_version or not project.chains:
        raise HD2BTError('工作骨架没有物理链；请先制作/迁移物理，或使用普通自动切割')
    from HD2PhysBoneTool.physbone.blender import adapter_v1, ownership
    from .physics_cut import plan_physics_cut, AVATAR_PROFILE_BONES
    issues = ownership.canonical_issues(target)
    if issues:
        raise HD2BTError('物理项目需要修复：' + '；'.join(str(issue) for issue in issues))
    document = adapter_v1.project_document_from_object(target)
    faces, spans = [], []
    for source, (labels, _) in zip(sources, plans):
        start = len(faces)
        # Match AQSDK: MMD metadata groups are not skin bones or Profile slots.
        names = {g.index: g.name for g in source.vertex_groups if g.name in target.data.bones}
        weights = []
        for vertex in source.data.vertices:
            if any(not math.isfinite(g.weight) or g.weight < 0 for g in vertex.groups):
                raise HD2BTError('网格包含非法权重；原网格未改动')
            weights.append({names[g.group]: float(g.weight) for g in vertex.groups if g.weight > 0 and g.group in names})
        for polygon in source.data.polygons:
            totals = Counter()
            for v in polygon.vertices:
                totals.update(weights[v])
            faces.append(dict(label=labels[polygon.index], bones=set(totals), bone_weights=dict(totals)))
        spans.append((start, len(faces)))
    from ..utils.reference import avatar_source_rest_locals
    if not AVATAR_PROFILE_BONES.issubset(avatar_source_rest_locals(target)):
        raise HD2BTError('工作骨架缺少公共 Avatar Profile 骨；原网格未改动')
    labels, report = plan_physics_cut(faces, document,
        AVATAR_PROFILE_BONES,
        custom_bones=[b.name for b in target.data.bones if b.get('HD2BT_CustomBone')])
    protected = set(report['protected_faces'])
    result = []
    for (start, end), (_, head_plan) in zip(spans, plans):
        if head_plan is not None:
            head_plan = dict(head_plan)
            head_plan['face_indices'] = frozenset(i for i in head_plan['face_indices'] if start+i not in protected)
            if not head_plan['face_indices']:
                head_plan = None
        result.append((labels[start:end], head_plan))
    return result, report


def auto_split_independent_character(context, target, *, physics_aware=False):
    """只在当前场景自动切出七部位；不存储、不封包、不写护甲路由。"""

    settings = getattr(context.scene, "hd2bt_settings", None)
    try:
        warning, limit = validate_thresholds(
            getattr(settings, "cut_normal_warning", DEFAULT_WARNING),
            getattr(settings, "cut_normal_limit", DEFAULT_LIMIT),
        )
    except ValueError as exc:
        raise HD2BTError(str(exc)) from exc
    sources = _selected_meshes(context, target)
    if any(not all(math.isfinite(v) for v in vertex.co) for source in sources for vertex in source.data.vertices):
        raise HD2BTError("源网格含非有限顶点坐标；法向阈值不能跳过此检查")
    plans = [_prepare_flat_head_cut(source, target, _face_labels(source, target)) for source in sources]
    physics_report = None
    if physics_aware:
        plans, physics_report = _physics_face_plans(sources, target, plans)
    collection = context.collection or context.scene.collection
    region_rows = defaultdict(list)
    created = []
    source_states = {
        source: (
            source.hide_get(),
            source.hide_render,
            "HD2BT_AutoSplitSource" in source,
            source.get("HD2BT_AutoSplitSource"),
        )
        for source in sources
    }
    # 中文注释：旧版法向报错可能留下未完成对象；只清理带本工具前缀、槽位标记且
    # 没有成功法向标记的残件，绝不触碰用户手切对象或已完成结果。
    incomplete = [
        obj
        for obj in bpy.data.objects
        if obj.type == "MESH"
        and obj.name.startswith("HD2BT_")
        and obj.get("HD2BT_PartSlot") in VISIBLE_PARTS
        and not bool(obj.get("HD2BT_CutNormalsPreserved"))
    ]
    for obj in incomplete:
        bpy.data.objects.remove(obj, do_unlink=True)
    try:
        for source, (labels, head_cut_plan) in zip(sources, plans):
            for region in VISIBLE_PARTS:
                duplicate = _duplicate_region(
                    source, labels, region, collection, head_cut_plan
                )
                if duplicate is not None:
                    created.append(duplicate)
                    _ensure_target_binding(duplicate, target)
                    region_rows[region].append(duplicate)
            source.hide_set(True)
            source.hide_render = True
            source["HD2BT_AutoSplitSource"] = True
        parts = [_join_region_objects(region_rows[region], region, limit) for region in VISIBLE_PARTS]
        empty_groups_removed = sum(remove_empty_vertex_groups(part, epsilon=0.0) if physics_aware
                                   else remove_empty_vertex_groups(part) for part in parts)
    except Exception:
        # 失败必须恢复到点击按钮前；Operator 返回 CANCELLED 本身不会替用户回滚场景。
        for obj in created:
            try:
                name = obj.name
            except ReferenceError:
                # Join 已经销毁被并入的临时对象；它们不需要再次删除。
                continue
            if bpy.data.objects.get(name) is obj:
                bpy.data.objects.remove(obj, do_unlink=True)
        for source, (hidden, hidden_render, had_flag, old_flag) in source_states.items():
            source.hide_set(hidden)
            source.hide_render = hidden_render
            if had_flag:
                source["HD2BT_AutoSplitSource"] = old_flag
            elif "HD2BT_AutoSplitSource" in source:
                del source["HD2BT_AutoSplitSource"]
        raise
    for part in parts:
        part["HD2BT_IndependentRig"] = True
        part["HD2BT_SceneOnlyCut"] = True
        part["HD2BT_CutNormalWarning"] = classify_error(part["HD2BT_CutNormalMaxError"], warning, limit) == "WARNING"
    maximum = max((float(part["HD2BT_CutNormalMaxError"]) for part in parts), default=0.0)
    return {
        "mode": "AUTO",
        "physics_report": physics_report,
        "parts": parts,
        "sources": sources,
        "empty_groups_removed": empty_groups_removed,
        "normal_max_error": maximum,
        "normal_warning": classify_error(maximum, warning, limit) == "WARNING",
        "normal_warning_threshold": warning,
        "normal_error_threshold": limit,
    }


def identify_mesh_part(mesh, target):
    totals = Counter()
    total_weight = 0.0
    for vertex in mesh.data.vertices:
        scores, distal, total = _vertex_scores(mesh, vertex)
        total_weight += total
        for key, value in scores.items():
            totals[key] += value
        totals["L_DISTAL"] += distal["L"]
        totals["R_DISTAL"] += distal["R"]
    left_proximal = max(0.0, totals["L_LEG"] - totals["L_DISTAL"])
    right_proximal = max(0.0, totals["R_LEG"] - totals["R_DISTAL"])
    candidates = {
        "Head": totals["HEAD"],
        "LeftArm": totals["L_ARM"],
        "RightArm": totals["R_ARM"],
        "Torso": totals["TORSO"],
        "Hips": totals["HIPS"] + 0.85 * (left_proximal + right_proximal),
        "LeftLeg": totals["L_DISTAL"] + 0.15 * left_proximal,
        "RightLeg": totals["R_DISTAL"] + 0.15 * right_proximal,
    }
    # 名称只用于同分辅助；权重总量始终是第一判据。
    name = _normalized(mesh.name)
    for slot, tokens in {
        "Head": ("head", "face", "hair", "helmet", "头部", "头", "脸", "髪", "头发", "帽"),
        "LeftArm": ("leftarm", "left_arm", "左臂", "左手"),
        "RightArm": ("rightarm", "right_arm", "右臂", "右手"),
        "Torso": ("torso", "body", "躯干", "上身"),
        "Hips": ("hips", "pelvis", "下身", "胯"),
        "LeftLeg": ("leftleg", "left_leg", "左腿"),
        "RightLeg": ("rightleg", "right_leg", "右腿"),
    }.items():
        if any(token in name for token in tokens):
            candidates[slot] += max(total_weight, 1.0) * 0.05
    slot, score = max(candidates.items(), key=lambda row: row[1])
    ordered = sorted(candidates.values(), reverse=True)
    confidence = score / max(score + (ordered[1] if len(ordered) > 1 else 0.0), 1.0e-8)
    if score <= 1.0e-8:
        # 无可识别权重时使用所有顶点的中心而非首顶点，防止顶点排序影响部位判断。
        if mesh.data.vertices:
            center = sum(
                (vertex.co for vertex in mesh.data.vertices),
                Vector((0.0, 0.0, 0.0)),
            ) / len(mesh.data.vertices)
        else:
            center = Vector((0.0, 0.0, 0.0))
        guides = _skeleton_guides(target, mesh)
        if _head_progress(center, guides["neck"], guides["head"]) >= HEAD_CUT_PROGRESS:
            slot = "Head"
        elif (center - guides["hips"]).dot(guides["spine"] - guides["hips"]) > 0.0:
            slot = "Torso"
        else:
            slot = "Hips"
        confidence = 0.0
    return slot, confidence, dict(candidates)
