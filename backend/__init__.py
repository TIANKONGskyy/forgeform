"""AI Workshop Backend — modular pipeline architecture.

Components:
  - pipeline.py:    Pipeline orchestrator (state + stage dispatch)
  - comfy_client.py: Pure ComfyUI API client (submit/wait/download)
  - stages/:        Plugin stages (image, video, threed, stl_export)
  - monitor.py:     Hardware monitoring (psutil + nvidia-smi)
  - logger.py:      Structured JSON logging
  - stl_convert.py: GLB→STL (legacy, now in stages/stl_export.py)
  - video_utils.py: Video frame extraction (ffmpeg)
"""
