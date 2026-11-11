"""把 custom_rasterizer 里的宏 INT64 改名为 CR_INT64。

★ 为什么（2026-10-15）
  rasterizer.h 里定义了 `#define INT64 unsigned long long`，而这个名字
  **和 Windows SDK 撞了**：Windows Kits 的 basetsd.h 里有
      typedef signed __int64 INT64;
  宏一展开就变成 `typedef signed __int64 unsigned long long;`，于是编译报：
      basetsd.h(78): error C2042: signed/unsigned 关键字互相排斥
      basetsd.h(78): error C2632: "__int64"后面的"long"非法

  这个冲突一直存在，只是以前靠"torch/extension.h 先把 SDK 头引完、宏才定义"
  的顺序侥幸躲过去。现在为了绕开 MSVC 的 C2872 把那支头换掉了，
  顺序一变就暴露出来 —— 属于本来就该修的东西。

  改法：改名成 CR_INT64（custom_rasterizer 前缀，不会和任何平台头冲突）。
        纯词法替换，语义完全不变。
"""
import pathlib

D = pathlib.Path(
    r"D:\ComfyUI-New\custom_nodes\ComfyUI-3D-Pack-main\Gen_3D_Modules"
    r"\Hunyuan3D_2_1\hy3dpaint\custom_rasterizer\lib\custom_rasterizer_kernel"
)

total = 0
for f in ("rasterizer.h", "rasterizer.cpp", "rasterizer_gpu.cu"):
    p = D / f
    src = p.read_text(encoding="utf-8", errors="surrogateescape")
    n = src.count("INT64")
    if not n:
        continue
    # 注意：只替换独立的 INT64 记号，MAXINT 之类不受影响（它们是不同词）
    src = src.replace("INT64", "CR_INT64")
    p.write_text(src, encoding="utf-8", errors="surrogateescape")
    print(f"  {f:<20} {n} 处")
    total += n
print(f"  合计 {total} 处")

# 在定义处加一行说明
h = D / "rasterizer.h"
s = h.read_text(encoding="utf-8", errors="surrogateescape")
old = "#define CR_INT64 unsigned long long"
new = (
    "// [win-msvc 2026-10-15] 原名 INT64，与 Windows SDK 的 basetsd.h 冲突\n"
    "//   （那里有 `typedef signed __int64 INT64;`，宏展开后语法直接崩）。\n"
    "//   改名加 CR_ 前缀。语义不变。\n"
    "#define CR_INT64 unsigned long long"
)
if old in s and "原名 INT64" not in s:
    h.write_text(s.replace(old, new, 1), encoding="utf-8", errors="surrogateescape")
    print("  已在定义处加注释")
