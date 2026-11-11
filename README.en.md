# ForgeForm 铸想

**One sentence in → one printable STL out.** A conversation-driven 3D modeling workflow that
runs entirely on your own machine.

<img src="docs/assets/showcase-grid.jpg" width="620" alt="Four objects generated from four prompts" />

Built by **[ZYKT studio](https://zykt.space)**. AGPL-3.0.

You describe a part in plain language. It asks only what it needs to ask, picks sensible
defaults, generates a reference image, waits for your OK, reconstructs a mesh, seals it,
scales it to millimetres, drops it on the print bed — and hands you an STL your slicer accepts.

No cloud API. Local LLM for dialogue, local ComfyUI for diffusion and 3D reconstruction,
local `trimesh` / `pymeshlab` / `pymeshfix` for mesh repair.

---

## Why not just wire up a ComfyUI graph?

ComfyUI gives you models. ForgeForm gives you the **pipeline**:

- **Conversation is the parameter panel.** "Make it smaller, the desk kind" becomes `size_mm=90`.
  You never write a prompt template.
- **It refuses to waste GPU.** A follow-up question does not trigger a render. Only "show me"
  does. Autopilot can chain image → 3D after a 5-second grace period, and it can be cancelled.
- **Images are generated for reconstruction, not for looks.** The prompt layer forces
  single-object, plain-background, no-props constraints so the downstream model gets geometry,
  not a pretty desk scene with a lamp in it.
- **The STL is actually printable.** Every mesh passes a watertight gate: pymeshlab closes holes,
  then `is_watertight` re-checks boundary edges, then pymeshfix runs if edges survive —
  because "pymeshlab said it's closed" and "the slicer sees a solid" are different claims.
- **VRAM yields itself.** Dialogue model, image model, 3D model and video model take turns on one
  card. Each handoff unloads the previous one instead of OOM-ing.

## Measured on a V100 32G (single card)

| Step | Time |
|---|---|
| Image 1024², 20 steps | ≈ 56 s |
| Image 1024², 30 steps (incl. zh→en translation) | ≈ 140–170 s |
| 3D, fast / 256 octree | ≈ 160 s |
| 3D, high / 512 octree | ≈ 8–10 min |
| STL export (200k → 80k faces) | ≈ 3–6 s |
| Video 832×480 / 96 frames | ≈ 19 min |

Four end-to-end runs (image → mesh → STL) are recorded in
[`docs/showcase/`](docs/showcase/) — with per-object timings, face counts, watertightness,
and a four-view render of the actual STL.

## Requirements

- **Windows 10/11** (PowerShell launchers; the Python side is portable, the print path is not)
- **NVIDIA GPU** — 16 GB works with reduced settings, 24 GB+ recommended, 8 GB is image-only
- **RAM** 32 GB+ (the video stage peaks around 46 GB)
- **ComfyUI**, installed separately, with Hunyuan3D 2.x nodes for the 3D stage
- A local LLM endpoint (llama.cpp / vLLM / Ollama) — optional, degrades to a rule-based mode

## Install

```powershell
git clone https://github.com/TIANKONGskyy/forgeform.git
cd forgeform
pip install -r requirements.txt

# point at your ComfyUI
$env:COMFYUI_DIR  = "D:\ComfyUI"
$env:COMFY_PYTHON = "D:\ComfyUI\python.exe"   # optional

# check what's missing (does not load models, takes seconds)
python tools/doctor.py

python app.py        # → http://127.0.0.1:7860
```

`tools/doctor.py` checks Python, every dependency, the ComfyUI directory *and service*, your
`config.json`, **whether each configured model file actually exists**, writable output dirs and
the GPU — then prints a verdict. Run it first whenever something "doesn't work".

## Usage

```python
import sys; sys.path.insert(0, r"D:\forgeform")
from backend.pipeline import Pipeline

pipe = Pipeline()

imgs = pipe.generate_images("a hexagonal flange coupling, single piece",
                            count=1, model_key="qwen21_ref", quality_name="高质量")
r    = pipe.generate_3d(model_key="hunyuan21", quality_name="高质量",
                        image_path=imgs[0].output_path)
s    = pipe.export_stl(r.output_path, target_size_mm=80)
print(s.output_path)
```

`quality_name` is one of `快速` / `标准` / `高质量` / `极致`.
`model_key` must match a key under `config.json → models.<stage>`.

## Swapping models

Everything is configuration. `config.json → models.<stage>.<key>` holds per-model parameters;
the `backend` field decides which workflow builder runs:

| Stage | `backend` | Notes |
|---|---|---|
| image | `flux` | FLUX family (flux1-dev / Klein / FLUX.2 / Z-Image all use this path) |
| image | `qwen_image` | Qwen-Image, needs `clip_type: "qwen_image"`, supports reference-image i2i |
| 3d | `hy21` | Hunyuan3D 2.1 — highest quality |
| 3d | `multiview` | Hunyuan3D V2 multi-view — faster |
| 3d | `comfy3d_pipeline` | ComfyUI-3D-Pack V2-Fast pipeline |
| 3d | `native_comfyui` | TripoSplat and other native nodes |
| video | `h3` | MiniMax H3 Ref2VA |

Then set `stages.<stage>.default_model` to your new key. Weight files are referenced by
**filename only**; put them where ComfyUI expects them and confirm the node's dropdown sees them
before blaming ForgeForm.

Full field-by-field documentation: [`docs/CONFIG.md`](docs/CONFIG.md) (Chinese).

## What it can't do

- **Parametric CAD.** Tolerances, threads and snap-fits need `parts_cad/` (CadQuery), not
  image-to-3D. A flange that *looks* right is not a flange that *fits*.
- **Thin shells, hair, cloth, openwork.** Single-image reconstruction breaks on these.
- **Multi-part assemblies.** One object per run.
- **One-shot success on hard shapes.** Expect 2–3 retries with a cleaner input image.
- **Printer-ready guarantees.** The watertight gate fixes closure, not overhangs or thin walls.

More on boundaries and per-VRAM recommended settings: [`docs/CONFIG.md`](docs/CONFIG.md).

## For agents

[`skills/forgeform/SKILL.md`](skills/forgeform/SKILL.md) is an
[Agent Skills](https://agentskills.io)-compatible file that teaches an agent what this project is
(and who made it), how to install it, how to drive it through `Pipeline`, how to swap models, and
which failures have known root causes. Drop the folder into your agent's skills directory.

```text
Use skills/forgeform/SKILL.md. Start with python app.py → 127.0.0.1:7860;
generate via backend.pipeline.Pipeline (generate_images / generate_3d / export_stl);
model swaps are config-only — the backend field picks the workflow.
```

## Tests

```powershell
python -m pytest          # 172 cases, ~40 s, no GPU, no ComfyUI
python tools/verify.py    # 151 structural + behavioural assertions
```

The suite tests **pipeline contracts**, not pixels: button visibility vs. phase, output arity
matching `ui_outputs`, autopilot not double-firing, state not leaking across conversations,
numbers in the prompt not being rewritten by the LLM. Those bugs show up as "I clicked and
nothing happened" — only source-level assertions catch them.

## License

**AGPL-3.0**. Not a preference — a consequence. The watertight repair links `pymeshfix`
(AGPL-3.0) and `pymeshlab` (GPL-3.0), both copyleft. Declaring a permissive license over that
would be a sentence without legal effect.

Practical meaning: personal use, coursework, competitions, modifications, self-hosted team
deployment — all fine, no permission needed. Exposing it as a paid public service requires
publishing your modifications. Closed-source commercial distribution needs either a separate
agreement or removal of those two dependencies (they are lazily imported behind try/except, so
removing them degrades watertightness rather than breaking the app) — at which point the rest of
the code can be relicensed under MIT/Apache-2.0.

**Models and prints you make with it are yours.** AGPL governs the software, not its output.

Third-party components and their licenses: [NOTICE](NOTICE).
