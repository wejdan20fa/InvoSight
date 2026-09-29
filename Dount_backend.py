import gc
import io
from pathlib import Path

import streamlit as st
import torch
from PIL import Image, ImageOps
from transformers import DonutProcessor, VisionEncoderDecoderModel

MODEL_DIR = Path(__file__).resolve().parent / "final_model_DOUNT"


@st.cache_resource(show_spinner=False)
def load_donut():
    if not MODEL_DIR.is_dir():
        raise FileNotFoundError(f"Donut model directory was not found: {MODEL_DIR}")
    processor = DonutProcessor.from_pretrained(str(MODEL_DIR))
    model = VisionEncoderDecoderModel.from_pretrained(str(MODEL_DIR))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model.eval()
    return processor, model, device


def extract_invoice(image_bytes: bytes, unload_after: bool = True) -> str:
    """Extract Donut token text. Unload weights before optional OCR on 8 GB PCs."""
    processor, model, device = load_donut()
    pixel_values = decoder_input_ids = outputs = None
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
        pixel_values = processor(image, return_tensors="pt").pixel_values.to(device)
        decoder_input_ids = processor.tokenizer(
            "<s_invoice>", add_special_tokens=False, return_tensors="pt"
        ).input_ids.to(device)
        with torch.inference_mode():
            outputs = model.generate(
                pixel_values,
                decoder_input_ids=decoder_input_ids,
                max_length=512,
                num_beams=1,
                do_sample=False,
                pad_token_id=processor.tokenizer.pad_token_id,
                eos_token_id=model.config.eos_token_id,
            )
        return processor.batch_decode(outputs, skip_special_tokens=False)[0]
    finally:
        del pixel_values, decoder_input_ids, outputs
        if unload_after:
            load_donut.clear()
            del model, processor
            gc.collect()
            if device == "cuda":
                torch.cuda.empty_cache()
