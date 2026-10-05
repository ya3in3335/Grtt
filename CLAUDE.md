# Workflow: promo video → voiceover + music

When the user sends a promo video and asks for voice/music:
1. Inspect it: `ffprobe` (duration, audio) and contact sheets at 1 fps
   (`ffmpeg -i v.mp4 -vf "fps=1,scale=360:-1,tile=8x1" sheet_%d.png`), then read the on-screen text per scene.
2. Detect the existing beat tempo if any (onset autocorrelation) and reuse it as `music.bpm`; scene changes → `music.transitions`; logo/intro end → `drop`; CTA start → `outro`.
3. Write `projects/<name>/script.json`: one segment per scene, start ~0.3–0.5s after the scene appears, end before the next. Base text on the on-screen text, in the audience's dialect; add tashkeel to hard words. Tone: energetic body (`rate +6%`, `pitch +3..4Hz`), calm brand intro, CTA with `volume +10%`, higher pitch.
4. Run `python promo_voiceover.py projects/<name>/script.json -i video.mp4 -o out.mp4 --work-dir <scratch>/work`. If any segment needs > ~+15% rate, shorten its text and rerun.
5. Verify: output duration matches, `ebur128` ≈ -14 LUFS, speech ≈ 6–10 dB above music-only gaps. Send the video to the user.

Network: edge-tts needs `SSL_CERT_FILE`/`HTTPS_PROXY` behind the sandbox proxy — `tts.configure_ssl` handles it.
