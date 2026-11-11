# 作品展示

`index.html` 是这个目录的入口，浏览器直接打开就行（纯静态，无外部依赖、不用起服务）。

```powershell
start docs/showcase/index.html
```

## 这里的数字是真的

页面上每个作品都是**这台机器上真跑出来的**，不是挑出来的历史图：

- 提示词 → 出图 → 建模 → 导出 STL，一次跑完
- 耗时是实测墙钟时间，面数/水密来自 `trimesh` 直接读产物
- 数据存在每个作品目录的 `job.json` 里，页面由 `build_page.py` 从这些文件渲染

## 目录结构

```
showcase/
  index.html          生成的页面（可以提交进仓库）
  build_page.py       生成器，改完数据重跑它
  <slug>/
    job.json          这个作品的实测记录（耗时/面数/水密/尺寸）
    image.png         原始出图（1024²，不入库）
    image.jpg         页面用的压缩版（入库）
    model.glb         生成的网格（不入库，几 MB）
    model.stl         导出的打印件（不入库，几 MB）
```

`image.png` / `model.glb` / `model.stl` 都在 `.gitignore` 里 —— 仓库只留 `image.jpg` 和
`job.json`，这样别人 clone 下来能看页面、能看数据，但不会被几十 MB 的产物拖成胖仓库。

想拿可打印文件，从 Releases 里下。

## 重新生成页面

```powershell
python docs/showcase/build_page.py
```

它会把所有 `*/job.json` 里的记录重新灌进 HTML，并把 `image.png` 压成 900px 的 `image.jpg`。
换一批作品只要改 `job.json`（或重跑生成脚本），页面会自动跟着变。

## 加一个新作品

1. 在 `showcase_gen.py` 的 `JOBS` 里加一条（提示词、质量档、目标尺寸）
2. 跑 `python showcase_gen.py --only <slug>`
3. 跑 `build_page.py`

生成脚本在开发者本地，不在仓库里 —— 因为它依赖铸想的 `backend` 包和一台装好 ComfyUI 的机器。
