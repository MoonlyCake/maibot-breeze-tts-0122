"""Small async client for BreezeBlue cloud and the official self-hosted API.

The public interface returns a complete, mono or stereo PCM WAV file as bytes.
Neither request content, API keys, reference audio nor error response bodies are
logged or embedded in exceptions.
"""
from __future__ import annotations

import asyncio
import io
import math
import os
from pathlib import Path
from urllib.parse import quote, urlsplit
import wave
import weakref

import aiohttp

_MAX_AUDIO = 32 * 1024 * 1024
_MAX_REFERENCE = 32 * 1024 * 1024
_SELF_HOSTED = "http://127.0.0.1:7860"
_CLOUD = "https://api.breeze.blue"
_locks = weakref.WeakKeyDictionary()


class BreezeError(RuntimeError):
    """An actionable error that contains no request secrets or user content."""


def _lock_for_loop():
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _locks[loop] = lock
    return lock


def _validated_url(value, fallback):
    url = str(value or fallback).strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise BreezeError("Breeze API 地址无效；请填写不含凭据和查询参数的 http(s) 基础地址。")
    return url


def _wav_from_pcm(pcm, rate):
    if not pcm or len(pcm) % 2:
        raise BreezeError("Breeze 服务返回了空的或不完整的 PCM 音频。")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return output.getvalue()


def _validate_wav(audio):
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            if wav.getcomptype() != "NONE" or wav.getnchannels() not in (1, 2) or wav.getsampwidth() not in (1, 2, 3, 4) or wav.getframerate() < 8000 or wav.getnframes() < 1:
                raise ValueError("invalid WAV parameters")
            # Check the data is actually present, rather than trusting its header.
            data = wav.readframes(wav.getnframes())
            if len(data) != wav.getnframes() * wav.getnchannels() * wav.getsampwidth():
                raise ValueError("truncated WAV")
    except (EOFError, wave.Error, ValueError):
        raise BreezeError("Breeze 云端没有返回完整的 PCM WAV 音频。") from None
    return audio


async def _read_audio(response):
    data = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        data.extend(chunk)
        if len(data) > _MAX_AUDIO:
            raise BreezeError("Breeze 返回的音频超过 32 MiB。")
    return bytes(data)


def _reference_bytes(path):
    try:
        with Path(path).expanduser().open("rb") as handle:
            data = handle.read(_MAX_REFERENCE + 1)
    except OSError:
        raise BreezeError("无法读取参考音频文件，请检查路径和权限。") from None
    if not data or len(data) > _MAX_REFERENCE:
        raise BreezeError("参考音频不能为空或超过 32 MiB。")
    return data


async def synthesize(text: str, settings: dict) -> bytes:
    """Synthesize text and return WAV bytes, raising :class:`BreezeError` on failure.

    settings keys: backend, api_url, api_key, voice_id, model, instruction,
    ref_audio, ref_text, cfg_scale, seed, timeout, language_code. API keys can
    alternatively be provided via the BREEZE_API_KEY environment variable.
    """
    if not isinstance(text, str) or not text.strip():
        raise BreezeError("合成文本不能为空。")
    backend = str(settings.get("backend", "self_hosted") or "self_hosted").strip().lower()
    if backend not in ("self_hosted", "cloud"):
        raise BreezeError("backend 必须是 self_hosted 或 cloud。")
    try:
        cfg = float(settings.get("cfg_scale", 1.0))
        timeout = float(settings.get("timeout", 120))
        seed = int(settings.get("seed", -1))
    except (ValueError, TypeError, OverflowError):
        raise BreezeError("cfg_scale、timeout 或 seed 配置无效。") from None
    if not math.isfinite(cfg) or cfg <= 0 or (backend == "cloud" and cfg > 10) or (backend == "cloud" and cfg < 1):
        raise BreezeError("cfg_scale 必须大于 0；云端允许范围为 1–10。")
    if not math.isfinite(timeout) or timeout <= 0:
        raise BreezeError("timeout 必须大于 0。")
    if seed < -1 or seed > 4294967295:
        raise BreezeError("seed 应为 -1 或 0 至 4294967295 的整数。")
    instruction = str(settings.get("instruction", "") or "").strip()
    url = _validated_url(settings.get("api_url"), _CLOUD if backend == "cloud" else _SELF_HOSTED)
    request_headers = {}
    kwargs = {}
    if backend == "cloud":
        voice_id = str(settings.get("voice_id", "") or "").strip()
        key = str(settings.get("api_key", "") or os.environ.get("BREEZE_API_KEY", "")).strip()
        if not voice_id or not key:
            raise BreezeError("云端模式需要 voice_id 与 API key（可设置环境变量 BREEZE_API_KEY）。")
        # Do not let a crafted voice ID change the URL path.
        if not all(c.isalnum() or c in "_-" for c in voice_id):
            raise BreezeError("voice_id 只能包含字母、数字、下划线或连字符。")
        url += "/v1/text-to-speech/" + quote(voice_id, safe="") + "?output_format=wav"
        request_headers["xi-api-key"] = key
        payload = {"text": text, "voice_settings": {"guidance_scale": cfg}}
        for key_name, field in (("model", "model_id"), ("language_code", "language_code")):
            value = str(settings.get(key_name, "") or "").strip()
            if value:
                payload[field] = value
        if instruction:
            payload["instructions"] = instruction
        kwargs["json"] = payload
    else:
        ref_audio = str(settings.get("ref_audio", "") or "").strip()
        ref_text = str(settings.get("ref_text", "") or "").strip()
        if bool(ref_audio) != bool(ref_text):
            raise BreezeError("自托管参考音频和对应的准确文字必须同时填写。")
        url += "/v1/audio/speech"
        form = aiohttp.FormData()
        form.add_field("text", text)
        form.add_field("cfg_scale", str(cfg))
        if instruction:
            form.add_field("instruction", instruction)
        if seed >= 0:
            form.add_field("seed", str(seed))
        if ref_audio:
            suffix = Path(ref_audio).suffix.lower()
            if suffix not in {".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".aif", ".aiff"}:
                raise BreezeError("参考音频的文件扩展名不受支持，请使用 WAV、MP3、FLAC 等常见格式。")
            data = await asyncio.to_thread(_reference_bytes, ref_audio)
            # Keep only the safe extension so the server can select a decoder;
            # never disclose the local filename to the service.
            form.add_field("ref_audio", data, filename="reference" + suffix, content_type="application/octet-stream")
            form.add_field("ref_text", ref_text)
        kwargs["data"] = form
    try:
        # The official self-hosted server is single-concurrency. Serialize
        # requests in this process, preventing avoidable HTTP 409 responses.
        async with _lock_for_loop():
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(url, headers=request_headers, allow_redirects=False, **kwargs) as response:
                    if response.status != 200:
                        raise BreezeError(f"Breeze 服务返回 HTTP {response.status}；请检查服务状态与配置。")
                    data = await _read_audio(response)
                    if backend == "cloud":
                        return _validate_wav(data)
                    sample_format = response.headers.get("X-Sample-Format", "s16le").lower()
                    try:
                        rate = int(response.headers.get("X-Sample-Rate", "24000"))
                    except ValueError:
                        raise BreezeError("Breeze 服务返回的采样率无效。") from None
                    if sample_format != "s16le" or not 8000 <= rate <= 192000:
                        raise BreezeError("Breeze 服务返回了不支持的 PCM 格式或采样率。")
                    return _wav_from_pcm(data, rate)
    except asyncio.TimeoutError:
        raise BreezeError("Breeze 请求超时，请检查服务并适当调整 timeout。") from None
    except aiohttp.ClientError:
        raise BreezeError("无法连接 Breeze 服务，请检查地址和网络。") from None
