"""Minimal dependency-free BVH parser + skeleton math.

Pure numpy. Used both standalone and inside Blender (Blender bundles numpy).
"""
import numpy as np
import re


class Joint:
    __slots__ = ("name", "parent", "offset", "channels", "index", "children", "ch_start")

    def __init__(self, name, parent, offset, channels):
        self.name = name
        self.parent = parent
        self.offset = offset
        self.channels = channels
        self.children = []
        self.index = -1
        self.ch_start = 0

    def __repr__(self):
        return "<Joint %s idx=%d ch=%s>" % (self.name, self.index, self.channels)


def _euler_to_mat(axis_order, degs):
    """BVH channel order is applied as written. Build R = R1 @ R2 @ R3 (first listed
    channel is the outermost / applied last in matrix composition, matching the
    common BVH convention where the listed order is the multiplication order)."""
    mats = []
    for ax, d in zip(axis_order, degs):
        t = np.radians(d)
        c, s = np.cos(t), np.sin(t)
        if ax == 'X':
            m = np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)
        elif ax == 'Y':
            m = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)
        else:
            m = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)
        mats.append(m)
    r = np.eye(3)
    for m in mats:
        r = r @ m
    return r


class BVH:
    def __init__(self, path):
        with open(path, 'r', errors='replace') as f:
            text = f.read()
        self.path = path
        self.joints = []
        self.index = {}
        self._parse_hierarchy(text)
        self._parse_motion(text)
        self._compute_channels()

    # ---------------- parsing ----------------
    def _parse_hierarchy(self, text):
        tokens = text.replace('\t', ' ').split()
        i = 0
        stack = []
        root = None
        while i < len(tokens):
            tok = tokens[i]
            up = tok.upper()
            if up == 'HIERARCHY':
                i += 1
                continue
            if up == 'MOTION':
                break
            if up in ('ROOT', 'JOINT'):
                name = tokens[i + 1]
                name = re.sub(r'[^A-Za-z0-9_.\-]', '_', name)
                parent = stack[-1] if stack else None
                j = Joint(name, parent, np.zeros(3), [])
                if parent is None:
                    root = j
                else:
                    parent.children.append(j)
                stack.append(j)
                self.joints.append(j)
                self.index[name] = j
                i += 2
                continue
            if up == 'END':
                # End Site: skip the block but keep note of it as a child leaf
                depth = 0
                while i < len(tokens):
                    if tokens[i] == '{':
                        depth += 1
                    elif tokens[i] == '}':
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
                continue
            if up == 'OFFSET':
                o = np.array([float(tokens[i + 1]), float(tokens[i + 2]), float(tokens[i + 3])])
                if stack:
                    stack[-1].offset = o
                i += 4
                continue
            if up == 'CHANNELS':
                n = int(tokens[i + 1])
                ch = tokens[i + 2:i + 2 + n]
                if stack:
                    stack[-1].channels = ch
                i += 2 + n
                continue
            if tok == '{':
                i += 1
                continue
            if tok == '}':
                if stack:
                    stack.pop()
                i += 1
                continue
            i += 1
        self.root = root

    def _parse_motion(self, text):
        m = re.search(r'MOTION\s*\n\s*Frames:\s*(\d+)\s*\n\s*Frame\s*Time:\s*([0-9.eE+-]+)', text)
        if not m:
            raise RuntimeError("no MOTION block")
        self.n_frames = int(m.group(1))
        self.frame_time = float(m.group(2))
        body = text[m.end():]
        vals = np.array(body.split(), dtype=np.float64)
        self.dof = sum(len(j.channels) for j in self.joints)
        self.motion = vals[:self.n_frames * self.dof].reshape(self.n_frames, self.dof)

    def _compute_channels(self):
        c = 0
        for j in self.joints:
            j.index = len([x for x in self.joints[:0]])  # placeholder
        for i, j in enumerate(self.joints):
            j.index = i
        for j in self.joints:
            j.ch_start = c
            c += len(j.channels)
        self.dof = c

    # ---------------- forward kinematics ----------------
    def rest_positions(self):
        """Joint world positions in the rest pose (offsets only)."""
        pos = {}
        for j in self._bfs():
            p = j.parent
            base = pos[p.name] if p is not None else np.zeros(3)
            pos[j.name] = base + j.offset
        return pos

    def _bfs(self):
        out = []
        q = [self.root]
        while q:
            j = q.pop(0)
            out.append(j)
            q.extend(j.children)
        return out

    def frames_world(self, frames=None, conv='post'):
        """Return dict name -> (F,3) world positions and dict name -> (F,3,3) world rotations.

        conv controls how a joint's multiple rotation channels compose:
          'post' -> R = R_c0 @ R_c1 @ R_c2   (listed order, left-to-right)
          'pre'  -> R = R_c2 @ R_c1 @ R_c0   (listed order applied innermost-first)
        CMU BVH channels are Z,Y,X; 'post' means Rz@Ry@Rx."""
        if frames is None:
            frames = range(self.n_frames)
        frames = list(frames)
        order = self._bfs()
        F = len(frames)
        pos = {j.name: np.zeros((F, 3)) for j in order}
        rot = {j.name: np.zeros((F, 3, 3)) for j in order}
        for j in order:
            sl = slice(j.ch_start, j.ch_start + len(j.channels))
            data = self.motion[np.array(frames), :][:, sl]
            # translation channels
            trans = np.zeros((F, 3))
            axes = []
            for k, ch in enumerate(j.channels):
                if ch.endswith('position'):
                    trans[:, 'XYZ'.index(ch[0])] = data[:, k]
                else:
                    axes.append((ch[0], k))
            local_r = np.zeros((F, 3, 3))
            if axes:
                # build per-frame euler with only the rotation channels that exist
                R = np.tile(np.eye(3), (F, 1, 1))
                for ax, k in axes:
                    t = np.radians(data[:, k])
                    c, s = np.cos(t), np.sin(t)
                    m = np.zeros((F, 3, 3))
                    if ax == 'X':
                        m[:, 0, 0] = 1; m[:, 1, 1] = c; m[:, 1, 2] = -s; m[:, 2, 1] = s; m[:, 2, 2] = c
                    elif ax == 'Y':
                        m[:, 1, 1] = 1; m[:, 0, 0] = c; m[:, 0, 2] = s; m[:, 2, 0] = -s; m[:, 2, 2] = c
                    else:
                        m[:, 2, 2] = 1; m[:, 0, 0] = c; m[:, 0, 1] = -s; m[:, 1, 0] = s; m[:, 1, 1] = c
                    if conv == 'post':
                        R = R @ m
                    else:
                        R = m @ R
                local_r = R
            else:
                local_r[:] = np.eye(3)
            p = j.parent
            if p is None:
                base_p = np.zeros((F, 3))
                base_r = np.tile(np.eye(3), (F, 1, 1))
            else:
                base_p = pos[p.name]
                base_r = rot[p.name]
            # world position of this joint = parent pos + parent_rot @ (offset + trans)
            off = j.offset[None, :] + trans
            pos[j.name] = base_p + np.einsum('fij,fj->fi', base_r, off)
            rot[j.name] = base_r @ local_r
        return pos, rot

    def fps(self):
        return 1.0 / self.frame_time if self.frame_time else 120.0


def swing_twist(rot_delta, axis):
    """Decompose rotation R into twist about `axis` (unit) and residual swing.
    Returns (swing, twist_angle).  R = Tw @ Sw is not exact; we use the standard
    decomposition  R = Tw(axis, angle) @ Sw  with Sw leaving axis invariant."""
    v = np.cross(axis, rot_delta[:, :].T @ axis) if False else None
    # projection method: twist quaternion
    from math import sqrt
    q = mat_to_quat(rot_delta)
    w, x, y, z = q
    d = x * axis[0] + y * axis[1] + z * axis[2]
    tw = np.array([w, d * axis[0], d * axis[1], d * axis[2]])
    n = np.linalg.norm(tw)
    if n < 1e-12:
        tw = np.array([1.0, 0, 0, 0])
    else:
        tw = tw / n
    Tw = quat_to_mat(tw)
    Sw = Tw.T @ rot_delta
    return Sw, tw


def mat_to_quat(m):
    t = np.trace(m)
    if t > 0:
        s = np.sqrt(t + 1.0) * 2
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    q = np.array([w, x, y, z])
    return q / np.linalg.norm(q)


def quat_to_mat(q):
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def minimal_rotation(a, b):
    """Smallest rotation taking unit vector a to unit vector b."""
    a = a / np.linalg.norm(a)
    b = b / np.linalg.norm(b)
    v = np.cross(a, b)
    c = float(a @ b)
    if np.linalg.norm(v) < 1e-9:
        if c > 0:
            return np.eye(3)
        # 180 deg: pick any perpendicular axis
        perp = np.array([1.0, 0, 0])
        if abs(a[0]) > 0.9:
            perp = np.array([0, 1.0, 0])
        axis = np.cross(a, perp)
        axis /= np.linalg.norm(axis)
        return quat_to_mat(np.array([0.0, *axis]))
    axis = v / np.linalg.norm(v)
    ang = np.arctan2(np.linalg.norm(v), c)
    return quat_to_mat(np.array([np.cos(ang / 2), *(axis * np.sin(ang / 2))]))
