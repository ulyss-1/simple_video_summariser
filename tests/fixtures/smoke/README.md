# Smoke fixtures

Fixtures for the hermetic `image` smokes (`tests/test_pipeline_smoke_docker.py`,
#137, and `tests/test_pipeline_whisper_smoke_docker.py`, #262).

## speech.opus

A short synthetic speech clip for the Whisper path. It is synthetic on purpose:
there is no licensing question and no real download. It is committed, never
generated at test time (CI's `images` job installs no ffmpeg).

- Spoken text: "Creativity is as important as literacy. Today we talk about how
  schools can help children grow their ideas."
- Voice: flite `slt`
- ffmpeg: 8.0.1-3ubuntu2 (Ubuntu, `--enable-libflite`)
- Length 7.1 s, about 21 KB, mono 16 kHz Opus

```sh
ffmpeg -y -f lavfi \
  -i "flite=text='Creativity is as important as literacy. Today we talk about how schools can help children grow their ideas.':voice=slt" \
  -ac 1 -ar 16000 -c:a libopus -b:a 24k speech.opus
```

A sine wave gives zero segments after faster-whisper's VAD, so real speech is
required.
