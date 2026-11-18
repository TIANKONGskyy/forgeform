import React from 'react';
import {Audio, Composition, Sequence, staticFile} from 'remotion';
import {Film, FILM_FRAMES} from './scenes/Film4';
import {whip, whoosh, uiSwitch, ding} from '@remotion/sfx';

/**
 * 铸想宣传片 v3（26.7 秒，802 帧）
 *
 * 和上一版的差别：
 *   · 节奏加快：从 61 秒压到 27 秒，四个模型每个只给 1.7 秒
 *   · 转场换成真转场（blurSlide / pushCut / zoomBlur / slide），不是淡入淡出
 *   · **去掉旁白**：中文 TTS 听着像播报（用户反馈"配音太奇怪"）。
 *     改成音乐 + 音效 + 屏幕文字 —— 设计类产品片的通行做法。
 *   · 音效用 @remotion/sfx 的现成素材（whip / whoosh / switch / ding），
 *     比我自己合成的更干净。
 */
export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="ForgeForm"
      component={() => (
        <>
          <Audio src={staticFile('music.wav')} volume={0.5} />

          {/* 转场音效：位置对齐 Film3 里各个 TransitionSeries.Transition */}
          <Sequence from={118} durationInFrames={30}>
            <Audio src={whip} volume={0.55} />
          </Sequence>
          <Sequence from={268} durationInFrames={30}>
            <Audio src={whoosh} volume={0.5} />
          </Sequence>
          <Sequence from={360} durationInFrames={30}>
            <Audio src={whoosh} volume={0.5} />
          </Sequence>
          <Sequence from={560} durationInFrames={30}>
            <Audio src={whip} volume={0.5} />
          </Sequence>
          <Sequence from={672} durationInFrames={30}>
            <Audio src={whoosh} volume={0.5} />
          </Sequence>

          {/* 模型逐个切上来的"咔" */}
          {[0, 52, 104, 156].map((d) => (
            <Sequence key={d} from={374 + d} durationInFrames={16}>
              <Audio src={uiSwitch} volume={0.4} />
            </Sequence>
          ))}

          {/* 收尾定格 */}
          <Sequence from={760} durationInFrames={40}>
            <Audio src={ding} volume={0.4} />
          </Sequence>

          <Film />
        </>
      )}
      durationInFrames={FILM_FRAMES}
      fps={30}
      width={1920}
      height={1080}
    />
  );
};
