"""Pure face partition planning; never edits geometry, weights or physics."""
from collections import Counter, defaultdict


PARTS = ('Head', 'LeftArm', 'RightArm', 'Torso', 'Hips', 'LeftLeg', 'RightLeg')
# HD2AvatarSourceSkeleton1 Profile contract. Working-rig source markers include
# 37 extra scene helpers, so they cannot define the runtime's base slot budget.
# A cross-module regression compares this set to AQSDK's avatar_source.json.
AVATAR_PROFILE_BONES = frozenset(
    ('boss', 'spine1', 'spine2', 'chest', 'neck', 'head', 'hips', 'root',
     'support_mg', 'attach_intelpad', 'attach_samplepouch')
    + tuple(f'{side}_{name}' for side in ('l', 'r') for name in
            ('clavicle', 'shoulder', 'elbow', 'hand', 'hand_twist', 'shoulder_twist',
             'thigh', 'knee', 'foot', 'ball', 'toe'))
    + tuple(f'{side}_{finger}_finger{joint}' for side in ('l', 'r')
            for finger in ('thumb', 'index', 'middle', 'ring', 'pinky') for joint in (1, 2, 3)))
# Working-rig anchors, never substring guesses about accessory names.
ATTACHMENT_PARTS = dict(head='Head', Head='Head', neck='Torso', Neck='Torso',
    chest='Torso', spine1='Torso', spine2='Torso', boss='Torso', hips='Hips', Hip='Hips',
    l_clavicle='Torso', r_clavicle='Torso',
    l_shoulder='LeftArm', l_elbow='LeftArm', l_hand='LeftArm',
    r_shoulder='RightArm', r_elbow='RightArm', r_hand='RightArm',
    l_thigh='LeftLeg', l_knee='LeftLeg', l_foot='LeftLeg',
    r_thigh='RightLeg', r_knee='RightLeg', r_foot='RightLeg')

OPTIONAL_FINGER_TARGETS = frozenset(f'{side}_{finger}_finger{joint}'
    for side in ('l', 'r') for finger in ('thumb', 'index', 'middle', 'ring', 'pinky')
    for joint in (1, 2, 3))


def profile_targets(required, native, parents):
    """Omit unused finger OUTPUT slots, never public animation input channels.

    Keep the complete structural rig for fitting/IK. A weighted finger, collider,
    logical node or custom descendant retains its native finger ancestor path.
    SceneGraph and author bones are not removed by this budget calculation.
    """
    selected = set(required) | (set(native) - OPTIONAL_FINGER_TARGETS)
    for name in tuple(required):
        seen = set()
        while name and name not in seen:
            seen.add(name)
            if name in native and name in OPTIONAL_FINGER_TARGETS:
                selected.add(name)
            name = parents.get(name)
    return selected


def attachment_parts(bones, anchors=None):
    """Resolve nearest anatomical ancestor; validate every path including anchors."""
    parents = {b['name']: b.get('parent') for b in bones}
    if len(parents) != len(bones):
        raise ValueError('物理骨架包含重复骨名')
    anchors = ATTACHMENT_PARTS if anchors is None else anchors
    resolved = {}
    def visit(name, visiting):
        if not name:
            return None
        if name in visiting:
            raise ValueError('物理骨架父链存在循环')
        if name in resolved:
            return resolved[name]
        inherited = visit(parents.get(name), visiting | {name})
        resolved[name] = anchors.get(name, inherited)
        return resolved[name]
    for name in parents:
        visit(name, set())
    return resolved


def plan_physics_cut(faces, project, native_bones=(), *, bone_limit=256,
                     head_bones=('head', 'Head'), custom_bones=()):
    """Keep chain/link clusters and custom FK footprints in their attached part.

    native_bones is the exporter's canonical Avatar Profile set (63), NOT all
    non-custom scene nodes. Production Profiles select weighted/logical/collider
    bones without ancestor closure; SceneGraph ancestors use a separate palette.
    """
    chains = {c['name']: c for c in project.get('chains', ())}
    if not chains or len(chains) != len(project.get('chains', ())):
        raise ValueError('没有有效物理链；请先在工作骨架上建立物理项目')
    anchors = dict(ATTACHMENT_PARTS)
    anchors.update({name: 'Head' for name in head_bones})
    attachments = attachment_parts(project.get('shared_bones', ()), anchors)
    native = set(native_bones)
    custom = set(custom_bones) - native
    domains = {name: attachments.get(chain['joints'][0]['bone']) for name, chain in chains.items()}
    roots = {name: name for name in chains}
    def root(name):
        while roots[name] != name:
            roots[name] = roots[roots[name]]
            name = roots[name]
        return name
    def union(names):
        names = sorted({root(n) for n in names})
        for name in names[1:]:
            roots[name] = names[0]
    affected = {}
    logical = {}
    for name, chain in chains.items():
        logical[name] = {j['bone'] for j in chain['joints']}
        joints = chain['joints'] if chain.get('root_swing', False) else chain['joints'][1:]
        for joint in joints:
            bone = joint['bone']
            if bone in affected:
                raise ValueError(f'模拟骨 {bone} 属于多条物理链')
            affected[bone] = name
    touched, face_parts = [], []
    parents = {b['name']: b.get('parent') for b in project.get('shared_bones', ())}
    def affecting_chains(bones):
        names=set()
        for bone in bones:
            seen=set()
            while bone and bone not in seen:
                seen.add(bone)
                if bone in affected:names.add(affected[bone])
                bone=parents.get(bone)
        return names
    for face in faces:
        if face['label'] not in PARTS:
            raise ValueError('网格包含未知部位标签')
        names = affecting_chains(face['bones'])
        union(names)
        touched.append(names)
        owners = {attachments.get(b) for b in face['bones'] if b in custom} - {None}
        # A supporting FK body bone can weight skirt roots too. Simulation
        # ownership wins on these faces; FK support must not relocate the chain.
        if names:
            face_parts.append(None)
        elif len(owners) == 1:
            face_parts.append(next(iter(owners)))
        elif owners and face.get('bone_weights'):
            votes = Counter()
            for bone, weight in face['bone_weights'].items():
                if bone in custom and attachments.get(bone):
                    votes[attachments[bone]] += weight
            face_parts.append(min(votes, key=lambda s: (-votes[s], PARTS.index(s))))
        else:
            face_parts.append(None)
    for link in project.get('chain_links', ()):
        names = (link['chain_a'], link['chain_b'])
        if any(name not in chains for name in names):
            raise ValueError('链组连接引用不存在的物理链')
        union(names)
    clusters = defaultdict(lambda: dict(faces=[], bones=set(), votes=Counter(), chains=[], owners=set()))
    for name in chains:
        row = clusters[root(name)]
        row['chains'].append(name)
        row['bones'].update(logical[name])
        if domains[name]:
            row['owners'].add(domains[name])
    labels = [owner or f['label'] for f, owner in zip(faces, face_parts)]
    parents = {b['name']: b.get('parent') for b in project.get('shared_bones', ())}
    used = {slot: profile_targets((), native, parents) for slot in PARTS}
    for i, (face, names, owner) in enumerate(zip(faces, touched, face_parts)):
        if names:
            row = clusters[root(next(iter(names)))]
            row['faces'].append(i)
            row['bones'].update(face['bones'])
            row['votes'][face['label']] += 1
            if owner:
                row['owners'].add(owner)
        else:
            used[labels[i]].update(profile_targets(face['bones'], native, parents))
    for slot, bones in used.items():
        if len(bones) > bone_limit:
            raise ValueError(f'{slot} 非物理部分需要 {len(bones)} 骨，超过 {bone_limit}；原网格未改动')
    collider_bones = {c.get(k) for c in project.get('colliders', ())
                      if not c.get('dynamic', False) for k in ('bone', 'bone_b') if c.get(k)}
    assignments, reasons = {}, {}
    for row in clusters.values():
        if not row['faces']:
            continue
        if len(row['owners']) > 1:
            detail = '头盔和身体' if 'Head' in row['owners'] else '多个身体部位'
            raise ValueError(f'物理链组 {", ".join(row["chains"])} 同时连接{detail} {", ".join(sorted(row["owners"]))}；请检查混合权重或链组连接')
        owner = next(iter(row['owners'])) if row['owners'] else min(row['votes'], key=lambda s: (-row['votes'][s], PARTS.index(s)))
        required = profile_targets(row['bones'] | collider_bones, native, parents)
        if len(required) > bone_limit:
            raise ValueError(f'物理链组 {", ".join(row["chains"])} 整体需要 {len(required)} 骨，超过 {bone_limit}；不能拆链或删权重')
        used[owner].update(required)
        if len(used[owner]) > bone_limit:
            raise ValueError(f'无法在 {bone_limit} 骨限制内将物理链组 {", ".join(row["chains"])} 保留于 {owner}（需要 {len(used[owner])}）；不能挪到其他部位，请调整制作分组')
        for i in row['faces']:
            labels[i] = owner
        assignments.update({name: owner for name in row['chains']})
        reasons.update({name: 'anatomical_attachment' if row['owners'] else 'face_majority_unresolved_attachment' for name in row['chains']})
    return labels, dict(chain_owners=assignments,
        moved_faces=sum(label != f['label'] for label, f in zip(labels, faces)),
        chain_owner_reasons=reasons,
        custom_bone_owners={b: attachments[b] for b in sorted(custom) if attachments.get(b)},
        protected_faces=[i for i, (names, owner) in enumerate(zip(touched, face_parts)) if names or owner],
        profile_bone_counts={k: len(v) for k, v in used.items()},
        unweighted_chains=sorted(set(chains)-set(assignments)))
