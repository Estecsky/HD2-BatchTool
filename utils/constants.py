"""HD2BatchTool 的稳定常量。

这里只保存不会随 Blender 场景变化的数据，避免 UI、骨架算法和后台任务互相
依赖。对象中文名只用于从随插件发布的参考文件中定位资产，运行时识别用户骨架
始终优先使用 BonesID / StateMachineID 与骨骼结构。
"""

from pathlib import Path


PROFILE_SCHEMA_VERSION = 2

THIRD_PERSON = "THIRD_PERSON"
FIRST_PERSON = "FIRST_PERSON"
AUTO = "AUTO"

THIRD_PERSON_RIG_ID = 5556372446766824087
FIRST_PERSON_RIG_ID = 15046329857013222381

TP_SOURCE_NAME = "content/fac_helldivers/cha_avatar/avatar_helldiver_rig"
TP_TPOSE_NAME = "avatar_helldiver_rig_t-pose"
REFERENCE_GROUPS = {
    "TP_INITIAL": (
        TP_SOURCE_NAME,
        ("content/fac_helldivers/cha_avatar/avatar_helldiver_lod0",),
    ),
    "TP_TPOSE": (
        TP_TPOSE_NAME,
        ("content/fac_helldivers/cha_avatar/avatar_helldiver_lod0.001",),
    ),
}

REFERENCE_GROUP_PROP = "HD2BT_ReferenceGroup"
WORKING_RIG_PROP = "HD2BT_WorkingRig"
FULL_REST_POSE_PROP = "HD2BT_FullRestPose"
BODY_AXIS_SPACE_PROP = "HD2BT_BodyAxisSpace"
POSE_SYSTEM_PROP = "HD2BT_PoseSystem"
RIG_KIND_PROP = "HD2BT_RigKind"
POSE_SPACE_PROP = "HD2BT_PoseSpace"
POSE_INITIAL = "INITIAL"
POSE_TPOSE = "TPOSE"
AXIS_INITIAL = "INITIAL"
AXIS_SYMMETRIC = "SYMMETRIC"
POSE_SYSTEM_BODY_V1 = "BODY_POSE_V1"
SOURCE_BONE_PROP = "HD2BT_WorkRigSourceBone"
SOURCE_REST_LOCAL_PROP = "HD2BT_AvatarSourceRestLocal"

def addon_root() -> Path:
    return Path(__file__).resolve().parents[1]


def reference_blend_path() -> Path:
    return addon_root() / "assets" / "hd2_reference.blend"


def body_pose_profile_path() -> Path:
    return addon_root() / "assets" / "body_pose_profile.json"
