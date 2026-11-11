"""决定性验证：新编的 sm_70 内核到底能不能在 V100 上跑。

★ 为什么这就是判据（2026-10-15）
  预编译的 wheel 只编了 **sm_86**，本卡是 **sm_70（Tesla V100）**。
  那个包的表现很迷惑人：
      · DLL 能加载           → 加载阶段不报错
      · Python 能 import     → import 阶段不报错
      · 属性也在（rasterize）→ 反射阶段不报错
  直到**真正调用内核**才抛：
      CUDA error: no kernel image is available for execution on the device
                           (cudaErrorNoKernelImageForDevice)
  所以"能 import"完全不能说明问题，必须真跑一次 GPU 调用。

这里造一个最小合法的输入（单三角形），调 rasterize。
跑通 → sm_70 内核镜像在，TexGen 的最后一块堵点通了。
报 no kernel image → 说明编进去的不是 sm_70（那就得回头看 TORCH_CUDA_ARCH_LIST）。
"""
import sys
import pathlib

SRC = pathlib.Path(
    r"D:\ComfyUI-New\custom_nodes\ComfyUI-3D-Pack-main\Gen_3D_Modules"
    r"\Hunyuan3D_2_1\hy3dpaint\custom_rasterizer"
)
# 让 custom_rasterizer_kernel 解析到**刚编出来的**那份，而不是 site-packages 里的 sm_86 旧件
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "custom_rasterizer"))

import torch  # noqa: E402

print(f"  torch {torch.__version__} · sm_70 in arch_list = {'sm_70' in torch.cuda.get_arch_list()}")
print(f"  设备: {torch.cuda.get_device_name(0)} · cc {torch.cuda.get_device_capability(0)}")

import custom_rasterizer_kernel as k  # noqa: E402

print(f"  载入的内核模块: {k.__file__}")
print(f"  有 rasterize_image 属性: {hasattr(k, 'rasterize_image')}")

# 一个三角形，放在 CUDA 上
pos = torch.tensor([[[0.0, 0.0, 1.0], [1.0, 0.0, 1.0], [0.0, 1.0, 1.0]]], device="cuda", dtype=torch.float32)
tri = torch.tensor([[0, 1, 2]], device="cuda", dtype=torch.int32)
H = W = 64

print("  → 调用 rasterize_image（关键一步）...")
try:
    # clamp_depth 在 C++ 侧是按 int64 解的（data_ptr<int64_t>），空张量也得是 int64
    clamp = torch.zeros(0, dtype=torch.int64, device="cuda")
    findices, barycentric = k.rasterize_image(pos[0], tri, clamp, W, H, 1e-6, 0)
    torch.cuda.synchronize()
    print("  ✅ 内核在 V100 上跑通了！")
    print(f"     findices {tuple(findices.shape)} {findices.dtype} · barycentric {tuple(barycentric.shape)}")
    nz = int((findices > 0).sum().item())
    print(f"     被覆盖的像素数 = {nz}（单三角形在 64×64 里应该是几百个像素，非 0 才算真的画了）")
    print(f"     barycentric 非零元素 = {int((barycentric != 0).sum().item())}")
    ok = True
except Exception as e:
    print(f"  ❌ 失败: {type(e).__name__}: {str(e)[:300]}")
    ok = False

print()
print("  ★ 结论:", "sm_70 内核可用 —— TexGen 的最后一块堵点通了" if ok else "内核仍不可用，需回查编入的架构")
sys.exit(0 if ok else 1)
