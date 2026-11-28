铸想 ForgeForm 一键安装（Windows）

怎么用
  1. 解压本压缩包到任意目录
  2. 双击「铸想安装.bat」
  3. 等它跑完（会显示进度），然后按提示启动

它会做什么
  - 检查系统 / 显卡 / 磁盘 / 网络
  - 没有 Python 3.10+ 就自动装一个便携版（不碰系统 Python，不需要管理员）
  - GitHub 加速（gh-proxy -> ghfast -> 直连），多连接下载主包
  - tar 主包支持「边下边解」：下载和解压同时进行
  - 装 pip 依赖，跑自检
  - 主程序放在 %USERPROFILE%\ForgeForm

关于模型
  安装器只装运行环境（约 1 GB）。出图/建模权重要另外下：
    cd %USERPROFILE%\ForgeForm
    python tools\first_run.py --with-image

要求
  - Windows 10/11 x64
  - 磁盘 ≥ 30 GB（含权重）
  - NVIDIA 显卡 8 GB 显存起；24 GB 可跑更大的模型
