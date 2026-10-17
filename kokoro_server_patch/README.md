# Tested Kokoro server derivative

**Optional.** The converter now detects omitted passages itself by comparing
each response's word timestamps with the text it sent, which works with any
stock Kokoro-FastAPI version. These patches only apply to the June 2025 image
(v0.2.4) and are kept as defense in depth and as a record of the fault
injection results.

This directory maintains narrow patches for the installed June 2025 Kokoro
FastAPI GPU image. It does not download another model, upgrade dependencies, or
rewrite the original container. The base is pinned to the repository digest in
`Dockerfile`; the patch script verifies every affected file's SHA-256 before it
writes anything. A different upstream build requires a fresh review.

## Reliability changes

- Model, conversion, pause, and finalization errors propagate to the
  non-streaming endpoint, which returns an error instead of successful partial audio.
- Empty audio or missing chunks fail explicitly.
- English caption generation fails if timestamp extraction fails or returns no
  timestamps. French Kokoro can generate PCM but this version does not generate
  French word timestamps; use Edge for French word highlighting.
- CUDA out-of-memory errors clear the cache and propagate. Retrying inside an
  async generator can duplicate already-yielded speech, so the client retries the
  complete request instead.

`GET /epub-tts/revision` returns a stable cache identity derived from the pinned
base image and the actual patched source. Reliability and optimized images have
different identities. The converter includes this identity in recovery cache
keys so an engine upgrade cannot silently reuse audio from the previous model.
Manual changes to weights, voice files, or server configuration inside a running
container are outside this identity contract: rebuild the derivative and change
its revision, or clear the corresponding converter cache after such changes.

The converter uses `stream:false`. A streaming HTTP response may already have
sent its status and some audio before a later model failure; these patches cannot
retract bytes already sent. Do not infer complete synthesis from a streaming 200.

## Build and isolated verification

From the project directory:

```powershell
docker build --pull=false -t epub-kokoro:reliable-20261015 kokoro_server_patch
docker run -d --name epub-kokoro-reliable-test --gpus all -p 127.0.0.1:8881:8880 epub-kokoro:reliable-20261015
python audit/implementation/verify_pcm_contract.py --url http://localhost:8881 --phase full
```

The installed original container has no custom environment overrides or mounts;
its image defaults are therefore retained by the derivative. Test containers use
port 8881 and leave the original `kokoro-fastapi` container on 8880 unchanged.
Run synthesis sequentially across the containers to avoid GPU contention.

## Optional CPU optimizations

The default build applies only reliability fixes. After verifying that image:

```powershell
docker build --pull=false --build-arg ENABLE_PERFORMANCE=1 -t epub-kokoro:optimized-20261015 kokoro_server_patch
```

`performance_patch.py` scans silence in bounded 4,096-sample NumPy blocks and
concatenates PCM once. Padding rules are retained. Signed comparisons also fix
the original `abs(int16(-32768))` overflow. This does not change neural inference,
voice tensors, MP3 encoder effort, or paragraph batching.

## Rollback

The original image and original container remain available. Stop a test container
when finished with it:

```powershell
docker stop epub-kokoro-reliable-test
```

To return to the original engine, point the converter to `http://localhost:8880`
and, if it was deliberately stopped later, run `docker start kokoro-fastapi`.
Do not remove the original image/container until the new pipeline has been
checked in the intended reader.

## Evidence and limits

`tests/test_kokoro_server_patch.py` uses isolated classes from the reviewed
`audit/kokoro_runtime_src` snapshot and fake backends; it needs no GPU or model
imports. It skips if the audit snapshot or NumPy is not installed. The real Docker
build independently checks the exact source version. Live contract evidence is
under `audit/implementation/pcm_contract`; CPU measurements are in
`audit/implementation/server_cpu_benchmark.json`.

English timestamps are model estimates, not forced alignment. Small negative
starts, punctuation overlap, and end-of-punctuation overshoot occur in the stock
server. Clients must validate lexical coverage and preserve all PCM samples,
including tails and inserted pauses, instead of trimming audio to timestamps.
