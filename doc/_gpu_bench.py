import logging
import subprocess
import sys
import time
import wave

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s %(message)s")

from app.transcriber.whisper import WhisperTranscriber

WAV = "D:/CyberStomach/VidInsight/doc/_gpu_test.wav"
SMI_OUT = "D:/CyberStomach/VidInsight/doc/_gpu_smi.log"

with wave.open(WAV, "rb") as w:
    audio_dur = w.getnframes() / w.getframerate()
    print(f"AUDIO: {WAV} duration={audio_dur:.1f}s rate={w.getframerate()}Hz ch={w.getnchannels()}")

# ---------- CPU ----------
t0 = time.perf_counter()
cpu = WhisperTranscriber(model_size="small", device="cpu")
t1 = time.perf_counter()
cpu_load = t1 - t0

r_cpu = cpu.transcript(WAV)
t2 = time.perf_counter()
cpu_trans = t2 - t1
print(f"CPU: load={cpu_load:.2f}s transcribe={cpu_trans:.2f}s device={cpu.device} text_len={len(r_cpu.full_text)}")

# ---------- GPU ----------
t3 = time.perf_counter()
gpu = WhisperTranscriber(model_size="small", device="cuda")
t4 = time.perf_counter()
gpu_load = t4 - t3

smi = None
try:
    smi = subprocess.Popen(
        ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv", "-l", "1"],
        stdout=open(SMI_OUT, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
except FileNotFoundError:
    print("nvidia-smi not found in PATH")

r_gpu = gpu.transcript(WAV)
t5 = time.perf_counter()
gpu_trans = t5 - t4
if smi:
    time.sleep(1.5)  # 让最后一次采样落盘
    smi.terminate()
print(f"GPU: load={gpu_load:.2f}s transcribe={gpu_trans:.2f}s device={gpu.device} text_len={len(r_gpu.full_text)}")

speedup = cpu_trans / gpu_trans if gpu_trans > 0 else float("inf")
print(f"SPEEDUP: {speedup:.1f}x (audio {audio_dur:.1f}s: CPU {cpu_trans:.2f}s -> GPU {gpu_trans:.2f}s)")
print(f"CPU head: {r_cpu.full_text[:80]}")
print(f"GPU head: {r_gpu.full_text[:80]}")
print("BENCH DONE")
