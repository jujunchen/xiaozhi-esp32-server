import os
import time
import edge_tts
import asyncio
import traceback
import queue
from io import BytesIO
from pydub import AudioSegment
from config.logger import setup_logging
from core.utils.tts import MarkdownCleaner
from core.utils import opus_encoder_utils, textUtils
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.voice = config.get("voice", "zh-CN-XiaoxiaoNeural")
        self.rate = config.get("rate", "+10%")
        self.volume = config.get("volume", "+10%")
        self.interface_type = InterfaceType.SINGLE_STREAM

        # 初始化Opus编码器 16kHz 单声道 60ms帧
        self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=16000, channels=1, frame_size_ms=60
        )
        # PCM缓冲区
        self.pcm_buffer = bytearray()
        self.audio_file_type = "mp3"

    def tts_text_priority_thread(self):
        """流式文本处理线程"""
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                if message.sentence_type == SentenceType.FIRST:
                    # 初始化参数
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    self.before_stop_play_files.clear()
                elif ContentType.TEXT == message.content_type:
                    self.tts_text_buff.append(message.content_detail)
                    segment_text = self._get_segment_text()
                    if segment_text:
                        self.to_tts_single_stream(segment_text)

                elif ContentType.FILE == message.content_type:
                    logger.bind(tag=TAG).info(
                        f"添加音频文件到待播放列表: {message.content_file}"
                    )
                    if message.content_file and os.path.exists(message.content_file):
                        # 先处理文件音频数据
                        self._process_audio_file_stream(message.content_file, callback=lambda audio_data: self.handle_audio_file(audio_data, message.content_detail))
                if message.sentence_type == SentenceType.LAST:
                    # 处理剩余的文本
                    self._process_remaining_text_stream(True)

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"处理TTS文本失败: {str(e)}, 类型: {type(e).__name__}, 堆栈: {traceback.format_exc()}"
                )

    def _process_remaining_text_stream(self, is_last=False):
        """处理剩余的文本并生成语音
        Returns:
            bool: 是否成功处理了文本
        """
        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars :]
        if remaining_text:
            segment_text = textUtils.get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                self.to_tts_single_stream(segment_text, is_last)
                self.processed_chars += len(full_text)
            else:
                self._process_before_stop_play_files()
        else:
            self._process_before_stop_play_files()

    def to_tts_single_stream(self, text, is_last=False):
        try:
            max_repeat_time = 5
            text = MarkdownCleaner.clean_markdown(text)
            while max_repeat_time > 0:
                try:
                    asyncio.run(self.text_to_speak(text, None, is_last))
                    break
                except Exception as e:
                    logger.bind(tag=TAG).warning(
                        f"语音生成失败{5 - max_repeat_time + 1}次: {text}，错误: {e}"
                    )
                    max_repeat_time -= 1

            if max_repeat_time > 0:
                logger.bind(tag=TAG).info(
                f"语音生成成功: {text}，重试{5 - max_repeat_time}次"
            )
            else:
                logger.bind(tag=TAG).error(
                    f"语音生成失败: {text}，请检查网络或服务是否正常"
                )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to generate TTS file: {e}")
        finally:
            return None

    async def text_to_speak(self, text, output_file=None, is_last=False):
        """流式处理TTS音频，边下载边处理"""
        # 如果output_file不为None，保持向后兼容，使用旧行为
        if output_file is not None:
            try:
                communicate = edge_tts.Communicate(text, voice=self.voice, rate=self.rate, volume=self.volume)
                with open(output_file, "ab") as f:
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            f.write(chunk["data"])
                return None
            except Exception as e:
                logger.bind(tag=TAG).error(f"EdgeTTS file output error: {e}")
                return None

        # 流式处理新模式
        communicate = edge_tts.Communicate(text, voice=self.voice, rate=self.rate, volume=self.volume)
        frame_bytes = int(
            self.opus_encoder.sample_rate
            * self.opus_encoder.channels
            * self.opus_encoder.frame_size_ms
            / 1000
            * 2
        )  # 16-bit = 2 bytes

        try:
            mp3_buffer = bytearray()
            self.pcm_buffer.clear()
            self.tts_audio_queue.put((SentenceType.FIRST, [], text))

            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    mp3_buffer.extend(chunk["data"])

                    # 积累足够数据后解码处理（约16KB以上）
                    if len(mp3_buffer) >= 16384:
                        if len(mp3_buffer) > 0:
                            try:
                                # 解码MP3为PCM
                                audio = AudioSegment.from_file(BytesIO(mp3_buffer), format="mp3", parameters=["-nostdin"])
                                # 转换为16kHz 单声道 16-bit PCM
                                audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
                                raw_pcm = audio.raw_data
                                self.pcm_buffer.extend(raw_pcm)
                                mp3_buffer.clear()
                            except Exception:
                                # 解码失败可能是因为不完整的MP3，继续积累数据
                                pass

                    # 处理所有完整的PCM帧
                    while len(self.pcm_buffer) >= frame_bytes:
                        frame = bytes(self.pcm_buffer[:frame_bytes])
                        del self.pcm_buffer[:frame_bytes]

                        self.opus_encoder.encode_pcm_to_opus_stream(
                            frame, end_of_stream=False, callback=self.handle_opus
                        )

            # 处理剩余的MP3数据（流结束后）
            if len(mp3_buffer) > 0:
                try:
                    audio = AudioSegment.from_file(BytesIO(mp3_buffer), format="mp3", parameters=["-nostdin"])
                    audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
                    raw_pcm = audio.raw_data
                    self.pcm_buffer.extend(raw_pcm)
                except Exception as e:
                    logger.bind(tag=TAG).debug(f"Final MP3 decode warning: {e}")

            # 处理剩余的PCM数据
            if len(self.pcm_buffer) > 0:
                # 填充不足一帧的数据
                if len(self.pcm_buffer) < frame_bytes:
                    padding_needed = frame_bytes - len(self.pcm_buffer)
                    self.pcm_buffer.extend(b"\x00" * padding_needed)

                self.opus_encoder.encode_pcm_to_opus_stream(
                    bytes(self.pcm_buffer),
                    end_of_stream=True,
                    callback=self.handle_opus
                )
                self.pcm_buffer.clear()

            # 如果是最后一段，输出音频获取完毕
            if is_last:
                self._process_before_stop_play_files()

        except Exception as e:
            logger.bind(tag=TAG).error(f"EdgeTTS streaming error: {e}")
            logger.bind(tag=TAG).error(f"Stack trace: {traceback.format_exc()}")
            self.tts_audio_queue.put((SentenceType.LAST, [], None))

    def to_tts(self, text):
        """非流式TTS处理，用于测试及保存音频文件的场景
        Args:
            text: 要转换的文本
        Returns:
            list: 返回opus编码后的音频数据列表
        """
        start_time = time.time()
        text = MarkdownCleaner.clean_markdown(text)
        try:
            # 同步获取完整MP3
            communicate = edge_tts.Communicate(text, voice=self.voice, rate=self.rate, volume=self.volume)
            mp3_bytes = b""
            async def collect_chunks():
                nonlocal mp3_bytes
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        mp3_bytes += chunk["data"]
            asyncio.run(collect_chunks())

            # 解码为PCM
            audio = AudioSegment.from_file(BytesIO(mp3_bytes), format="mp3")
            audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
            pcm_data = bytearray(audio.raw_data)

            logger.info(f"EdgeTTS合成成功: {text}, 耗时: {time.time() - start_time:.3f}秒")

            # 使用opus编码器处理
            opus_datas = []
            frame_bytes = int(
                self.opus_encoder.sample_rate
                * self.opus_encoder.channels
                * self.opus_encoder.frame_size_ms
                / 1000
                * 2
            )

            # 分帧处理
            for i in range(0, len(pcm_data), frame_bytes):
                frame = bytes(pcm_data[i:i+frame_bytes])
                if len(frame) < frame_bytes:
                    frame += b"\x00" * (frame_bytes - len(frame))

                self.opus_encoder.encode_pcm_to_opus_stream(
                    frame,
                    end_of_stream=(i + frame_bytes >= len(pcm_data)),
                    callback=lambda opus: opus_datas.append(opus)
                )

            return opus_datas

        except Exception as e:
            logger.bind(tag=TAG).error(f"EdgeTTS non-streaming error: {e}")
            return []

    async def close(self):
        """资源清理"""
        await super().close()
        if hasattr(self, "opus_encoder"):
            self.opus_encoder.close()