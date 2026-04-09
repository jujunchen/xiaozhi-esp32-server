import os
import time
import numpy as np
import asyncio
import traceback
import queue
from config.logger import setup_logging
from core.utils.tts import MarkdownCleaner
from core.utils import opus_encoder_utils, textUtils
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType
from modelscope.hub.file_download import model_file_download

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.model_dir = config.get("model_dir", "models/sherpa-onnx-tts-matcha-icefall-zh-en")
        self.output_dir = config.get("output_dir", "tmp/")
        self.num_threads = config.get("num_threads", 2)
        self.provider = config.get("provider", "cpu")
        self.speed = config.get("speed", 1.0)
        self.interface_type = InterfaceType.SINGLE_STREAM

        # 默认模型文件 (Matcha-TTS + VITS 中文模型)
        # 参考: https://k2-fsa.github.io/sherpa/onnx/tts/all/Chinese-English/matcha-icefall-zh-en.html
        self.model_files = {
            "model": os.path.join(self.model_dir, "model-int8.onnx"),
            "vocab": os.path.join(self.model_dir, "vocab.txt"),
            "lexicon": os.path.join(self.model_dir, "lexicon.txt"),
            "tokens": os.path.join(self.model_dir, "tokens.txt"),
        }

        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.model_dir, exist_ok=True)

        # 下载并检查模型文件
        try:
            self._download_model_files()
            logger.bind(tag=TAG).info(f"初始化 SherpaOnnx TTS, 模型目录: {self.model_dir}")

            import sherpa_onnx
            self.model = sherpa_onnx.OfflineTts.from_matcha(
                model=self.model_files["model"],
                vocab=self.model_files["vocab"],
                lexicon=self.model_files["lexicon"],
                tokens=self.model_files["tokens"],
                num_threads=self.num_threads,
                provider=self.provider,
                debug=False,
            )

            # SherpaOnnx Matcha TTS 输出是 22050Hz，转换为 16kHz Opus
            self.output_sample_rate = 16000
            # 初始化Opus编码器 16kHz 单声道 60ms帧
            self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
                sample_rate=self.output_sample_rate, channels=1, frame_size_ms=60
            )
            # PCM缓冲区
            self.pcm_buffer = bytearray()
            # 首包计时
            self.first_opus_sent = False
            self.start_time = 0

            logger.bind(tag=TAG).info(f"SherpaOnnx TTS 初始化完成，输出采样率: {self.model.sample_rate}")

        except Exception as e:
            logger.bind(tag=TAG).error(f"SherpaOnnx TTS 初始化失败: {str(e)}")
            raise

    def _download_model_files(self):
        """下载模型文件如果不存在"""
        # 正确的模型在 ModelScope 上是 k2-fsa/sherpa-onnx-tts-matcha-icefall-zh-en
        model_id = "k2-fsa/sherpa-onnx-tts-matcha-icefall-zh-en"
        # 默认文件映射: 远程文件名 -> 本地路径
        files_to_download = {
            "model-int8.onnx": self.model_files["model"],
            "vocab.txt": self.model_files["vocab"],
            "lexicon.txt": self.model_files["lexicon"],
            "tokens.txt": self.model_files["tokens"],
        }

        for remote_name, local_path in files_to_download.items():
            if not os.path.isfile(local_path):
                logger.bind(tag=TAG).info(f"正在下载模型文件: {remote_name}")
                model_file_download(
                    model_id=model_id,
                    file_path=remote_name,
                    local_dir=self.model_dir,
                )
                if not os.path.isfile(local_path):
                    raise FileNotFoundError(f"模型文件下载失败: {local_path}")

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
                    f"语音生成失败: {text}，请检查模型是否正确加载"
                )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to generate TTS file: {e}")
        finally:
            return None

    async def text_to_speak(self, text, output_file=None, is_last=False):
        """流式处理TTS音频，本地合成直接流式输出"""
        # 如果output_file不为None，保持向后兼容
        if output_file is not None:
            # 直接生成完整音频写入文件
            audio = self.model.generate(text, speed=self.speed)
            samples_float32 = np.array(audio.samples, dtype=np.float32)
            # Matcha TTS 输出是 22050Hz，重采样到 16kHz
            from pydub import AudioSegment
            from io import BytesIO
            import wave

            samples_int16 = (samples_float32 * 32768).astype(np.int16)
            wav_io = BytesIO()
            with wave.open(wav_io, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(audio.sample_rate)
                wf.writeframes(samples_int16.tobytes())
            wav_io.seek(0)
            audio_segment = AudioSegment.from_file(wav_io, format="wav")
            audio_segment = audio_segment.set_frame_rate(self.output_sample_rate).set_channels(1)
            audio_segment.export(output_file, format="wav")
            return None

        # 流式处理模式
        frame_bytes = int(
            self.opus_encoder.sample_rate
            * self.opus_encoder.channels
            * self.opus_encoder.frame_size_ms
            / 1000
            * 2
        )  # 16-bit = 2 bytes

        try:
            self.pcm_buffer.clear()
            self.first_opus_sent = False
            self.start_time = time.time()
            self.tts_audio_queue.put((SentenceType.FIRST, [], text))

            # SherpaOnnx TTS 生成完整音频
            # 注: SherpaOnnx OfflineTts 一次性生成完整音频
            # 但我们仍然可以分割成帧流式输出，这样第一帧可以快速发送
            start_gen = time.time()
            audio = self.model.generate(text, speed=self.speed)
            logger.bind(tag=TAG).debug(f"SherpaOnnx 生成完成，总采样数: {len(audio.samples)}, 耗时: {time.time() - start_gen:.3f}s, 输入采样率: {audio.sample_rate}")

            # Matcha TTS 输出是 22050Hz，需要重采样到 16kHz
            from pydub import AudioSegment
            from io import BytesIO
            import wave

            # 转换PCM格式: float32 -> int16 -> wav
            samples_float32 = np.array(audio.samples, dtype=np.float32)
            samples_int16 = (samples_float32 * 32768).astype(np.int16)
            wav_io = BytesIO()
            with wave.open(wav_io, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(audio.sample_rate)
                wf.writeframes(samples_int16.tobytes())
            wav_io.seek(0)

            # 重采样到 16kHz
            audio_segment = AudioSegment.from_file(wav_io, format="wav")
            audio_segment = audio_segment.set_frame_rate(self.output_sample_rate).set_channels(1)
            pcm_bytes = audio_segment.raw_data

            self.pcm_buffer.extend(pcm_bytes)

            # 分帧处理，逐步输出
            while len(self.pcm_buffer) >= frame_bytes:
                frame = bytes(self.pcm_buffer[:frame_bytes])
                del self.pcm_buffer[:frame_bytes]

                if not self.first_opus_sent:
                    # 记录首包延迟
                    first_latency = time.time() - self.start_time
                    logger.bind(tag=TAG).info(f"SherpaOnnx TTS 首包延迟: {first_latency:.3f}s")
                    self.first_opus_sent = True

                self.opus_encoder.encode_pcm_to_opus_stream(
                    frame, end_of_stream=False, callback=self.handle_opus
                )

            # 处理剩余的PCM数据
            if len(self.pcm_buffer) > 0:
                # 填充不足一帧的数据
                if len(self.pcm_buffer) < frame_bytes:
                    padding_needed = frame_bytes - len(self.pcm_buffer)
                    self.pcm_buffer.extend(b"\x00" * padding_needed)

                if not self.first_opus_sent:
                    # 记录首包延迟（这是唯一的一帧）
                    first_latency = time.time() - self.start_time
                    logger.bind(tag=TAG).info(f"SherpaOnnx TTS 首包延迟: {first_latency:.3f}s")
                    self.first_opus_sent = True

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
            logger.bind(tag=TAG).error(f"SherpaOnnx TTS streaming error: {e}")
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
            # 生成完整音频
            audio = self.model.generate(text, speed=self.speed)
            samples_float32 = np.array(audio.samples, dtype=np.float32)

            # Matcha TTS 输出是 22050Hz，需要重采样到 16kHz
            from pydub import AudioSegment
            from io import BytesIO
            import wave

            samples_int16 = (samples_float32 * 32768).astype(np.int16)
            wav_io = BytesIO()
            with wave.open(wav_io, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(audio.sample_rate)
                wf.writeframes(samples_int16.tobytes())
            wav_io.seek(0)

            # 重采样到 16kHz
            audio_segment = AudioSegment.from_file(wav_io, format="wav")
            audio_segment = audio_segment.set_frame_rate(self.output_sample_rate).set_channels(1)
            pcm_data = bytearray(audio_segment.raw_data)

            logger.info(f"SherpaOnnx TTS 合成成功: {text}, 总耗时: {time.time() - start_time:.3f}秒")

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
            logger.bind(tag=TAG).error(f"SherpaOnnx TTS non-streaming error: {e}")
            return []

    async def close(self):
        """资源清理"""
        await super().close()
        if hasattr(self, "opus_encoder"):
            self.opus_encoder.close()
