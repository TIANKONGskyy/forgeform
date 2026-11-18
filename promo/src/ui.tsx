import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import React from 'react';

/**
 * 全片的视觉语言（参考主流 AI 产品发布片的做法）：
 *   · 深底 + 极细网格 + 缓慢移动的光晕，不是纯色背景
 *   · 强调色只用在"数字"和"动作"上，标题永远白
 *   · 所有元素都用 spring 入场，不用线性淡入（线性看着像 PPT）
 *   · 文字逐词/逐字入场，带轻微上浮和模糊
 */

export const C = {
  bg: '#080b12',
  panel: '#0e141f',
  line: 'rgba(120,140,170,0.14)',
  ink: '#eef3fb',
  dim: '#93a1b5',
  accent: '#5fe3d0',
  accent2: '#ffc978',
  hot: '#7aa2ff',
};

export const FONT = '"Microsoft YaHei", "PingFang SC", system-ui, sans-serif';

/** 极细网格 + 光晕背景 */
export const Backdrop: React.FC<{hue?: string; grid?: boolean}> = ({
  hue = C.accent,
  grid = true,
}) => {
  const f = useCurrentFrame();
  const drift = Math.sin(f / 160) * 40;
  return (
    <AbsoluteFill style={{backgroundColor: C.bg}}>
      {grid ? (
        <AbsoluteFill
          style={{
            backgroundImage: `linear-gradient(${C.line} 1px, transparent 1px), linear-gradient(90deg, ${C.line} 1px, transparent 1px)`,
            backgroundSize: '72px 72px',
            maskImage: 'radial-gradient(circle at 50% 45%, black 20%, transparent 78%)',
            WebkitMaskImage: 'radial-gradient(circle at 50% 45%, black 20%, transparent 78%)',
            opacity: 0.55,
            transform: `translateY(${drift}px)`,
          }}
        />
      ) : null}
      <AbsoluteFill
        style={{
          background: `radial-gradient(900px 520px at ${50 + drift / 4}% 40%, ${hue}22, transparent 70%)`,
        }}
      />
      <AbsoluteFill
        style={{
          background: 'radial-gradient(1200px 700px at 50% 120%, #0b1220, transparent 70%)',
        }}
      />
    </AbsoluteFill>
  );
};

/** 入场：spring + 上浮 + 轻微模糊 */
export const useEnter = (delay = 0, from = 26) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const s = spring({frame: f - delay, fps, config: {damping: 200, mass: 0.6}});
  const opacity = interpolate(s, [0, 1], [0, 1]);
  const y = interpolate(s, [0, 1], [from, 0]);
  const blur = interpolate(s, [0, 1], [6, 0]);
  return {opacity, transform: `translateY(${y}px)`, filter: `blur(${blur}px)`};
};

/** 逐词入场 */
export const Words: React.FC<{
  text: string;
  delay?: number;
  step?: number;
  style?: React.CSSProperties;
}> = ({text, delay = 0, step = 3, style}) => {
  const words = text.split(/(\s+)/);
  return (
    <div style={{display: 'flex', flexWrap: 'wrap', justifyContent: 'center', ...style}}>
      {words.map((w, i) =>
        w.trim() === '' ? (
          <span key={i} style={{width: '0.4em'}} />
        ) : (
          <Word key={i} text={w} delay={delay + i * step} />
        )
      )}
    </div>
  );
};

const Word: React.FC<{text: string; delay: number}> = ({text, delay}) => {
  const e = useEnter(delay, 40);
  return <span style={{...e, display: 'inline-block'}}>{text}</span>;
};

/** 数字滚动 */
export const NumberRoll: React.FC<{
  to: number;
  delay?: number;
  dur?: number;
  suffix?: string;
  decimals?: number;
  style?: React.CSSProperties;
}> = ({to, delay = 0, dur = 30, suffix = '', decimals = 0, style}) => {
  const f = useCurrentFrame();
  const v = interpolate(f - delay, [0, dur], [0, to], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return (
    <span style={{fontVariantNumeric: 'tabular-nums', ...style}}>
      {v.toFixed(decimals)}
      {suffix}
    </span>
  );
};

/** 细线扫过的光带（用在图片/模型上） */
export const Sweep: React.FC<{delay?: number; color?: string}> = ({
  delay = 0,
  color = C.accent,
}) => {
  const f = useCurrentFrame();
  const p = interpolate(f - delay, [0, 40], [-30, 130], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return (
    <AbsoluteFill
      style={{
        background: `linear-gradient(105deg, transparent ${p - 18}%, ${color}30 ${p}%, transparent ${p + 18}%)`,
        mixBlendMode: 'screen',
      }}
    />
  );
};

/** 数据标注小卡 */
export const Stat: React.FC<{
  k: string;
  v: React.ReactNode;
  delay?: number;
  align?: 'left' | 'right';
}> = ({k, v, delay = 0, align = 'left'}) => {
  const e = useEnter(delay, 22);
  return (
    <div
      style={{
        ...e,
        display: 'flex',
        flexDirection: 'column',
        alignItems: align === 'right' ? 'flex-end' : 'flex-start',
        gap: 6,
      }}
    >
      <div style={{fontSize: 34, fontWeight: 700, color: C.ink, lineHeight: 1.1}}>{v}</div>
      <div style={{fontSize: 19, color: C.dim, letterSpacing: 0.5}}>{k}</div>
    </div>
  );
};
