import asyncio
import io
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from breeze_client import BreezeError, synthesize


def wav_bytes(pcm=b'\x00\x00\x01\x00'):
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    return out.getvalue()


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.status = 200
        self.body = b'\x01\x00\x02\x00'
        self.active = 0
        self.max_active = 0
        app = web.Application()
        app.router.add_post('/v1/audio/speech', self.local)
        app.router.add_post('/v1/text-to-speech/{voice}', self.cloud)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def local(self, req):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            form = await req.post()
            record = dict(form)
            if 'ref_audio' in record:
                f = record['ref_audio']
                record['ref_audio'] = (f.filename, f.file.read())
            self.requests.append(record)
            await asyncio.sleep(0.01)
            return web.Response(body=self.body, status=self.status, headers={'X-Sample-Rate': '24000', 'X-Sample-Format': 's16le'})
        finally:
            self.active -= 1

    async def cloud(self, req):
        self.requests.append((req.match_info['voice'], req.query.get('output_format'), req.headers.get('xi-api-key'), await req.json()))
        return web.Response(body=self.body, status=self.status, content_type='audio/wav')

    async def test_self_hosted_pcm_conversion_and_single_concurrency(self):
        settings = {'api_url': self.url, 'instruction': '温柔', 'cfg_scale': 4, 'seed': 5}
        a, b = await asyncio.gather(synthesize('你好', settings), synthesize('再见', settings))
        self.assertEqual(self.max_active, 1)
        with wave.open(io.BytesIO(a), 'rb') as audio:
            self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()), (24000, 1, 2))
            self.assertEqual(audio.readframes(2), self.body)
        self.assertTrue(b.startswith(b'RIFF'))
        self.assertEqual(self.requests[0]['instruction'], '温柔')
        self.assertEqual(self.requests[0]['seed'], '5')

    async def test_reference_audio_and_pair_validation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'private-name.wav'
            path.write_bytes(b'reference')
            await synthesize('text', {'api_url': self.url, 'ref_audio': str(path), 'ref_text': 'exact'})
            self.assertEqual(self.requests[0]['ref_audio'], ('reference.wav', b'reference'))
            self.assertEqual(self.requests[0]['ref_text'], 'exact')
        with self.assertRaises(BreezeError):
            await synthesize('text', {'ref_audio': 'missing'})

    async def test_cloud_format_and_auth(self):
        self.body = wav_bytes()
        with patch.dict(os.environ, {'BREEZE_API_KEY': 'test-key'}):
            result = await synthesize('你好', {'backend': 'cloud', 'api_url': self.url, 'voice_id': 'voc_test', 'model': 'breeze-tts-2', 'instruction': '轻声', 'cfg_scale': 4})
        self.assertEqual(result, self.body)
        voice, output, key, payload = self.requests[0]
        self.assertEqual((voice, output, key), ('voc_test', 'wav', 'test-key'))
        self.assertEqual(payload, {'text': '你好', 'model_id': 'breeze-tts-2', 'instructions': '轻声', 'voice_settings': {'guidance_scale': 4.0}})

    async def test_reject_invalid_audio_and_http_errors_without_body(self):
        self.body = b'not-a-wave'
        with self.assertRaises(BreezeError):
            await synthesize('x', {'backend': 'cloud', 'api_url': self.url, 'api_key': 'secret', 'voice_id': 'voc_test'})
        self.body = b'secret data'
        self.status = 401
        with self.assertRaises(BreezeError) as error:
            await synthesize('x', {'backend': 'cloud', 'api_url': self.url, 'api_key': 'secret', 'voice_id': 'voc_test'})
        self.assertIn('401', str(error.exception))
        self.assertNotIn('secret', str(error.exception))

    async def test_invalid_settings_rejected_before_request(self):
        for settings in ({'cfg_scale': float('nan')}, {'timeout': 0}, {'backend': 'other'}, {'backend': 'cloud', 'cfg_scale': 0.5}, {'seed': -2}):
            with self.subTest(settings=settings), self.assertRaises(BreezeError):
                await synthesize('text', settings)
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
