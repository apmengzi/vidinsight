import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s %(message)s")

from app.transcriber.whisper import WhisperTranscriber

t = WhisperTranscriber(model_size="small", device="cuda")
print(f"ASSERT device == 'cuda': {t.device == 'cuda'} (actual: {t.device})")
assert t.device == "cuda", f"device fell back to {t.device}"
print("SMOKE OK")
