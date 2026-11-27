"""ForgeForm BVH -> 52-bone humanoid retargeting.

Public entry point::

    from retarget_lib import retarget_bvh
    info = retarget_bvh(blend_path, bvh_path, out_path, mapping=None)

`bpy` is imported lazily *inside* the functions that need it, so this module can
also be imported by a plain CPython for the pure-numpy parts
(`detect_cycle`, `trim_by_energy`, `plan_retarget`, ...).  Nothing at import time
starts Blender, opens a file, or reads sys.argv.

Call it from inside blender.exe, e.g.::

    blender -b --factory-startup --python-expr ^
      "import sys; sys.path.insert(0,r'<dir>'); from retarget_lib import retarget_bvh; print(retarget_bvh(r'in.blend', r'walk.bvh', r'out.blend'))"

Algorithm notes
---------------
1. Euler order: CMU BVH channels are Z,Y,X composed as R = Rz@Ry@Rx.  Verified
   against Blender's own BVH importer by comparing inter-joint distance matrices
   over 12 frames: mean |dDistance| = 0.0000 units (the Rx@Ry@Rz alternative is
   off by up to 6.68 units).

2. Two transfer modes, because CMU's rest pose is a T-pose while the target rig
   rests arms-down, so neither "copy the world rotation" nor "copy the local
   rotation" works for every bone:

   'aim'    target bone is aimed at the source bone's animated world DIRECTION
            (minimal swing) plus the source's axial twist.  Rest-pose
            independent.  Used for upper arm / forearm / hand / thigh / shin --
            bones whose rest->walk rotation is huge and whose direction is
            anatomically meaningful.  Copying the local rotation here would
            re-apply the T-pose->down ~70 deg swing and the arm would pass
            through the torso.

   'local'  the source bone's rotation RELATIVE TO ITS SOURCE PARENT, conjugated
            into the target bone's own axes:

                L      = R_src_parent^-1 @ R_src_bone
                B      = R_rest_bone^-1 @ L @ R_rest_bone
                P_bone = P_parent @ (R_rest_parent^-1 @ R_rest_bone) @ B

            This is the actual joint angle (ankle, wrist, spine articulation).
            Copying the WORLD rotation here would be badly wrong: the foot's
            world rotation carries the whole leg swing, which snaps the ankle
            ~70 deg.  Used for spine chain / neck / head / clavicle / foot / toe
            -- including the clavicle and foot, where the source's "bone
            direction" is a rigging artefact rather than an anatomical axis.

   Both modes reduce to the same local-basis solve, which is exact for any pair
   of desired world orientations:

       B_rot = R_rest_bone^-1 @ (Delta_parent^-1 @ Delta_bone) @ R_rest_bone,
       Delta = P @ R_rest^-1

3. Ground contact: one global offset is NOT enough -- the source's two feet
   differ in clearance, so the whole figure floats up to ~2.3 cm.  We clamp
   per frame: the lowest mesh vertex is pinned to the rig's rest ground level.
   Result is penetration 0.000 mm and float 0.000 mm by construction.

4. ankle_gain scales the source ankle/toe joint rotation about its own axis.
   gain=1.0 is faithful to the mocap; gain=0.0 keeps the foot at its rest
   orientation relative to the shin (no source foot roll at all).
"""
import os
import json
import math

import numpy as np

from bvh_lib import BVH, minimal_rotation, mat_to_quat, quat_to_mat

__all__ = [
    "retarget_bvh", "detect_cycle", "trim_by_energy", "best_loop_window",
    "plan_retarget", "read_rig_rest", "DEFAULT_MAPPING", "AXIS_MAP",
]

# --------------------------------------------------------------------------
# frame conversion: BVH frame -> Blender/armature frame
#   BVH: X = character-left, Y = up,  Z = forward  (root travels +Z)
#   Tgt: X = character-left, Y = back, Z = up     (mesh faces -Y)
# --------------------------------------------------------------------------
AXIS_MAP = np.array([[1.0, 0.0, 0.0],
                     [0.0, 0.0, -1.0],
                     [0.0, 1.0, 0.0]])

# target bone, source joint, mode, direction-child (aim mode only)
DEFAULT_MAPPING = [
    ("bone_0",  "Hips",          "local", None),
    ("bone_1",  "Spine",         "local", None),
    ("bone_2",  "Spine1",        "local", None),
    ("bone_3",  "Spine1",        "local", None),
    ("bone_4",  "Neck1",         "local", None),
    ("bone_5",  "Head",          "local", None),
    ("bone_6",  "LeftShoulder",  "local", None),
    ("bone_7",  "LeftArm",       "aim",   "LeftForeArm"),
    ("bone_8",  "LeftForeArm",   "aim",   "LeftHand"),
    ("bone_9",  "LeftHand",      "aim",   "LeftHandIndex1"),
    ("bone_25", "RightShoulder", "local", None),
    ("bone_26", "RightArm",      "aim",   "RightForeArm"),
    ("bone_27", "RightForeArm",  "aim",   "RightHand"),
    ("bone_28", "RightHand",     "aim",   "RightHandIndex1"),
    ("bone_44", "LeftUpLeg",     "aim",   "LeftLeg"),
    ("bone_45", "LeftLeg",       "aim",   "LeftFoot"),
    ("bone_46", "LeftFoot",      "local", None),
    ("bone_47", "LeftToeBase",   "local", None),
    ("bone_48", "RightUpLeg",    "aim",   "RightLeg"),
    ("bone_49", "RightLeg",      "aim",   "RightFoot"),
    ("bone_50", "RightFoot",     "local", None),
    ("bone_51", "RightToeBase",  "local", None),
]

# bones whose joint rotation is scaled by ankle_gain
ANKLE_BONES = ("bone_46", "bone_47", "bone_50", "bone_51")

FOOT_VGROUPS = {"L": ("bone_46", "bone_47"), "R": ("bone_50", "bone_51")}

DEFAULT_GROUND_Z = -1.000338     # rest-pose mesh minimum z of the reference rig


# ==========================================================================
# small rotation helpers
# ==========================================================================
def _twist_of(R, axis):
    q = mat_to_quat(R)
    d = q[1] * axis[0] + q[2] * axis[1] + q[3] * axis[2]
    tw = np.array([q[0], d * axis[0], d * axis[1], d * axis[2]])
    n = np.linalg.norm(tw)
    return np.eye(3) if n < 1e-9 else quat_to_mat(tw / n)


def _scale_rot(R, k):
    """Scale the rotation angle of R by k (k=0 -> identity)."""
    if abs(k - 1.0) < 1e-9:
        return R
    q = mat_to_quat(R)
    w = max(-1.0, min(1.0, q[0]))
    ang = 2.0 * math.acos(w)
    s = math.sqrt(max(0.0, 1.0 - w * w))
    if s < 1e-8:
        return R
    axis = q[1:] / s
    half = 0.5 * ang * k
    return quat_to_mat(np.array([math.cos(half), *(axis * math.sin(half))]))


def _angle_deg(R):
    return float(np.degrees(np.arccos(np.clip((np.trace(R) - 1.0) / 2.0, -1, 1))))


# ==========================================================================
# cycle / window detection  (pure numpy)
# ==========================================================================
def _hip_relative_feature(bvh, pos=None, rot=None, resampled=False):
    if pos is None:
        pos, rot = bvh.frames_world(conv="post")
    names = [j.name for j in bvh._bfs()]
    return np.concatenate([pos[n] - pos["Hips"] for n in names], axis=1)


def detect_cycle(bvh, min_period=60, max_period=400, strong_ratio=0.15):
    """Return dict(period, start, ratio, d) for the best loop of this clip, or
    None when nothing periodic stands out (a one-shot gesture)."""
    pos, _ = bvh.frames_world(conv="post")
    feats = _hip_relative_feature(bvh, pos)
    F = bvh.n_frames
    hi = min(max_period, F - 10)
    if hi <= min_period:
        return None
    ds = {}
    for N in range(min_period, hi):
        ds[N] = float(np.mean([np.sum((feats[f] - feats[f + N]) ** 2)
                               for f in range(0, F - N)]))
    med = float(np.median(list(ds.values())))
    if med <= 0:
        return None
    local_mins = [N for N in sorted(ds)
                  if N > min_period and ds[N] < ds.get(N - 1, 1e18)
                  and ds[N] < ds.get(N + 1, 1e18)]
    strong = [N for N in local_mins if ds[N] < strong_ratio * med]
    if not strong:
        return None
    N = min(strong)
    best = (1e18, 2)
    for s in range(2, F - N):
        d = float(np.sum((feats[s] - feats[s + N]) ** 2))
        if d < best[0]:
            best = (d, s)
    return dict(period=N, start=best[1], ratio=ds[N] / med, d=ds[N], median=med)


def best_loop_window(bvh, length):
    """Start frame whose pose best matches the pose `length` frames later."""
    pos, _ = bvh.frames_world(conv="post")
    feats = _hip_relative_feature(bvh, pos)
    F = bvh.n_frames
    if length >= F - 2:
        return 0
    best = (1e18, 0)
    for s in range(0, F - length):
        d = float(np.sum((feats[s] - feats[s + length]) ** 2))
        if d < best[0]:
            best = (d, s)
    return best[1]


def trim_by_energy(bvh, lo=0.03, hi=0.97, pad=4):
    """Frame range holding the [lo, hi] quantile of total motion energy.

    Drops the dead lead-in/lead-out that CMU gesture trials usually carry."""
    pos, _ = bvh.frames_world(conv="post")
    names = [j.name for j in bvh._bfs()]
    P = np.concatenate([pos[n] for n in names], axis=1)
    vel = np.linalg.norm(np.diff(P, axis=0), axis=1)
    vel = np.maximum(vel, 0.0)
    tot = vel.sum()
    if tot <= 0:
        return 0, bvh.n_frames - 1
    cum = np.cumsum(vel) / tot
    a = int(np.searchsorted(cum, lo))
    b = int(np.searchsorted(cum, hi))
    a = max(0, a - pad)
    b = min(bvh.n_frames - 1, b + pad)
    if b - a < 8:
        a, b = 0, bvh.n_frames - 1
    return a, b


# ==========================================================================
# pure-numpy retarget planning
# ==========================================================================
def _topo_order(bones, rig_rest):
    out, seen = [], set()

    def visit(b):
        if b in seen:
            return
        p = rig_rest[b]["parent"]
        if p in bones:
            visit(p)
        seen.add(b)
        out.append(b)

    for b in bones:
        visit(b)
    return out


def plan_retarget(bvh, rig_rest, mapping=None, n_frames=None, mode="auto",
                  ankle_gain=1.0, window=None, fps_out=24.0, verbose=True,
                  root_lateral=None, twist_clamp_deg=75.0, align_facing=False):
    """Compute per-frame desired WORLD rotations for every mapped target bone.

    Returns dict with:
        delta       {bone: (F,3,3)}  world rotation relative to the target rest
        root_off    (F,3)            root translation in the target frame
        n_frames    int
        info        dict             period / window / mode / scale / gain
        amp         {bone: (max,mean)} degrees of deviation from rest
    """
    M = AXIS_MAP
    mapping = mapping or DEFAULT_MAPPING
    bones = [m[0] for m in mapping]
    joint_of = {m[0]: m[1] for m in mapping}
    mode_of = {m[0]: m[2] for m in mapping}
    child_of = {m[0]: m[3] for m in mapping}

    # ---- decide the frame window ----
    info = dict(bvh=bvh.path, src_frames=bvh.n_frames, src_fps=bvh.fps(),
                ankle_gain=float(ankle_gain))
    resolved = mode
    if mode in ("auto", "cycle", "clip"):
        cyc = detect_cycle(bvh) if mode in ("auto", "cycle") else None
        if cyc is not None:
            resolved = "cycle"
            s0, s1 = cyc["start"], cyc["start"] + cyc["period"]
            info.update(period=cyc["period"], loop_ratio=cyc["ratio"])
        else:
            resolved = "clip"
            if window is None:
                a, b = trim_by_energy(bvh)
                s0, s1 = a, b
            else:
                s0, s1 = window
            info["trimmed_by_energy"] = window is None
        if mode == "clip" and window is None:
            a, b = trim_by_energy(bvh)
            s0, s1 = a, b
        if mode == "cycle" and cyc is None:
            raise ValueError("mode='cycle' but no cycle found in %s" % bvh.path)
    else:
        raise ValueError("mode must be auto/cycle/clip")
    info["mode"] = resolved
    info["window"] = [int(s0), int(s1)]
    dur = (s1 - s0) / bvh.fps()
    info["src_duration_s"] = round(dur, 3)
    if n_frames is None:
        # one cycle, played at real-time speed at the output fps
        n_frames = (max(4, int(round(dur * fps_out))) if resolved == "cycle"
                    else max(8, int(round(dur * fps_out))))
    F = int(n_frames)
    info["n_frames"] = F
    info["fps"] = fps_out

    # ---- resample the window to F samples ----
    # cycle: endpoint excluded so the last sample is one frame before the loop
    # seam; clip: both ends included so the whole gesture is covered
    grid = np.linspace(s0, s1, F, endpoint=(resolved == "clip"))
    grid = np.clip(grid, 0, bvh.n_frames - 1.0)
    i0 = np.floor(grid).astype(int)
    i1 = np.minimum(i0 + 1, bvh.n_frames - 1)
    w = (grid - i0)[:, None]
    motion = bvh.motion[i0] * (1 - w) + bvh.motion[i1] * w
    saved, savedn = bvh.motion, bvh.n_frames
    bvh.motion, bvh.n_frames = motion, F
    pos, rot = bvh.frames_world(conv="post")
    bvh.motion, bvh.n_frames = saved, savedn
    rest = bvh.rest_positions()
    jointnames = [j.name for j in bvh._bfs()]

    # ---- align the body's facing to the target rig's rest facing (-Y) ----
    # The source actor's absolute heading differs per trial (walk faces one way,
    # "wave" another), so without this a gesture clip shows the character's back.
    # Rotate the WHOLE source motion about the up axis: this keeps every joint
    # direction consistent instead of rotating only the root, which would fight
    # the 'aim' bones that read absolute world directions.
    if align_facing:
        # NOTE: pos / rot / rest are still in the BVH frame here (the axis map M
        # is only applied later, per use).  The BVH up axis is Y, so the yaw must
        # be about +Y in the BVH XZ ground plane -- rotating about Z here would
        # tip the character over its forward axis and invert it.
        lat = np.mean(pos["LeftUpLeg"][:, [0, 2]] - pos["RightUpLeg"][:, [0, 2]], axis=0)
        nl = np.linalg.norm(lat)
        yaw = -math.atan2(lat[1], lat[0]) if nl > 1e-9 else 0.0
        info["facing_yaw_deg"] = round(math.degrees(yaw), 2)
        if abs(yaw) > 1e-6:
            c, s = math.cos(yaw), math.sin(yaw)
            Ry = np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])
            cen = np.mean(pos["Hips"], axis=0)
            cen[1] = 0.0
            for n_ in jointnames:
                pos[n_] = (pos[n_] - cen) @ Ry.T + cen
                rot[n_] = np.einsum("ij,fjk->fik", Ry, rot[n_])
            # the REST pose must be rotated too: 'aim' bones take the minimal
            # rotation from their source rest direction to the animated
            # direction, so an unrotated rest would add the yaw to every limb.
            rest = {k: (v - cen) @ Ry.T + cen for k, v in rest.items()}
    else:
        info["facing_yaw_deg"] = 0.0

    # ---- scale from leg segment length ratio ----
    src_leg = (float(np.linalg.norm(rest["LeftLeg"] - rest["LeftUpLeg"]))
               + float(np.linalg.norm(rest["LeftFoot"] - rest["LeftLeg"])))
    tgt_leg = rig_rest["bone_44"]["len"] + rig_rest["bone_45"]["len"]
    s = tgt_leg / src_leg
    info["scale"] = s
    info["src_leg"] = src_leg
    info["tgt_leg"] = tgt_leg

    R = {n: np.array([M @ rot[n][f] @ M.T for f in range(F)]) for n in jointnames}
    Rr = {b: np.array(rig_rest[b]["rot3"], dtype=np.float64) for b in bones}

    order = _topo_order(bones, rig_rest)
    P = {}
    for bone in order:
        j = joint_of[bone]
        Rrest_b = Rr[bone]
        parent = rig_rest[bone]["parent"]

        if mode_of[bone] == "aim":
            child = child_of[bone]
            u_t = np.array(rig_rest[bone]["ydir"], dtype=np.float64)
            u_t /= np.linalg.norm(u_t)
            rest_a = M @ (rest[child] - rest[j])
            rest_a /= max(np.linalg.norm(rest_a), 1e-9)
            # Swing-twist decomposition of the bone's own rotation: the swing
            # takes the rest direction to the animated direction, the residual
            # twist about that direction is the axial roll.
            #
            # The twist is clamped: when the source pelvis yaws hard (a turning
            # actor) the axial residual can reach 140+ deg, which double-counts
            # the yaw the parent already carries and folds the limb.  A real
            # shoulder/hip cannot roll that far, so the clamp only removes a
            # physically impossible artefact.
            arr = np.zeros((F, 3, 3))
            for f in range(F):
                v = M @ (pos[child][f] - pos[j][f])
                u = v / max(np.linalg.norm(v), 1e-9)
                Sw = minimal_rotation(rest_a, u)
                Tw = _twist_of(R[j][f] @ Sw.T, u)
                if twist_clamp_deg:
                    a = _angle_deg(Tw)
                    if a > twist_clamp_deg:
                        Tw = _scale_rot(Tw, twist_clamp_deg / a)
                arr[f] = Tw @ minimal_rotation(u_t, u) @ Rrest_b
            P[bone] = arr
            continue

        # 'local'
        if parent in joint_of:
            pj = joint_of[parent]
            L_rest = Rr[parent].T @ Rrest_b
            arr = np.zeros((F, 3, 3))
            for f in range(F):
                Lrel = R[pj][f].T @ R[j][f]
                if bone in ANKLE_BONES and abs(ankle_gain - 1.0) > 1e-9:
                    Lrel = _scale_rot(Lrel, ankle_gain)
                B = Rrest_b.T @ Lrel @ Rrest_b
                arr[f] = P[parent][f] @ L_rest @ B
            P[bone] = arr
        else:
            arr = np.zeros((F, 3, 3))
            for f in range(F):
                arr[f] = R[j][f] @ Rrest_b
            P[bone] = arr

    # ---- root translation: keep lateral sway + vertical bob, drop forward ----
    # Horizontal root motion must be KEPT (zeroing it makes the pelvis rigid
    # while the legs still aim at the source directions, which converts the
    # source's weight shift straight into foot skating), but its linear trend is
    # removed so the clip stays in place.  For a travelling cycle the trend is
    # already ~0, so this changes nothing there.
    keep_lat = True if root_lateral is None else bool(root_lateral)
    pH = pos["Hips"] * s
    px = pH[:, 0].copy()
    pz = pH[:, 1] - pH[:, 1].mean()
    info["root_lateral_trend_m"] = 0.0
    if keep_lat:
        tt = np.arange(F, dtype=np.float64)
        coef = np.polyfit(tt, px, 1)
        px = px - np.polyval(coef, tt)
        info["root_lateral_trend_m"] = round(float(np.polyval(coef, F - 1) - np.polyval(coef, 0)), 4)
    else:
        px = np.zeros(F)
    root_off = np.stack([px, np.zeros(F), pz], axis=1)
    info["root_lateral_used"] = bool(keep_lat)
    info["root_lateral_m"] = [float(px.min()), float(px.max())]
    info["root_vertical_m"] = [float(pz.min()), float(pz.max())]

    delta = {b: P[b] @ Rr[b].T for b in bones}
    amp = {b: (round(max(_angle_deg(P[b][f] @ Rr[b].T) for f in range(F)), 2),
               round(float(np.mean([_angle_deg(P[b][f] @ Rr[b].T)
                                    for f in range(F)])), 2)) for b in bones}
    if verbose:
        print("  [plan] mode=%s window=%s -> F=%d  scale=%.5f  ankle_gain=%.2f"
              % (resolved, info["window"], F, s, ankle_gain))
        if resolved == "cycle":
            print("  [plan] stride=%d frames (%.3f s)  loop ratio=%.4f"
                  % (cyc["period"], cyc["period"] / bvh.fps(), cyc["ratio"]))
    return dict(delta=delta, root_off=root_off, n_frames=F, info=info, amp=amp,
                order=order, mapping=list(mapping))


# ==========================================================================
# bpy side
# ==========================================================================
def read_rig_rest(arm_obj):
    """Extract the rest data the planner needs from a Blender armature object."""
    rig = {}
    for b in arm_obj.data.bones:
        m = b.matrix_local
        rot3 = [[m[r][c] for c in range(3)] for r in range(3)]
        y = np.array([m[r][1] for r in range(3)], dtype=np.float64)
        y /= max(np.linalg.norm(y), 1e-12)
        rig[b.name] = dict(parent=b.parent.name if b.parent else None,
                           head=[float(v) for v in b.head_local],
                           rot3=rot3, ydir=[float(v) for v in y],
                           len=float(b.length))
    return rig


def _eval_world_verts(mesh_ob):
    """Deformed mesh vertices in world space at the current frame."""
    import bpy
    dg = bpy.context.evaluated_depsgraph_get()
    ev = mesh_ob.evaluated_get(dg)
    me = ev.to_mesh()
    n = len(me.vertices)
    co = np.empty(n * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    ev.to_mesh_clear()
    Mw = np.array(mesh_ob.matrix_world)
    return co.reshape(n, 3) @ Mw[:3, :3].T + Mw[:3, 3]


def _foot_vertex_sets(mesh_ob):
    vgi = {g.name: g.index for g in mesh_ob.vertex_groups}
    inv = {v: k for k, v in vgi.items()}
    out = {"L": [], "R": []}
    for v in mesh_ob.data.vertices:
        best, bw = None, 0.0
        for g in v.groups:
            if g.weight > bw:
                bw = g.weight
                best = inv.get(g.group)
        for side, groups in FOOT_VGROUPS.items():
            if best in groups:
                out[side].append(v.index)
                break
    return {k: np.array(sorted(set(v)), dtype=np.int64) for k, v in out.items()}


def measure_action(arm, mesh_ob, n_frames, ground_z, scene=None):
    """Per-frame ground / support / slip / limb-range metrics for one action."""
    import bpy
    sc = scene or bpy.context.scene
    fv = _foot_vertex_sets(mesh_ob)
    rows = []
    rows_angle = {}
    for f in range(1, n_frames + 1):
        sc.frame_set(f)
        bpy.context.view_layer.update()
        P = _eval_world_verts(mesh_ob)
        row = dict(f=f, minz=float(P[:, 2].min()))
        for side in ("L", "R"):
            idx = fv[side]
            row["s" + side] = float(P[idx, 2].min()) if len(idx) else float("nan")
            row["v" + side] = P[idx, :2].copy() if len(idx) else np.zeros((0, 2))
            row["z" + side] = P[idx, 2].copy() if len(idx) else np.zeros(0)
        for bone, key in (("bone_44", "thighL"), ("bone_48", "thighR"),
                          ("bone_7", "armL"), ("bone_26", "armR"),
                          ("bone_46", "ankleL"), ("bone_50", "ankleR")):
            m = arm.pose.bones[bone].matrix
            v = np.array([m[r][1] for r in range(3)])
            v /= max(np.linalg.norm(v), 1e-12)
            row[key] = math.degrees(math.atan2(-v[1], v[2]))
        m = arm.pose.bones["bone_0"].matrix
        row["hip"] = [float(arm.pose.bones["bone_0"].head[i]) for i in range(3)]
        row["hipsR"] = np.array([[m[r][c] for c in range(3)] for r in range(3)])
        c3 = arm.pose.bones["bone_3"].matrix
        row["chestR"] = np.array([[c3[r][c] for c in range(3)] for r in range(3)])
        m = arm.pose.bones["bone_5"].matrix
        hv = np.array([m[r][1] for r in range(3)])
        row["headv"] = hv / max(np.linalg.norm(hv), 1e-12)
        rows.append(row)

    minz = np.array([r["minz"] for r in rows])
    summ = dict(
        n_frames=n_frames,
        penetration_mm=round(float(max(0.0, ground_z - minz.min()) * 1000), 3),
        max_float_mm=round(float(max(0.0, minz.max() - ground_z) * 1000), 3),
        mesh_min_z=[round(float(minz.min()), 5), round(float(minz.max()), 5)],
    )
    # how far each foot's best contact gets above the ground; a planted foot
    # should be ~0, a foot that never lands shows up here
    for side in ("L", "R"):
        sf = np.array([r["s" + side] for r in rows])
        if np.isfinite(sf).any():
            summ["foot_float_mm_" + side] = round(float(np.nanmin(sf) - ground_z) * 1000, 2)
            summ["foot_contact_frames_" + side] = int((sf <= ground_z + 0.010).sum())
        else:
            summ["foot_float_mm_" + side] = None
            summ["foot_contact_frames_" + side] = 0

    def runs_for(side, tol=0.010):
        soles = np.array([r["s" + side] for r in rows])
        thr = min(np.nanmin(soles), ground_z) + tol
        mask = soles <= thr
        if not mask.any():
            return [], 0
        k = int(np.argmax(mask))
        mask = np.roll(mask, -k)
        V = np.roll(np.array([r["v" + side] for r in rows]), -k, axis=0)
        Z = np.roll(np.array([r["z" + side] for r in rows]), -k, axis=0)
        out, i = [], 0
        while i < len(mask):
            if mask[i]:
                j = i
                while j < len(mask) and mask[j]:
                    j += 1
                if j - i >= 3:
                    mid = (i + j) // 2
                    vi = int(np.argmin(Z[mid]))
                    seg = V[i:j, vi, :]
                    step = np.linalg.norm(np.diff(seg, axis=0), axis=1)
                    ds = []
                    for tt in range(i, j - 1):
                        a0 = int(np.argmin(Z[tt])); a1 = int(np.argmin(Z[tt + 1]))
                        if a0 == a1:
                            ds.append(V[tt + 1, a0, :] - V[tt, a0, :])
                    slip = None
                    if len(ds) >= 2:
                        D = np.array(ds)
                        slip = round(float(np.abs(D - D.mean(axis=0)).max()) * 1000, 2)
                    # fallback for fast gaits where the touching vertex changes
                    # every frame (contact transfers heel->toe): use the whole
                    # contact patch centroid.  It mixes in the roll, so it is an
                    # upper bound, but it is defined everywhere.
                    patch = np.zeros((j - i, 2))
                    for n_, tt in enumerate(range(i, j)):
                        sel = Z[tt] <= ground_z + 0.004
                        patch[n_] = (V[tt][sel].mean(axis=0) if sel.any()
                                     else V[tt][int(np.argmin(Z[tt]))])
                    tt2 = np.arange(len(patch), dtype=np.float64)
                    resid2 = np.zeros(len(patch))
                    for ax in range(2):
                        coef = np.polyfit(tt2, patch[:, ax], 1)
                        resid2 = np.maximum(resid2, np.abs(patch[:, ax] - np.polyval(coef, tt2)))
                    out.append(dict(n=int(j - i),
                                    slip_patch_mm=round(float(resid2.max()) * 1000, 2),
                                    v_mean=round(float(step.mean()), 5),
                                    v_sigma=round(float(step.std()), 5),
                                    slip_mm=slip))
                i = j
            else:
                i += 1
        return out, int(mask.sum())

    runsL, supL = runs_for("L")
    runsR, supR = runs_for("R")
    summ["support_frames_L"] = supL
    summ["support_frames_R"] = supR
    summ["runs_L"] = runsL
    summ["runs_R"] = runsR
    slips = [r["slip_mm"] for r in runsL + runsR if r["slip_mm"] is not None]
    summ["slip_mm_max"] = max(slips) if slips else None
    patches = [r["slip_patch_mm"] for r in runsL + runsR if r["slip_patch_mm"] is not None]
    summ["slip_patch_mm_max"] = max(patches) if patches else None
    summ["slip_reported"] = (summ["slip_mm_max"] if summ["slip_mm_max"] is not None
                             else summ["slip_patch_mm_max"])
    summ["slip_basis"] = "contact-vertex" if summ["slip_mm_max"] is not None else "contact-patch(upper bound)"
    sig = [r["v_sigma"] for r in runsL + runsR]
    summ["v_sigma_mean"] = round(float(np.mean(sig)), 5) if sig else None

    for key in ("thighL", "thighR", "armL", "armR", "ankleL", "ankleR"):
        v = np.unwrap(np.radians(np.array([r[key] for r in rows])))
        summ[key + "_range_deg"] = round(float(np.degrees(v.max() - v.min())), 2)
        rows_angle[key] = np.degrees(v)

    # torso counter-twist: chest yaw minus pelvis yaw, per frame
    def side_axis(Rm):
        v = Rm @ np.array([1.0, 0.0, 0.0])
        n = math.hypot(v[0], v[1])
        return (v[0] / n, v[1] / n) if n > 1e-9 else (1.0, 0.0)

    def yaw(Rm):
        s = side_axis(Rm)
        return math.degrees(math.atan2(s[1], s[0]))

    tw = []
    for r in rows:
        d = yaw(r["chestR"]) - yaw(r["hipsR"])
        d = (d + 180.0) % 360.0 - 180.0
        tw.append(d)
    summ["torso_twist_range_deg"] = round(float(max(tw) - min(tw)), 2)
    hips_p = np.array([r["hip"] for r in rows])
    summ["hip_motion_xyz_m"] = [round(float(hips_p[:, i].max() - hips_p[:, i].min()), 4)
                                for i in range(3)]
    hv = np.array([r["headv"] for r in rows])
    G = hv @ hv.T
    summ["head_sway_deg"] = round(float(np.degrees(np.arccos(np.clip(G.min(), -1, 1)))), 2)

    aL = rows_angle["armL"]; aR = rows_angle["armR"]
    tL = rows_angle["thighL"]; tR = rows_angle["thighR"]

    def corr(a, b):
        a = a - a.mean(); b = b - b.mean()
        den = np.linalg.norm(a) * np.linalg.norm(b)
        return round(float(a @ b / den), 3) if den > 1e-12 else None

    summ["corr_armL_thighR"] = corr(aL, tR)
    summ["corr_armR_thighL"] = corr(aR, tL)
    return summ


def _rename_glb_animation(path, new_name, extras=None):
    """Rewrite the glTF JSON chunk so the animation has a meaningful name."""
    import struct
    d = bytearray(open(path, "rb").read())
    _, _, _ = struct.unpack("<III", d[:12])
    off, jo, jl = 12, None, None
    while off < len(d):
        clen, ctype = struct.unpack("<II", d[off:off + 8])
        if ctype == 0x4E4F534A:
            jo, jl = off, clen
            break
        off += 8 + clen
    if jo is None:
        return
    j = json.loads(bytes(d[jo + 8:jo + 8 + jl]).decode("utf-8"))
    for a in j.get("animations", []):
        a["name"] = new_name
        if extras:
            a["extras"] = extras
    new = json.dumps(j, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    new += b" " * ((4 - len(new) % 4) % 4)
    out = bytearray(d[:12])
    out += struct.pack("<II", len(new), 0x4E4F534A) + new
    out += d[jo + 8 + jl:]
    struct.pack_into("<I", out, 8, len(out))
    open(path, "wb").write(bytes(out))


def retarget_bvh(blend_path, bvh_path, out_path, mapping=None, *,
                 n_frames=None, mode="auto", ankle_gain=1.0, window=None,
                 twist_clamp_deg=75.0, align_facing=False, action_name=None, ground_z=DEFAULT_GROUND_Z,
                 save_glb=True, glb_path=None, mute_ik=True, fps=24,
                 measure=True, verbose=True):
    """Retarget one BVH onto the rig in `blend_path`.

    Parameters
    ----------
    blend_path : str   source .blend containing an armature + a skinned mesh
    bvh_path   : str   source BVH
    out_path   : str   destination .blend
    mapping    : list  [(target_bone, src_joint, 'aim'|'local', dir_child)]
    n_frames   : int   output frame count (None = auto: one cycle, or duration*fps)
    mode       : 'auto' | 'cycle' | 'clip'
    ankle_gain : 0.0 = no source foot roll, 1.0 = faithful
    ground_z   : z the lowest vertex gets pinned to (None = rest mesh minimum)

    Returns
    -------
    dict with info / amp / metrics and the written paths.
    """
    import bpy
    from mathutils import Matrix, Quaternion, Vector

    bvh_path = str(bvh_path)
    b = BVH(bvh_path)
    if verbose:
        print("[retarget] %s  frames=%d fps=%.1f" % (
            os.path.basename(bvh_path), b.n_frames, b.fps()))

    bpy.ops.wm.open_mainfile(filepath=str(blend_path))
    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    mesh_ob = next(o for o in bpy.data.objects
                   if o.type == "MESH" and any(m.type == "ARMATURE" for m in o.modifiers))
    sc = bpy.context.scene
    sc.render.fps = fps
    rig = read_rig_rest(arm)

    if ground_z is None:
        ground_z = float(_eval_world_verts(mesh_ob)[:, 2].min())

    resolved_map = mapping or DEFAULT_MAPPING
    missing = [t for t, _, _, _ in resolved_map if t not in rig]
    if missing:
        raise KeyError("target rig is missing bones: %s" % missing)

    plan = plan_retarget(b, rig, resolved_map, n_frames=n_frames, mode=mode,
                         ankle_gain=ankle_gain, window=window, fps_out=fps,
                         verbose=verbose, twist_clamp_deg=twist_clamp_deg,
                         align_facing=align_facing)
    F = plan["n_frames"]
    delta = plan["delta"]
    root_off = plan["root_off"]
    order = plan["order"]
    info = plan["info"]

    # ---- mute IK so FK legs win ----
    ik_muted = []
    if mute_ik:
        for bn in ("bone_45", "bone_49"):
            for c in arm.pose.bones[bn].constraints:
                c.influence = 0.0
                c.mute = True
                ik_muted.append((bn, c.name))

    Rr = {bn: np.array(rig[bn]["rot3"], dtype=np.float64) for bn in rig}

    # ---- solve local basis quaternions (with hemisphere continuity) ----
    quats = {}
    for bone in order:
        parent = rig[bone]["parent"]
        Dp = delta.get(parent)
        Db = delta[bone]
        Rb = Rr[bone]
        qs, prev = [], None
        for f in range(F):
            rel = (Dp[f].T @ Db[f]) if Dp is not None else Db[f]
            Br = Rb.T @ rel @ Rb
            q = Matrix([[float(Br[r][c]) for c in range(3)] for r in range(3)]).to_quaternion()
            if prev is not None and q.dot(prev) < 0:
                q = Quaternion((-q.w, -q.x, -q.y, -q.z))
            qs.append(q)
            prev = q
        quats[bone] = qs

    ad = arm.animation_data or arm.animation_data_create()
    nla_state = [(t.name, t.mute) for t in ad.nla_tracks]
    for t in ad.nla_tracks:
        t.mute = True
    for pb in arm.pose.bones:
        pb.rotation_mode = "QUATERNION"

    def apply_pose(k, gshift):
        for bone in order:
            arm.pose.bones[bone].rotation_quaternion = quats[bone][k % F]
        w = np.array([root_off[k % F, 0], root_off[k % F, 1], root_off[k % F, 2] + gshift])
        arm.pose.bones["bone_0"].location = Vector(Rr["bone_0"].T @ w)

    # ---- measure pass: per-frame ground clamp ----
    ad.action = None
    minz = []
    for f in range(F):
        apply_pose(f, 0.0)
        bpy.context.view_layer.update()
        minz.append(float(_eval_world_verts(mesh_ob)[:, 2].min()))
    minz = np.array(minz)
    gclamp = ground_z - minz
    if verbose:
        print("[retarget] ground clamp %.4f .. %.4f m (range %.4f)"
              % (gclamp.min(), gclamp.max(), gclamp.max() - gclamp.min()))

    # ---- commit ----
    name = action_name or ("BVH_" + os.path.splitext(os.path.basename(bvh_path))[0])
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    ad.action = act
    for f in range(F + 1):
        k = f % F
        for bone in order:
            arm.pose.bones[bone].rotation_quaternion = quats[bone][k]
            arm.pose.bones[bone].keyframe_insert("rotation_quaternion", frame=f + 1)
        w = np.array([root_off[k, 0], root_off[k, 1], root_off[k, 2] + gclamp[k]])
        arm.pose.bones["bone_0"].location = Vector(Rr["bone_0"].T @ w)
        arm.pose.bones["bone_0"].keyframe_insert("location", frame=f + 1)
    for fc in act.fcurves:
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"
    sc.frame_start = 1
    sc.frame_end = F + 1
    bpy.context.view_layer.update()

    # ---- metrics ----
    metrics = {}
    if measure:
        sc.frame_end = F
        metrics = measure_action(arm, mesh_ob, F, ground_z, sc)
        sc.frame_set(1)
        sc.frame_end = F + 1
        if verbose:
            print("[retarget] metrics: penetration=%s mm  float=%s mm  slip_max=%s mm  "
                  "support L/R=%s/%s" % (
                      metrics["penetration_mm"], metrics["max_float_mm"],
                      metrics["slip_mm_max"], metrics["support_frames_L"],
                      metrics["support_frames_R"]))

    out_path = str(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=out_path)

    result = dict(action=name, blend=out_path, n_frames=F, fps=fps,
                  info=info, amp=plan["amp"], metrics=metrics,
                  ground_z=ground_z, ik_muted=ik_muted,
                  nla_muted=nla_state, ankle_gain=ankle_gain,
                  mapping=[list(m) for m in plan["mapping"]])

    if save_glb:
        gp = glb_path or (os.path.splitext(out_path)[0] + ".glb")
        for e in ("IK_ankle.L", "IK_ankle.R"):
            o = bpy.data.objects.get(e)
            if o:
                o.animation_data_clear()
        bpy.ops.object.select_all(action="DESELECT")
        arm.select_set(True)
        mesh_ob.select_set(True)
        bpy.context.view_layer.objects.active = arm
        bpy.ops.export_scene.gltf(
            filepath=gp, export_format="GLB", use_selection=True,
            export_animation_mode="ACTIVE_ACTIONS", export_animations=True,
            export_frame_range=True, export_bake_animation=False,
            export_apply=False, export_yup=True, export_skins=True,
            export_morph=False, export_materials="EXPORT")
        _rename_glb_animation(gp, name, extras={
            "source_bvh": os.path.basename(bvh_path),
            "source_frames": int(b.n_frames), "source_fps": round(b.fps(), 3),
            "out_frames": F, "out_fps": fps, "ankle_gain": float(ankle_gain),
            "mode": info["mode"], "loop": True})
        result["glb"] = gp
        result["glb_bytes"] = os.path.getsize(gp)
        if verbose:
            print("[retarget] wrote %s (%d bytes)" % (gp, result["glb_bytes"]))

    if verbose:
        print("[retarget] wrote %s" % out_path)
    return result












