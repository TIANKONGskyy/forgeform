"""把 custom_rasterizer 源码里的 data_ptr<long> 改成 data_ptr<int64_t>（Windows 移植）。

★ 为什么（2026-10-15）
  Windows 是 LLP64 模型：`long` 是 **32 位**，而 Linux 上 `long` 是 64 位。
  torch 只为 `int64_t`（= `long long`，64 位）实例化了 `data_ptr<T>()`，
  所以在 Windows 上编出来会链接失败：
      grid_neighbor.obj : error LNK2001: 无法解析的外部符号
        "public: long * __cdecl at::TensorBase::data_ptr<long>(void)const"
  这些张量本来就是用 int64_options 建的，语义上就是 int64，
  写成 `long` 只是 Tencent 在 Linux 上开发时"顺手"的写法。

  改法：`data_ptr<long>` → `data_ptr<int64_t>`，
        并把接收它的 `long* nptr/dptr` 声明一并改成 `int64_t*`（否则类型不匹配）。
  另外像 `(INT64*)z_min.data_ptr<long>()` 这种强转也一并受益 ——
  原本在 Windows 上 `long` 是 32 位，强转成 INT64* 后按 8 字节读会**读错内存**
  （不只是链接不过，是个真 bug）。所以这不是"为了编过而编"，是修正确性。
"""
import re
import pathlib

D = pathlib.Path(
    r"D:\ComfyUI-New\custom_nodes\ComfyUI-3D-Pack-main\Gen_3D_Modules"
    r"\Hunyuan3D_2_1\hy3dpaint\custom_rasterizer\lib\custom_rasterizer_kernel"
)

total = 0
for f in ("grid_neighbor.cpp", "rasterizer.cpp", "rasterizer_gpu.cu"):
    p = D / f
    src = p.read_text(encoding="utf-8", errors="surrogateescape")
    n1 = src.count("data_ptr<long>")
    n2 = len(re.findall(r"\blong\s*\*\s*(nptr|dptr)", src))
    src = src.replace("data_ptr<long>", "data_ptr<int64_t>")
    src = re.sub(r"\blong\s*\*\s*(nptr|dptr)", r"int64_t* \1", src)
    p.write_text(src, encoding="utf-8", errors="surrogateescape")
    print(f"  {f:<22} data_ptr<long> {n1} 处 · long* 声明 {n2} 处")
    total += n1
print(f"  合计替换 {total} 处")
