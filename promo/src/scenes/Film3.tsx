import React from 'react';
import {
  AbsoluteFill,
  Easing,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import {TransitionSeries, springTiming, linearTiming} from '@remotion/transitions';
import {blurSlide} from '@remotion/transitions/blur-slide';
import {zoomBlur} from '@remotion/transitions/zoom-blur';
import {pushCut} from '@remotion/transitions/push-cut';
import {slide} from '@remotion/transitions/slide';

/**
 * 铸想宣传片 v3 —— 参考主流 AI 产品发布片的节奏与手法：
 *
 *   1. 快：36 秒、十来次剪切，平均每个镜头 2–3 秒（上一版平均 6–10 秒，太闷）
 *   2. 转场用真的转场（blurSlide / zoomBlur / pushCut / slide），不是淡入淡出
 *   3. 文字做动态排版：遮罩揭示、字符 stagger、数字滚动、跟随镜头的横向模糊
 *   4. 模型不是一个，是四个不同品类快速过一遍（鲸鱼 / 兔子 / 几何狐狸 / 香炉）
 *   5. **不要旁白**：中文 TTS 听起来像播报（用户反馈"配音太奇怪"），
 *      改成音乐 + 音效 + 屏幕文字，这是设计类产品片的通行做法
 */

export const C = {
  bg: '#07090f',
  ink: '#f2f6fc',
  dim: '#8b99ad',
  accent: '#5fe3d0',
  accent2: '#ffcb7a',
  hot: '#7aa2ff',
  pink: '#ff8ea8',
};
export const FONT = '"Microsoft YaHei", "PingFang SC", system-ui, sans-serif';

/* ── 基础动效 ───────────────────────────────────────────── */

const useSpring = (delay = 0, damping = 200, mass = 0.55) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  return spring({frame: f - delay, fps, config: {damping, mass}});
};

const Enter: React.FC<{
  delay?: number;
  from?: number;
  children: React.ReactNode;
  style?: React.CSSProperties;
}> = ({delay = 0, from = 30, children, style}) => {
  const s = useSpring(delay);
  return (
    <div
      style={{
        opacity: s,
        transform: `translateY(${(1 - s) * from}px)`,
        filter: `blur(${(1 - s) * 8}px)`,
        ...style,
      }}
    >
      {children}
    </div>
  );
};

/** 遮罩揭示：文字从下往上"擦"出来 */
const MaskReveal: React.FC<{
  delay?: number;
  dur?: number;
  children: React.ReactNode;
  style?: React.CSSProperties;
}> = ({delay = 0, dur = 22, children, style}) => {
  const f = useCurrentFrame();
  const p = interpolate(f - delay, [0, dur], [0, 100], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });
  return (
    <div style={{overflow: 'hidden', ...style}}>
      <div style={{transform: `translateY(${100 - p}%)`}}>{children}</div>
    </div>
  );
};

/** 逐字弹出（中文按字切） */
const Chars: React.FC<{
  text: string;
  delay?: number;
  step?: number;
  style?: React.CSSProperties;
}> = ({text, delay = 0, step = 2, style}) => (
  <span style={{display: 'inline-flex', ...style}}>
    {Array.from(text).map((ch, i) => (
      <Char key={i} ch={ch} delay={delay + i * step} />
    ))}
  </span>
);

const Char: React.FC<{ch: string; delay: number}> = ({ch, delay}) => {
  const s = useSpring(delay, 190, 0.5);
  return (
    <span
      style={{
        display: 'inline-block',
        transform: `translateY(${(1 - s) * 46}px) scale(${0.86 + 0.14 * s})`,
        opacity: Math.min(1, s * 1.6),
        filter: `blur(${(1 - s) * 10}px)`,
      }}
    >
      {ch}
    </span>
  );
};

/** 横向快门线（切换瞬间的"唰"） */
const WhipLines: React.FC<{at: number; n?: number}> = ({at, n = 14}) => {
  const f = useCurrentFrame();
  const p = interpolate(f - at, [0, 12], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  if (p <= 0 || p >= 1) return null;
  return (
    <AbsoluteFill style={{opacity: (1 - p) * 0.55, mixBlendMode: 'screen'}}>
      {new Array(n).fill(0).map((_, i) => (
        <div
          key={i}
          style={{
            position: 'absolute',
            top: `${(i / n) * 100 + 2}%`,
            left: 0,
            width: '100%',
            height: i % 3 === 0 ? 3 : 1,
            background: i % 4 === 0 ? C.accent : 'rgba(200,220,255,0.5)',
            transform: `translateX(${(i % 2 ? 1 : -1) * p * 120}px)`,
          }}
        />
      ))}
    </AbsoluteFill>
  );
};

/* ── 背景 ──────────────────────────────────────────────── */

const Bg: React.FC<{hue?: string; grid?: boolean}> = ({hue = C.accent, grid = true}) => {
  const f = useCurrentFrame();
  const g = Math.sin(f / 90) * 26;
  return (
    <AbsoluteFill style={{backgroundColor: C.bg}}>
      {grid && (
        <AbsoluteFill
          style={{
            backgroundImage: `linear-gradient(rgba(120,140,170,0.09) 1px, transparent 1px),
              linear-gradient(90deg, rgba(120,140,170,0.09) 1px, transparent 1px)`,
            backgroundSize: '64px 64px',
            backgroundPosition: `${g}px ${g / 2}px`,
            maskImage: 'radial-gradient(circle at 50% 45%, #000 25%, transparent 74%)',
            WebkitMaskImage: 'radial-gradient(circle at 50% 45%, #000 25%, transparent 74%)',
          }}
        />
      )}
      <AbsoluteFill style={{background: `radial-gradient(820px 480px at 50% 42%, ${hue}26, transparent 72%)`}} />
      <AbsoluteFill style={{background: 'radial-gradient(1100px 620px at 50% 115%, #0a1120, transparent 70%)'}} />
    </AbsoluteFill>
  );
};

/* ── 1. 片头（4.5s）────────────────────────────────────── */

const Hook: React.FC = () => {
  const f = useCurrentFrame();
  const out = interpolate(f, [112, 135], [1, 1.06], {extrapolateRight: 'clamp'});
  return (
    <AbsoluteFill style={{transform: `scale(${out})`}}>
      <Bg />
      <AbsoluteFill style={{fontFamily: FONT, justifyContent: 'center', alignItems: 'center'}}>
        <Enter delay={0} from={10}>
          <div style={{fontSize: 20, letterSpacing: 14, color: C.accent, fontWeight: 600}}>
            铸想 FORGEFORM
          </div>
        </Enter>
        <MaskReveal delay={6} dur={20} style={{marginTop: 30}}>
          <div style={{fontSize: 112, fontWeight: 800, color: C.ink, letterSpacing: 2, lineHeight: 1.05}}>
            一句话
          </div>
        </MaskReveal>
        <div style={{display: 'flex', alignItems: 'center', gap: 26, marginTop: 14}}>
          <Enter delay={22} from={0} style={{fontSize: 40, color: C.accent}}>
            →
          </Enter>
          <MaskReveal delay={26} dur={20}>
            <div style={{fontSize: 74, fontWeight: 700, color: C.ink, letterSpacing: 1}}>
              可打印的实体
            </div>
          </MaskReveal>
        </div>
        <Enter delay={48} from={16}>
          <div style={{marginTop: 40, fontSize: 21, color: C.dim, letterSpacing: 4}}>
            本地跑 · 不出机器 · AGPL-3.0
          </div>
        </Enter>
      </AbsoluteFill>
      <WhipLines at={120} />
    </AbsoluteFill>
  );
};

/* ── 2. 真实界面（5.5s）───────────────────────────────── */

const Screen: React.FC = () => {
  const f = useCurrentFrame();
  const zoom = interpolate(f, [10, 46], [1.0, 1.5], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.bezier(0.4, 0, 0.2, 1),
  });
  const back = interpolate(f, [104, 150], [1.5, 1.06], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const z = f < 104 ? zoom : back;
  const tx = (790 - 800 / 1920 * 1580) / z;
  const ty = (445 - 660 / 1080 * 890) / z;

  return (
    <AbsoluteFill>
      <Bg hue={C.hot} />
      <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center'}}>
        <div
          style={{
            width: 1580,
            borderRadius: 18,
            overflow: 'hidden',
            background: '#0b1018',
            boxShadow: '0 70px 170px rgba(0,0,0,0.75), 0 0 0 1px rgba(120,140,170,0.22)',
          }}
        >
          <div
            style={{
              height: 34,
              display: 'flex',
              alignItems: 'center',
              gap: 7,
              paddingLeft: 16,
              borderBottom: '1px solid rgba(120,140,170,0.14)',
            }}
          >
            {['#ff5f57', '#febc2e', '#28c840'].map((c) => (
              <div key={c} style={{width: 10, height: 10, borderRadius: 5, background: c, opacity: 0.7}} />
            ))}
            <span style={{marginLeft: 14, fontFamily: FONT, fontSize: 13, color: 'rgba(140,154,173,0.8)'}}>
              铸想 · 创作空间
            </span>
          </div>
          <div style={{width: 1580, height: 890, overflow: 'hidden'}}>
            <div style={{transform: `scale(${z}) translate(${tx}px, ${ty}px)`}}>
              <OffthreadVideo
                src={staticFile('session.mp4')}
                startFrom={165}
                muted
                style={{width: 1580, display: 'block', filter: 'contrast(1.07) brightness(1.05)'}}
              />
            </div>
          </div>
        </div>
      </AbsoluteFill>

      {/* 左下标语：一句话说清"这是在干什么" */}
      <div style={{position: 'absolute', left: 90, top: 96, fontFamily: FONT}}>
        <MaskReveal delay={14} dur={18}>
          <div style={{fontSize: 44, fontWeight: 800, color: C.ink}}>说一句，就开始了</div>
        </MaskReveal>
        <Enter delay={22} from={14}>
          <div style={{marginTop: 10, fontSize: 20, color: C.dim, letterSpacing: 1}}>
            尺寸 / 结构 / 风格，它自己拆成步骤
          </div>
        </Enter>
      </div>
      <WhipLines at={160} />
    </AbsoluteFill>
  );
};

/* ── 3. 出图（3.5s）───────────────────────────────────── */

const ImageShot: React.FC = () => {
  const f = useCurrentFrame();
  const cards = ['whale', 'rabbit', 'foxgeo', 'censer'];
  return (
    <AbsoluteFill>
      <Bg hue={C.accent2} />
      <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center'}}>
        <div style={{display: 'flex', gap: 22}}>
          {cards.map((k, i) => (
            <ImageCard key={k} src={staticFile(`casts/img_${k}.png`)} delay={i * 6} />
          ))}
        </div>
      </AbsoluteFill>
      <div
        style={{
          position: 'absolute',
          bottom: 92,
          left: 0,
          right: 0,
          textAlign: 'center',
          fontFamily: FONT,
        }}
      >
        <MaskReveal delay={10} dur={18}>
          <div style={{fontSize: 46, fontWeight: 800, color: C.ink}}>一次出四张参考图</div>
        </MaskReveal>
        <Enter delay={20} from={12}>
          <div style={{marginTop: 8, fontSize: 19, color: C.dim, letterSpacing: 2}}>
            本地扩散模型 · 1024² · 每张 60–80 秒
          </div>
        </Enter>
      </div>
    </AbsoluteFill>
  );
};

const ImageCard: React.FC<{src: string; delay: number}> = ({src, delay}) => {
  const s = useSpring(delay, 170, 0.6);
  const f = useCurrentFrame();
  const float = Math.sin((f + delay * 3) / 22) * 6;
  return (
    <div
      style={{
        width: 300,
        height: 380,
        borderRadius: 18,
        overflow: 'hidden',
        transform: `translateY(${(1 - s) * 60 + float}px) rotate(${(1 - s) * 4}deg) scale(${0.9 + 0.1 * s})`,
        opacity: s,
        boxShadow: '0 40px 90px rgba(0,0,0,0.6), 0 0 0 1px rgba(120,140,170,0.22)',
        background: '#f6f8fa',
      }}
    >
      <Img src={src} style={{width: '100%', height: '100%', objectFit: 'cover'}} />
    </div>
  );
};

/* ── 4. 四个模型快速过（7s）───────────────────────────── */

const MODELS = [
  {k: 'whale', name: '白瓷鲸鱼', tag: '海洋生物', faces: 200000, scale: 1.52},
  {k: 'rabbit', name: '白瓷兔子', tag: '动物摆件', faces: 200000, scale: 1.06},
  {k: 'foxgeo', name: '几何狐狸', tag: '低多边形', faces: 200000, scale: 1.26},
  {k: 'censer', name: '陶瓷香炉', tag: '器物', faces: 200000, scale: 1.22},
];

const ModelReel: React.FC = () => {
  const {fps} = useVideoConfig();
  const per = 52; // 每件 52 帧 ≈ 1.7 秒
  return (
    <AbsoluteFill style={{backgroundColor: '#05070c'}}>
      {MODELS.map((m, i) => (
        <Sequence key={m.k} from={i * per} durationInFrames={per + 12}>
          <ModelCard {...m} idx={i} />
        </Sequence>
      ))}
      <div style={{position: 'absolute', top: 54, left: 78, fontFamily: FONT}}>
        <Enter delay={2} from={12}>
          <div style={{fontSize: 19, color: C.accent, letterSpacing: 6}}>四个品类 · 一次跑完</div>
        </Enter>
      </div>
    </AbsoluteFill>
  );
};

const ModelCard: React.FC<{
  k: string;
  name: string;
  tag: string;
  faces: number;
  scale: number;
  idx: number;
}> = ({k, name, tag, faces, scale, idx}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const s = spring({frame: f, fps, config: {damping: 160, mass: 0.5}});
  const zoom = interpolate(f, [0, 60], [1.14, 1.0], {extrapolateRight: 'clamp'});
  const out = interpolate(f, [48, 64], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  return (
    <AbsoluteFill style={{opacity: 1 - out}}>
      <AbsoluteFill>
        <OffthreadVideo
          src={staticFile(`casts/tt_${k}.mp4`)}
          muted
          style={{
            width: '100%',
            height: '100%',
            objectFit: 'cover',
            transform: `scale(${zoom * scale})`,
          }}
        />
      </AbsoluteFill>
      <AbsoluteFill
        style={{background: 'linear-gradient(0deg, rgba(5,7,12,0.92) 0%, transparent 42%)'}}
      />
      <div
        style={{
          position: 'absolute',
          left: 92,
          bottom: 74,
          fontFamily: FONT,
          transform: `translateY(${(1 - s) * 30}px)`,
          opacity: s,
        }}
      >
        <div style={{fontSize: 17, color: C.accent, letterSpacing: 5, marginBottom: 8}}>
          0{idx + 1} · {tag}
        </div>
        <div style={{fontSize: 62, fontWeight: 800, color: C.ink, letterSpacing: 1}}>{name}</div>
        <div style={{fontSize: 19, color: C.dim, marginTop: 10}}>
          {faces.toLocaleString()} 面 · 水密 · 80 mm · 导出 STL
        </div>
      </div>
      <div
        style={{
          position: 'absolute',
          right: 92,
          bottom: 78,
          fontFamily: FONT,
          textAlign: 'right',
          opacity: s,
        }}
      >
        <div style={{fontSize: 15, color: C.dim, letterSpacing: 3}}>hunyuan3d 2mini</div>
        <div style={{fontSize: 15, color: C.dim, letterSpacing: 3, marginTop: 4}}>512 octree · 本地</div>
      </div>
    </AbsoluteFill>
  );
};

/* ── 5. 打印（4s）─────────────────────────────────────── */

const PrintShot: React.FC = () => {
  const f = useCurrentFrame();
  const count = interpolate(f, [14, 46], [0, 80], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  return (
    <AbsoluteFill>
      <Bg hue={C.pink} />
      <AbsoluteFill style={{flexDirection: 'row', alignItems: 'center'}}>
        <div style={{marginLeft: 110, display: 'flex', gap: 26, alignItems: 'flex-end'}}>
          <Enter delay={2} from={50}>
            <div
              style={{
                width: 460,
                height: 600,
                borderRadius: 18,
                overflow: 'hidden',
                background: '#f6f8fa',
                boxShadow: '0 50px 120px rgba(0,0,0,0.65), 0 0 0 1px rgba(120,140,170,0.2)',
              }}
            >
              <Img src={staticFile('casts/prev_whale.png')} style={{width: '100%', height: '100%', objectFit: 'cover'}} />
            </div>
          </Enter>
          <Enter delay={8} from={50}>
            <div
              style={{
                width: 320,
                height: 340,
                borderRadius: 18,
                overflow: 'hidden',
                background: '#f6f8fa',
                boxShadow: '0 50px 120px rgba(0,0,0,0.6), 0 0 0 1px rgba(120,140,170,0.2)',
              }}
            >
              <Img src={staticFile('coupon.png')} style={{width: '100%', height: '100%', objectFit: 'cover'}} />
            </div>
          </Enter>
        </div>

        <div style={{marginLeft: 70, fontFamily: FONT, width: 760}}>
          <MaskReveal delay={8} dur={18}>
            <div style={{fontSize: 60, fontWeight: 800, color: C.ink}}>能打，才算完</div>
          </MaskReveal>
          <Enter delay={16} from={14}>
            <div style={{marginTop: 14, fontSize: 23, color: C.dim, lineHeight: 1.7}}>
              转 Z-up、按毫米缩放、补洞修成水密实体，导出 STL。
            </div>
          </Enter>
          <div style={{display: 'flex', alignItems: 'baseline', gap: 14, marginTop: 22}}>
            <span style={{fontSize: 92, fontWeight: 800, color: C.accent, fontVariantNumeric: 'tabular-nums'}}>
              {Math.round(count)}
            </span>
            <span style={{fontSize: 30, color: C.accent, fontWeight: 700}}>mm</span>
            <span style={{fontSize: 20, color: C.dim}}>默认目标高度</span>
          </div>
          <div style={{display: 'flex', gap: 10, marginTop: 18}}>
            {['SLA 光固化', 'SLS 粉末', 'FDM 熔融'].map((t, i) => (
              <Enter key={t} delay={26 + i * 6} from={14}>
                <div
                  style={{
                    padding: '10px 18px',
                    borderRadius: 999,
                    border: '1px solid rgba(120,140,170,0.3)',
                    background: 'rgba(14,20,31,0.7)',
                    color: C.ink,
                    fontSize: 19,
                  }}
                >
                  {t}
                </div>
              </Enter>
            ))}
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ── 6. 收尾（4s）─────────────────────────────────────── */

const CTA: React.FC = () => {
  const f = useCurrentFrame();
  const glow = 0.5 + 0.5 * Math.sin(f / 14);
  return (
    <AbsoluteFill>
      <Bg />
      <AbsoluteFill style={{fontFamily: FONT, justifyContent: 'center', alignItems: 'center'}}>
        <Enter delay={0} from={16}>
          <div
            style={{
              width: 84,
              height: 84,
              borderRadius: 22,
              background: `linear-gradient(135deg, ${C.accent}, ${C.hot})`,
              boxShadow: `0 0 ${50 + glow * 26}px ${C.accent}66`,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 38,
              fontWeight: 800,
              color: '#07110f',
            }}
          >
            铸
          </div>
        </Enter>
        <MaskReveal delay={6} dur={18} style={{marginTop: 26}}>
          <div style={{fontSize: 70, fontWeight: 800, color: C.ink}}>装上就能用</div>
        </MaskReveal>
        <Enter delay={16} from={12}>
          <div style={{marginTop: 14, fontSize: 23, color: C.dim}}>
            Windows 双击启动 · 自带环境检测 · 全程本机
          </div>
        </Enter>
        <Enter delay={26} from={12}>
          <div
            style={{
              marginTop: 30,
              padding: '16px 44px',
              borderRadius: 14,
              border: `1px solid ${C.accent}88`,
              background: 'rgba(10,18,26,0.72)',
              fontSize: 40,
              fontWeight: 700,
              color: C.accent,
              letterSpacing: 1,
            }}
          >
            zykt.space/foundry
          </div>
        </Enter>
        <Enter delay={36} from={10}>
          <div style={{marginTop: 18, fontSize: 19, color: 'rgba(139,153,173,0.85)'}}>
            github.com/TIANKONGskyy/forgeform · AGPL-3.0
          </div>
        </Enter>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ── 时间轴 ───────────────────────────────────────────── */

export const Film: React.FC = () => {
  return (
    <AbsoluteFill style={{backgroundColor: C.bg}}>
      <TransitionSeries>
        <TransitionSeries.Sequence durationInFrames={135}>
          <Hook />
        </TransitionSeries.Sequence>

        <TransitionSeries.Transition
          presentation={blurSlide({direction: 'from-right'})}
          timing={springTiming({config: {damping: 200}, durationInFrames: 14})}
        />
        <TransitionSeries.Sequence durationInFrames={168}>
          <Screen />
        </TransitionSeries.Sequence>

        <TransitionSeries.Transition
          presentation={pushCut()}
          timing={linearTiming({durationInFrames: 10})}
        />
        <TransitionSeries.Sequence durationInFrames={105}>
          <ImageShot />
        </TransitionSeries.Sequence>

        <TransitionSeries.Transition
          presentation={zoomBlur({direction: 'in'})}
          timing={linearTiming({durationInFrames: 14})}
        />
        <TransitionSeries.Sequence durationInFrames={218}>
          <ModelReel />
        </TransitionSeries.Sequence>

        <TransitionSeries.Transition
          presentation={slide({direction: 'from-bottom'})}
          timing={springTiming({config: {damping: 200}, durationInFrames: 12})}
        />
        <TransitionSeries.Sequence durationInFrames={120}>
          <PrintShot />
        </TransitionSeries.Sequence>

        <TransitionSeries.Transition
          presentation={blurSlide({direction: 'from-left'})}
          timing={springTiming({config: {damping: 200}, durationInFrames: 14})}
        />
        <TransitionSeries.Sequence durationInFrames={120}>
          <CTA />
        </TransitionSeries.Sequence>
      </TransitionSeries>
    </AbsoluteFill>
  );
};

/** 总帧数：各段之和 − 转场重叠（14+10+14+12+14 = 64） */
export const FILM_FRAMES = 135 + 168 + 105 + 218 + 120 + 120 - 64;
