import React from 'react';
import {
  AbsoluteFill,
  OffthreadVideo,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import {Backdrop, C, FONT, NumberRoll, Stat, Sweep, Words, useEnter} from '../ui';

/* ─────────── 1. 片头：一句话 → 可打印 ─────────── */

export const Hook: React.FC = () => {
  const f = useCurrentFrame();
  const lineW = interpolate(f, [46, 78], [0, 100], {
    extrapolateRight: 'clamp',
    extrapolateLeft: 'clamp',
  });
  const kick = 1 + 0.010 * Math.sin(f / 7) * Math.max(0, 1 - f / 70);

  return (
    <AbsoluteFill>
      <Backdrop hue={C.accent} />
      <AbsoluteFill
        style={{
          fontFamily: FONT,
          justifyContent: 'center',
          alignItems: 'center',
          transform: `scale(${kick})`,
        }}
      >
        <div
          style={{
            ...useEnter(0, 14),
            fontSize: 21,
            letterSpacing: 12,
            color: C.accent,
            fontWeight: 600,
            marginBottom: 44,
          }}
        >
          铸想 FORGEFORM
        </div>

        <div
          style={{
            ...useEnter(8, 30),
            fontSize: 96,
            fontWeight: 800,
            color: C.ink,
            letterSpacing: 2,
            whiteSpace: 'nowrap',
            lineHeight: 1.15,
          }}
        >
          一句话
        </div>

        <div
          style={{
            ...useEnter(30, 26),
            marginTop: 26,
            fontSize: 58,
            fontWeight: 600,
            color: C.dim,
            letterSpacing: 1,
            whiteSpace: 'nowrap',
          }}
        >
          到可打印的实体
        </div>

        <div
          style={{
            marginTop: 30,
            width: 460,
            height: 3,
            borderRadius: 2,
            background: 'rgba(120,140,170,0.18)',
            overflow: 'hidden',
          }}
        >
          <div style={{width: `${lineW}%`, height: '100%', background: C.accent}} />
        </div>

        <div
          style={{
            ...useEnter(62, 20),
            marginTop: 34,
            fontSize: 23,
            color: C.dim,
            letterSpacing: 3,
          }}
        >
          本地运行 · 不出机器 · AGPL-3.0
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ─────────── 2. 真实界面：打字 + 发送 ─────────── */

export const Screen: React.FC = () => {
  const f = useCurrentFrame();

  // 镜头语言：画面装进一个"窗口"里（有标题栏、圆角、投影），窗口本身保持不动，
  // 只在窗口内部推近到输入框 —— 这样推镜不会把边缘裁得七零八落。
  const push = interpolate(f, [46, 136], [1.0, 1.55], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const pull = interpolate(f, [196, 262], [1.55, 1.0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const zoom = f < 196 ? push : pull;

  // 输入框在录像里的相对位置（1920×1080）：(800, 660)
  const tx = (1580 / 2 - 800 / 1920 * 1580) / zoom;
  const ty = (890 / 2 - 660 / 1080 * 890) / zoom;

  return (
    <AbsoluteFill>
      <Backdrop hue={C.hot} />
      <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center'}}>
        <div
          style={{
            ...useEnter(0, 18),
            width: 1580,
            borderRadius: 20,
            overflow: 'hidden',
            boxShadow: '0 70px 180px rgba(0,0,0,0.7), 0 0 0 1px rgba(120,140,170,0.22)',
            background: '#0c111a',
          }}
        >
          {/* 假窗口标题栏，给"这是真软件"的暗示 */}
          <div
            style={{
              height: 38,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              paddingLeft: 18,
              borderBottom: '1px solid rgba(120,140,170,0.16)',
            }}
          >
            {['#ff5f57', '#febc2e', '#28c840'].map((c) => (
              <div key={c} style={{width: 11, height: 11, borderRadius: 6, background: c, opacity: 0.75}} />
            ))}
            <div
              style={{
                marginLeft: 16,
                fontFamily: FONT,
                fontSize: 14,
                color: 'rgba(147,161,181,0.75)',
                letterSpacing: 1,
              }}
            >
              铸想 · 创作空间
            </div>
          </div>
          <div style={{width: 1580, height: 890, overflow: 'hidden'}}>
            <div
              style={{
                width: 1580,
                height: 890,
                transform: `scale(${zoom}) translate(${tx}px, ${ty}px)`,
                transformOrigin: 'center',
              }}
            >
              <OffthreadVideo
                src={staticFile('session.mp4')}
                muted
                style={{width: 1580, height: 'auto', display: 'block'}}
              />
            </div>
          </div>
        </div>
      </AbsoluteFill>

      <div
        style={{
          position: 'absolute',
          left: 96,
          bottom: 74,
          fontFamily: FONT,
          display: 'flex',
          gap: 14,
        }}
      >
        {['说你想做什么', '尺寸原样保留', '不用建模软件'].map((t, i) => (
          <Chip key={t} text={t} delay={26 + i * 14} />
        ))}
      </div>
    </AbsoluteFill>
  );
};

const Chip: React.FC<{text: string; delay: number}> = ({text, delay}) => {
  const e = useEnter(delay, 18);
  return (
    <div
      style={{
        ...e,
        padding: '12px 20px',
        borderRadius: 999,
        background: 'rgba(14,20,31,0.82)',
        border: '1px solid rgba(120,140,170,0.28)',
        color: C.ink,
        fontSize: 22,
        backdropFilter: 'blur(8px)',
      }}
    >
      {text}
    </div>
  );
};

/* ─────────── 3. 出图 ─────────── */

export const ImageShot: React.FC = () => {
  const f = useCurrentFrame();
  const slide = interpolate(f, [0, 34], [120, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const zoom = interpolate(f, [0, 240], [1.04, 1.12]);

  return (
    <AbsoluteFill>
      <Backdrop hue={C.accent2} />
      <AbsoluteFill style={{flexDirection: 'row', alignItems: 'center'}}>
        <div
          style={{
            marginLeft: 130,
            width: 760,
            height: 900,
            borderRadius: 24,
            overflow: 'hidden',
            transform: `translateX(${slide}px) scale(${zoom})`,
            boxShadow: '0 50px 140px rgba(0,0,0,0.6), 0 0 0 1px rgba(120,140,170,0.2)',
            position: 'relative',
          }}
        >
          <img src={staticFile('ref.png')} style={{width: '100%', height: '100%', objectFit: 'cover'}} />
          <Sweep delay={30} color={C.accent2} />
        </div>

        <div style={{marginLeft: 90, fontFamily: FONT, display: 'flex', flexDirection: 'column', gap: 30}}>
          <div style={{...useEnter(10, 24), fontSize: 64, fontWeight: 800, color: C.ink}}>
            先出图，再建模
          </div>
          <div style={{...useEnter(20, 24), fontSize: 26, color: C.dim, lineHeight: 1.7, maxWidth: 720}}>
            参考图决定形体和细节。
            <br />
            本地扩散模型出 1024²，再交给 3D 重建。
          </div>
          <div style={{display: 'flex', gap: 70, marginTop: 18}}>
            <Stat k="出图耗时" delay={34} v={<><NumberRoll to={72} delay={34} dur={40} /> s</>} />
            <Stat k="分辨率" delay={44} v="1024²" />
            <Stat k="模型" delay={54} v="flux_klein" />
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ─────────── 4. 建模转盘 ─────────── */

export const ModelShot: React.FC = () => {
  const f = useCurrentFrame();
  const zoom = interpolate(f, [0, 480], [1.0, 1.07]);
  const dim = interpolate(f, [330, 420], [0, 0.35], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  return (
    <AbsoluteFill style={{backgroundColor: '#05070c'}}>
      <AbsoluteFill style={{transform: `scale(${zoom})`}}>
        <OffthreadVideo src={staticFile('turntable.mp4')} muted />
      </AbsoluteFill>

      {/* 顶部标签 */}
      <div
        style={{
          position: 'absolute',
          top: 60,
          left: 80,
          fontFamily: FONT,
          ...useEnter(6, 16),
        }}
      >
        <div style={{fontSize: 24, color: C.ink, fontWeight: 600}}>
          蜷着睡的猫 · hunyuan3d 2mini · 512 octree
        </div>
        <div style={{fontSize: 19, color: C.dim, marginTop: 6}}>本地 V100 · 8 分 25 秒</div>
      </div>

      {/* 右下数据 */}
      <div
        style={{
          position: 'absolute',
          right: 96,
          bottom: 96,
          fontFamily: FONT,
          display: 'flex',
          gap: 76,
          padding: '26px 34px',
          borderRadius: 20,
          background: 'rgba(8,11,18,0.72)',
          border: '1px solid rgba(120,140,170,0.22)',
          backdropFilter: 'blur(10px)',
        }}
      >
        <Stat k="网格面数" delay={60} v={<><NumberRoll to={199582} delay={60} dur={46} /> </>} />
        <Stat k="是否水密" delay={78} v={<span style={{color: C.accent}}>√ 水密</span>} />
        <Stat k="目标高度" delay={96} v="80 mm" />
      </div>

      <Sweep delay={20} color={C.accent} />
      <AbsoluteFill style={{backgroundColor: `rgba(0,0,0,${dim})`}} />
    </AbsoluteFill>
  );
};

/* ─────────── 5. 打印 ─────────── */

export const PrintShot: React.FC = () => {
  const f = useCurrentFrame();
  const wipe = interpolate(f, [0, 30], [100, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const count = interpolate(f, [50, 84], [0, 80], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  const card = (src: string, w: number, h: number) => (
    <div
      style={{
        width: w,
        height: h,
        borderRadius: 20,
        overflow: 'hidden',
        background: '#f4f6f9',
        boxShadow: '0 50px 130px rgba(0,0,0,0.65), 0 0 0 1px rgba(120,140,170,0.25)',
        clipPath: `inset(${wipe}% 0 0 0 round 20px)`,
      }}
    >
      <img
        src={staticFile(src)}
        style={{width: '100%', height: '100%', objectFit: 'cover', filter: 'contrast(1.05)'}}
      />
    </div>
  );

  return (
    <AbsoluteFill>
      <Backdrop hue={C.hot} />
      <AbsoluteFill style={{flexDirection: 'row', alignItems: 'center'}}>
        <div style={{marginLeft: 110, display: 'flex', gap: 26, alignItems: 'flex-end'}}>
          {card('cat.png', 520, 640)}
          {card('coupon_c.png', 330, 380)}
        </div>

        <div
          style={{
            marginLeft: 70,
            fontFamily: FONT,
            display: 'flex',
            flexDirection: 'column',
            gap: 24,
            width: 780,
          }}
        >
          <div style={{...useEnter(12, 24), fontSize: 62, fontWeight: 800, color: C.ink}}>
            能打，才叫做完
          </div>
          <div style={{...useEnter(22, 24), fontSize: 25, color: C.dim, lineHeight: 1.7}}>
            自动转 Z-up、按毫米缩放、补洞修成水密实体，导出 STL。
一只蜷着睡的猫，80 mm 高，单一连通体。
          </div>

          <div style={{display: 'flex', alignItems: 'baseline', gap: 16, marginTop: 6}}>
            <span
              style={{
                fontSize: 82,
                fontWeight: 800,
                color: C.accent,
                fontVariantNumeric: 'tabular-nums',
              }}
            >
              {Math.round(count)}
            </span>
            <span style={{fontSize: 30, color: C.accent, fontWeight: 700}}>mm</span>
            <span style={{fontSize: 22, color: C.dim, marginLeft: 6}}>默认目标高度</span>
          </div>

          <div style={{display: 'flex', gap: 12, marginTop: 8}}>
            {['SLA 光固化', 'SLS 粉末', 'FDM 熔融'].map((t, i) => (
              <Chip key={t} text={t} delay={44 + i * 10} />
            ))}
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ─────────── 6. 收尾 ─────────── */

export const CTA: React.FC = () => {
  const f = useCurrentFrame();
  const glow = 0.5 + 0.5 * Math.sin(f / 18);

  return (
    <AbsoluteFill>
      <Backdrop hue={C.accent} />
      <AbsoluteFill
        style={{
          fontFamily: FONT,
          justifyContent: 'center',
          alignItems: 'center',
          gap: 30,
        }}
      >
        <div
          style={{
            ...useEnter(0, 14),
            width: 96,
            height: 96,
            borderRadius: 26,
            background: `linear-gradient(135deg, ${C.accent}, ${C.hot})`,
            boxShadow: `0 0 ${60 + glow * 30}px ${C.accent}66`,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 44,
            fontWeight: 800,
            color: '#07110f',
          }}
        >
          铸
        </div>
        <div style={{fontSize: 76, fontWeight: 800, color: C.ink, ...useEnter(10, 18)}}>
          装上就能用
        </div>
        <div style={{fontSize: 26, color: C.dim, ...useEnter(20, 18)}}>
          Windows 双击启动 · 自带环境检测 · 全程本机
        </div>
        <div
          style={{
            ...useEnter(32, 18),
            marginTop: 18,
            padding: '18px 46px',
            borderRadius: 16,
            border: '1px solid rgba(95,227,208,0.5)',
            background: 'rgba(10,18,26,0.7)',
            fontSize: 44,
            fontWeight: 700,
            color: C.accent,
            letterSpacing: 1,
          }}
        >
          zykt.space/foundry
        </div>
        <div style={{fontSize: 22, color: 'rgba(147,161,181,0.8)', ...useEnter(44, 14)}}>
          github.com/TIANKONGskyy/forgeform · AGPL-3.0
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

export const Film: React.FC = () => {
  return (
    <AbsoluteFill style={{backgroundColor: C.bg}}>
      <Sequence durationInFrames={180}>
        <Hook />
      </Sequence>
      <Sequence from={180} durationInFrames={300}>
        <Screen />
      </Sequence>
      <Sequence from={480} durationInFrames={240}>
        <ImageShot />
      </Sequence>
      <Sequence from={720} durationInFrames={480}>
        <ModelShot />
      </Sequence>
      <Sequence from={1200} durationInFrames={300}>
        <PrintShot />
      </Sequence>
      <Sequence from={1500} durationInFrames={330}>
        <CTA />
      </Sequence>
    </AbsoluteFill>
  );
};
