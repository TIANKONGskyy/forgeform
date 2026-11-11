# -*- coding: utf-8 -*-
"""生成原创开机动画 JS v4（2026-08-11）
主题：天枢 —— 北斗第一星
设计：深空星海背景 + 北斗七星依次点亮连线 + 粒子从星海汇聚成"天枢"两字 + 星轨环绕
全 Canvas 原创，无版权素材
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

JS = r"""
<script>
(function() {
  'use strict';
  // ═══ 天枢 · 北斗第一星 开机动画（原创 v4）═══
  function waitFor(selector, cb, timeoutMs) {
    var t0 = Date.now();
    var iv = setInterval(function() {
      var el = document.querySelector(selector);
      if (el) { clearInterval(iv); cb(el); return; }
      if (Date.now() - t0 > timeoutMs) clearInterval(iv);
    }, 50);
  }

  // ── 按钮 tooltip（一并移入 head）──
  var TIPS = {
    'btn_enhance': '用 Ollama 把简短描述扩写成详细英文提示词（可选）',
    'btn_gen': '生成候选图，完成后在右侧画廊点击选择一张',
    'batch_set_btn': '把上方文本框里的提示词载入批量队列',
    'batch_add_btn': '把左侧当前描述词追加到队列末尾',
    'batch_start_btn': '开始顺序执行队列（运行期间建议勿手动操作）',
    'batch_stop_btn': '当前任务跑完后停止',
    'batch_clear_btn': '清空整个队列',
    'vid_btn': '从已选图片生成视频（MiniMax H3，约 5-10 分钟）',
    'td_btn': '从已选图片生成 3D（Hunyuan3D / TripoSplat，约 1-2 分钟）',
    'reset_btn': '清空当前工作区的图片/视频/3D 状态',
    'stl_btn': '把当前 3D 模型导出为可打印 STL',
    'pr_save_btn': '保存打印机信息（IP/序列号/访问码）',
    'pr_remove_btn': '从列表移除选中的打印机',
    'pr_status_btn': 'Ping 检测打印机是否在线',
    'pr_send_btn': '把当前 STL 发送到打印机开始打印',
    'rev_vid_3d_btn': '用上传视频的第一帧生成 3D 模型',
    'rev_stl_btn': '把上传/生成的 GLB 转换为可打印 STL',
    'repair_btn': '诊断 ComfyUI/Ollama/模型/目录并自动修复',
    'startup_btn': '完整自检：目录/服务/依赖/模型/GPU/文件库'
  };
  function applyTips() {
    var applied = 0;
    document.querySelectorAll('button[id]').forEach(function(btn) {
      if (TIPS[btn.id]) { btn.setAttribute('title', TIPS[btn.id]); applied++; }
    });
    return applied;
  }
  function tipLoop() {
    var n = applyTips();
    if (n < Object.keys(TIPS).length) setTimeout(tipLoop, 400);
  }
  waitFor('.gradio-container', function() { tipLoop(); }, 15000);
  setTimeout(tipLoop, 2000);

  // ═══ 天枢开机动画 ═══
  function bootAnimation() {
    var boot = document.getElementById('harmonyBoot');
    var canvas = document.getElementById('bootCanvas');
    if (!boot || !canvas) return false;
    var ctx = canvas.getContext('2d');
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var W, H, CX, CY;

    function resize() {
      W = window.innerWidth; H = window.innerHeight;
      canvas.width = W * dpr; canvas.height = H * dpr;
      canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      CX = W / 2; CY = H / 2;
      buildScene();
    }
    resize();
    window.addEventListener('resize', resize);

    // ── 场景元素 ──
    var stars = [];        // 背景星海
    var dipper = [];       // 北斗七星
    var particles = [];    // 汇聚粒子
    var rings = [];        // 星轨环
    var textTargets = [];  // "天枢"文字像素目标
    var phase = 'stars';   // stars → dipper → gather → done
    var startTime = performance.now();
    var dipperLit = 0;
    var dipperDone = false;
    var gatherDone = false;

    // 北斗七星位置（相对屏幕）
    function buildDipper() {
      var s = Math.min(W, H) * 0.16;
      var ox = CX - s * 2.2, oy = CY - s * 0.3;
      // 北斗七星相对坐标（斗柄 3 颗 + 斗身 4 颗）
      var pts = [
        [-2.6, 0.9], [-1.8, 1.3], [-1.0, 1.0],  // 斗柄
        [-0.3, 0.3], [0.5, -0.2], [0.2, -1.2], [1.1, -1.6]  // 斗身
      ];
      dipper = pts.map(function(p, i) {
        return { x: ox + p[0] * s, y: oy + p[1] * s, i: i, lit: false };
      });
    }

    // 背景星海（随机小星）
    function buildStars() {
      stars = [];
      var n = Math.floor(W * H / 9000);
      for (var i = 0; i < n; i++) {
        stars.push({
          x: Math.random() * W, y: Math.random() * H,
          r: Math.random() * 1.3 + 0.3,
          tw: Math.random() * Math.PI * 2,
          sp: 0.5 + Math.random() * 1.5
        });
      }
    }

    // "天枢"文字采样
    function sampleText() {
      var off = document.createElement('canvas');
      off.width = 900; off.height = 300;
      var octx = off.getContext('2d');
      octx.fillStyle = '#fff';
      octx.font = '700 220px "Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif';
      octx.textAlign = 'center';
      octx.textBaseline = 'middle';
      octx.fillText('天枢', 450, 150);
      var img = octx.getImageData(0, 0, off.width, off.height).data;
      var pts = [];
      for (var step = 4; step <= 24 && pts.length < 260; step++) {
        pts = [];
        for (var y = 0; y < off.height; y += step) {
          for (var x = 0; x < off.width; x += step) {
            if (img[(y * off.width + x) * 4 + 3] > 100) pts.push({x: x, y: y});
          }
        }
      }
      while (pts.length > 260) pts.splice(Math.floor(Math.random() * pts.length), 1);
      var scale = Math.min(W / 900, H / 300) * 0.8;
      pts.forEach(function(p) {
        p.tx = CX + (p.x - 450) * scale;
        p.ty = CY + (p.y - 150) * scale - H * 0.04;
      });
      return pts;
    }

    function buildScene() {
      buildStars();
      buildDipper();
      textTargets = sampleText();
      // 粒子从四周生成（汇聚用）
      particles = textTargets.map(function(t, i) {
        var side = Math.floor(Math.random() * 4);
        var x, y, m = Math.min(W, H) * 0.2;
        if (side === 0) { x = Math.random() * W; y = -m; }
        else if (side === 1) { x = W + m; y = Math.random() * H; }
        else if (side === 2) { x = Math.random() * W; y = H + m; }
        else { x = -m; y = Math.random() * H; }
        return { x: x, y: y, tx: t.tx, ty: t.ty, r: 1.2 + Math.random() * 1.8,
                 vx: 0, vy: 0, delay: Math.random() * 0.5 };
      });
      // 星轨环
      rings = [0.55, 0.75, 0.95].map(function(rr, i) {
        return { r: Math.min(W, H) * rr * 0.5, sp: 0.15 + i * 0.08, a: 0 };
      });
    }

    // ── 绘制 ──
    function draw(t) {
      var el = (t - startTime) / 1000;
      ctx.clearRect(0, 0, W, H);
      // 深空渐变
      var bg = ctx.createRadialGradient(CX, CY, 0, CX, CY, Math.max(W, H) * 0.7);
      bg.addColorStop(0, '#151627');
      bg.addColorStop(0.6, '#0d0e18');
      bg.addColorStop(1, '#06070c');
      ctx.fillStyle = bg;
      ctx.fillRect(0, 0, W, H);

      // 背景星海（闪烁）
      for (var i = 0; i < stars.length; i++) {
        var s = stars[i];
        var a = 0.25 + 0.55 * (0.5 + 0.5 * Math.sin(t / 1000 * s.sp + s.tw));
        ctx.globalAlpha = a;
        ctx.fillStyle = '#cdd3ff';
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;

      // 星轨环（缓慢旋转的虚线圆环）
      for (var ri = 0; ri < rings.length; ri++) {
        var rg = rings[ri];
        rg.a += rg.sp * 0.016;
        ctx.strokeStyle = 'rgba(139,92,246,0.12)';
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 10]);
        ctx.beginPath();
        ctx.arc(CX, CY, rg.r, rg.a, rg.a + Math.PI * 1.2);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      // 北斗七星（依次点亮 + 连线）
      var dipperProgress = Math.min(el / 2.4, 1);  // 2.4s 内点亮
      var litCount = Math.floor(dipperProgress * 7);
      for (var d = 0; d < dipper.length; d++) {
        var star = dipper[d];
        var lit = d < litCount;
        var twinkle = 0.7 + 0.3 * Math.sin(t / 500 + d);
        ctx.globalAlpha = lit ? twinkle : 0.15;
        ctx.fillStyle = lit ? '#a78bfa' : '#4a4a6a';
        var rad = lit ? (4 + Math.sin(t / 300 + d) * 1.2) : 2.5;
        ctx.beginPath();
        ctx.arc(star.x, star.y, rad, 0, Math.PI * 2);
        ctx.fill();
        // 光晕
        if (lit) {
          var g = ctx.createRadialGradient(star.x, star.y, 0, star.x, star.y, rad * 5);
          g.addColorStop(0, 'rgba(167,139,250,0.5)');
          g.addColorStop(1, 'rgba(167,139,250,0)');
          ctx.fillStyle = g;
          ctx.beginPath();
          ctx.arc(star.x, star.y, rad * 5, 0, Math.PI * 2);
          ctx.fill();
        }
        // 连线（已点亮的星之间）
        if (d > 0 && lit && d - 1 < litCount) {
          var p1 = dipper[d - 1], p2 = star;
          ctx.strokeStyle = 'rgba(167,139,250,0.35)';
          ctx.lineWidth = 1.2;
          ctx.beginPath();
          ctx.moveTo(p1.x, p1.y);
          ctx.lineTo(p2.x, p2.y);
          ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;

      // 北斗 → 汇聚过渡（第 7 颗亮起后 0.5s 开始汇聚）
      var gatherStart = 2.9;
      if (el > gatherStart && !gatherDone) {
        // 汇聚粒子：从四周飞向文字
        var allNear = true;
        for (var pi = 0; pi < particles.length; pi++) {
          var p = particles[pi];
          if (el < gatherStart + p.delay) continue;
          var dx = p.tx - p.x, dy = p.ty - p.y;
          var dist = Math.sqrt(dx * dx + dy * dy);
          if (dist > 1.5) {
            var ease = Math.min(0.05 + dist * 0.0003, 0.11);
            p.vx += dx * ease; p.vy += dy * ease;
            p.vx *= 0.85; p.vy *= 0.85;
            p.x += p.vx; p.y += p.vy;
            allNear = false;
          } else {
            p.x = p.tx; p.y = p.ty;
          }
          // 绘制粒子（光晕）
          var grd = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r * 3.5);
          grd.addColorStop(0, 'rgba(196,181,253,0.95)');
          grd.addColorStop(0.5, 'rgba(139,92,246,0.4)');
          grd.addColorStop(1, 'rgba(139,92,246,0)');
          ctx.fillStyle = grd;
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.r * 3.5, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = '#e8e2ff';
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
          ctx.fill();
        }
        if (allNear && !gatherDone) {
          gatherDone = true;
          // 汇聚完成 → 文字脉冲发光 → 淡出
          setTimeout(function() { boot.classList.add('fade-out'); }, 700);
          setTimeout(function() { if (boot.parentNode) boot.parentNode.removeChild(boot); }, 1700);
        }
      }

      // 底部文字
      ctx.globalAlpha = Math.min(el / 1.5, 1);
      ctx.fillStyle = '#8a8aa0';
      ctx.font = '500 12px "Microsoft YaHei", sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('A I  创 意 工 坊', CX, H - H * 0.08);
      ctx.fillStyle = '#55556a';
      ctx.font = '400 10px "Microsoft YaHei", sans-serif';
      ctx.fillText('天 枢 · 北 斗 第 一 星', CX, H - H * 0.055);
      ctx.globalAlpha = 1;

      // 兜底 8s 强制消失
      if (el > 8 && !boot.classList.contains('fade-out')) boot.classList.add('fade-out');
      if (!boot.parentNode) return;
      requestAnimationFrame(draw);
    }
    requestAnimationFrame(draw);
    return true;
  }

  var bootTries = 0;
  var bootIv = setInterval(function() {
    bootTries++;
    if (bootAnimation()) clearInterval(bootIv);
    else if (bootTries > 120) clearInterval(bootIv);
  }, 100);
})();
</script>
"""
print(JS)
