import React from 'react';
import {Composition, Audio, Sequence, staticFile} from 'remotion';
import {Film} from './scenes/Film';

/**
 * 时间轴（30fps，共 1830 帧 = 61 秒）：
 *   0    – 180   片头
 *   180  – 480   真实界面（录制）
 *   480  – 720   出图
 *   720  – 1200  建模转盘
 *   1200 – 1500  打印
 *   1500 – 1830  收尾
 *
 * 音频：音乐铺底 + 每个切点上一下音效（whoosh/pop/riser）。
 * 音乐和音效都是本地 numpy 合成的，没有版权问题。
 */
export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="ForgeForm"
      component={() => (
        <>
          <Audio src={staticFile('music.wav')} volume={0.34} />
          <Sequence from={176} durationInFrames={40}>
            <Audio src={staticFile('whoosh.wav')} volume={0.5} />
          </Sequence>
          <Sequence from={476} durationInFrames={40}>
            <Audio src={staticFile('whoosh.wav')} volume={0.5} />
          </Sequence>
          <Sequence from={712} durationInFrames={48}>
            <Audio src={staticFile('riser.wav')} volume={0.5} />
          </Sequence>
          <Sequence from={1196} durationInFrames={40}>
            <Audio src={staticFile('whoosh.wav')} volume={0.45} />
          </Sequence>
          <Sequence from={1496} durationInFrames={40}>
            <Audio src={staticFile('whoosh.wav')} volume={0.5} />
          </Sequence>
          <Sequence from={1520} durationInFrames={20}>
            <Audio src={staticFile('pop.wav')} volume={0.5} />
          </Sequence>
          {/* 旁白：按幕投放，音量比音乐高，保证听懂 */}
          <Sequence from={16} durationInFrames={170}>
            <Audio src={staticFile('vo1.mp3')} volume={0.95} />
          </Sequence>
          <Sequence from={196} durationInFrames={290}>
            <Audio src={staticFile('vo2.mp3')} volume={0.95} />
          </Sequence>
          <Sequence from={492} durationInFrames={230}>
            <Audio src={staticFile('vo3.mp3')} volume={0.95} />
          </Sequence>
          <Sequence from={736} durationInFrames={470}>
            <Audio src={staticFile('vo4.mp3')} volume={0.95} />
          </Sequence>
          <Sequence from={1210} durationInFrames={290}>
            <Audio src={staticFile('vo5.mp3')} volume={0.95} />
          </Sequence>
          <Sequence from={1512} durationInFrames={300}>
            <Audio src={staticFile('vo6.mp3')} volume={0.95} />
          </Sequence>
          <Film />
        </>
      )}
      durationInFrames={1830}
      fps={30}
      width={1920}
      height={1080}
    />
  );
};
