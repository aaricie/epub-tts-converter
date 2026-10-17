"""Read-only audio review probes; no server requests or source modification."""
import ast
import io
import json
from pathlib import Path

import av
import numpy as np

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "kokoro_runtime_src"


def sample_inventory():
    rows = []
    for path in Path(r"C:\Users\Aricie\Downloads\temp audio").glob("*.mp3"):
        with av.open(str(path)) as container:
            stream = container.streams.audio[0]
            samples = sum(frame.samples for frame in container.decode(stream))
            rows.append({"name": path.name, "bytes": path.stat().st_size,
                         "sample_rate": stream.sample_rate,
                         "bitrate": stream.bit_rate, "channels": stream.channels,
                         "decoded_samples": samples,
                         "decoded_seconds": samples / stream.sample_rate})
    return rows


def mp3_delay_probe():
    rows = []
    rate = 24000
    source = (np.sin(np.arange(rate) * 2 * np.pi * 440 / rate) * 12000).astype(np.int16)
    for bitrate in (128000, 48000):
        output = io.BytesIO()
        with av.open(output, "w", format="mp3", options={"write_xing": "0"}) as container:
            stream = container.add_stream("mp3", rate=rate, layout="mono")
            stream.bit_rate = bitrate
            frame = av.AudioFrame.from_ndarray(source.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = rate
            frame.pts = 0
            for packet in stream.encode(frame):
                container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
        data = output.getvalue()
        with av.open(io.BytesIO(data)) as container:
            frames = list(container.decode(container.streams.audio[0]))
        decoded = np.concatenate([frame.to_ndarray().flatten() for frame in frames])
        tag_length = 0
        if data[:3] == b"ID3":
            tag_length = 10 + ((data[6] & 127) << 21 | (data[7] & 127) << 14
                               | (data[8] & 127) << 7 | (data[9] & 127))
        rows.append({"bitrate": bitrate, "input_samples": len(source),
                     "decoded_samples": len(decoded), "id3_bytes": tag_length,
                     "audio_byte_duration_seconds": (len(data) - tag_length) * 8 / bitrate,
                     "first_signal_sample_abs_gt_0_01": int(np.argmax(np.abs(decoded) > .01))})
    return rows


async def swallowed_failure_probe():
    # Compile only the real runtime method; substitute inference dependencies.
    tree = ast.parse((RUNTIME / "services" / "tts_service.py").read_text(encoding="utf-8"))
    original_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                          and node.name == "TTSService")
    method = next(node for node in original_class.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "_process_chunk")
    cls = ast.ClassDef(name="ProbeService", bases=[], keywords=[], body=[method], decorator_list=[])
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
    ast.fix_missing_locations(module)
    logs = []

    class FakeLogger:
        def error(self, message):
            logs.append(str(message))

    class FakeKokoro:
        pass

    class FakeChunk:
        def __init__(self):
            self.audio = np.ones(24000, dtype=np.float32)

    class FakeManager:
        def get_backend(self):
            return FakeKokoro()

        async def generate(self, *args, **kwargs):
            yield FakeChunk()
            raise RuntimeError("injected failure on second internal chunk")

    class FakeAudioService:
        @staticmethod
        def trim_audio(chunk, *args):
            return chunk

    class FakeSemaphore:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    namespace = {"KokoroV1": FakeKokoro, "AudioService": FakeAudioService,
                 "logger": FakeLogger(), "np": np}
    exec(compile(module, str(RUNTIME / "services" / "tts_service.py"), "exec"), namespace)
    instance = namespace["ProbeService"]()
    instance._chunk_semaphore = FakeSemaphore()
    instance.model_manager = FakeManager()
    chunks = []
    async for chunk in instance._process_chunk("text", [1], "af_heart", "unused", 1.0, None):
        chunks.append(chunk)
    return {"raised_to_caller": False, "yielded_chunks": len(chunks),
            "yielded_samples": sum(len(chunk.audio) for chunk in chunks),
            "logged_errors": logs}


def run_without_io(coroutine):
    # All mocked awaits complete inline; avoid opening an event-loop socket pair.
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("Unexpected external I/O in a mock-only probe")


if __name__ == "__main__":
    result = {"pyav_version": av.__version__, "supplied_clips": sample_inventory(),
              "mp3_no_xing_delay": mp3_delay_probe(),
              "runtime_swallowed_failure": run_without_io(swallowed_failure_probe())}
    destination = ROOT / "compression_review_probe.json"
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
