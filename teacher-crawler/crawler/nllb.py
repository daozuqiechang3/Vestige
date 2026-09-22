from __future__ import annotations

import os
import sqlite3
import threading
from hashlib import sha256
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "facebook/nllb-200-distilled-600M"
SOURCE_LANGUAGE = "eng_Latn"
TARGET_LANGUAGE = "zho_Hans"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = ROOT / "output" / "models"
DEFAULT_CACHE_PATH = ROOT / "output" / "translation_cache.sqlite3"


class TranslationCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    @staticmethod
    def key(model_name: str, text: str) -> str:
        payload = f"{model_name}\0{SOURCE_LANGUAGE}\0{TARGET_LANGUAGE}\0{text}"
        return sha256(payload.encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS translations (
                cache_key TEXT PRIMARY KEY,
                model_name TEXT NOT NULL,
                source_text TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        return connection

    def get_many(self, model_name: str, texts: list[str]) -> dict[str, str]:
        keys = {self.key(model_name, text): text for text in texts}
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        with self.lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT cache_key, translated_text FROM translations "
                f"WHERE cache_key IN ({placeholders})",
                tuple(keys),
            ).fetchall()
        return {keys[key]: translated for key, translated in rows}

    def put_many(self, model_name: str, translations: dict[str, str]) -> None:
        if not translations:
            return
        rows = [
            (self.key(model_name, source), model_name, source, translated)
            for source, translated in translations.items()
        ]
        with self.lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO translations (
                    cache_key, model_name, source_text, translated_text
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    translated_text = excluded.translated_text
                """,
                rows,
            )


class NllbTranslator:
    def __init__(
        self,
        model_name: str | None = None,
        model_dir: Path | None = None,
        cache_path: Path | None = None,
    ) -> None:
        self.model_name = model_name or os.environ.get("ACL_NLLB_MODEL", DEFAULT_MODEL)
        self.model_dir = model_dir or Path(
            os.environ.get("ACL_NLLB_MODEL_DIR", DEFAULT_MODEL_DIR)
        )
        self.cache = TranslationCache(
            cache_path
            or Path(os.environ.get("ACL_TRANSLATION_CACHE", DEFAULT_CACHE_PATH))
        )
        self.batch_size = max(1, int(os.environ.get("ACL_NLLB_BATCH_SIZE", "4")))
        self.device_setting = os.environ.get("ACL_NLLB_DEVICE", "auto").strip().lower()
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._model: Any = None
        self._tokenizer: Any = None
        self._torch: Any = None
        self._device = ""
        self._load_error = ""

    @property
    def device(self) -> str:
        return self._device or self.device_setting

    def _load(self) -> None:
        if self._model is not None:
            return
        if self._load_error:
            raise RuntimeError(self._load_error)
        with self._load_lock:
            if self._model is not None:
                return
            if self._load_error:
                raise RuntimeError(self._load_error)
            try:
                self._initialize_model()
            except Exception as exc:
                self._load_error = (
                    "本地 NLLB 初始化失败，当前任务后续翻译将直接降级："
                    f"{type(exc).__name__}: {exc}"
                )
                raise RuntimeError(self._load_error) from exc

    def _initialize_model(self) -> None:
        try:
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError("本地 NLLB 依赖未安装，请重新安装项目依赖") from exc

        if self.device_setting not in {"auto", "cpu", "cuda"}:
            raise ValueError("ACL_NLLB_DEVICE 只能是 auto、cpu 或 cuda")
        if self.device_setting == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("已指定 CUDA，但当前 PyTorch 无法访问显卡")
        device = (
            "cuda"
            if self.device_setting == "cuda"
            or (self.device_setting == "auto" and torch.cuda.is_available())
            else "cpu"
        )
        dtype = torch.float16 if device == "cuda" else torch.float32
        self.model_dir.mkdir(parents=True, exist_ok=True)
        tokenizer = AutoTokenizer.from_pretrained(
            self.model_name,
            src_lang=SOURCE_LANGUAGE,
            cache_dir=self.model_dir,
        )
        model = AutoModelForSeq2SeqLM.from_pretrained(
            self.model_name,
            cache_dir=self.model_dir,
            torch_dtype=dtype,
        )
        model.to(device)
        model.eval()
        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model
        self._device = device

    def _generate(self, texts: list[str]) -> list[str]:
        self._load()
        tokenizer = self._tokenizer
        torch = self._torch
        encoded = tokenizer(
            texts,
            return_tensors="pt",
            padding=True,
            truncation=False,
        )
        input_length = int(encoded["input_ids"].shape[1])
        max_input_length = int(
            getattr(self._model.config, "max_position_embeddings", 1024)
        )
        if input_length > max_input_length:
            raise ValueError(
                f"NLLB 输入包含 {input_length} 个 token，超过模型上限 "
                f"{max_input_length}；请减小翻译分段长度"
            )
        encoded = {name: value.to(self._device) for name, value in encoded.items()}
        target_id = tokenizer.convert_tokens_to_ids(TARGET_LANGUAGE)
        with torch.inference_mode():
            generated = self._model.generate(
                **encoded,
                forced_bos_token_id=target_id,
                max_new_tokens=768,
                num_beams=2,
            )
        translations = tokenizer.batch_decode(generated, skip_special_tokens=True)
        if len(translations) != len(texts) or any(not value.strip() for value in translations):
            raise ValueError("NLLB 返回的译文数量或内容异常")
        return [value.strip() for value in translations]

    def translate_many(self, texts: list[str]) -> list[str]:
        if any(not isinstance(text, str) or not text for text in texts):
            raise ValueError("翻译文本不能为空")
        cached = self.cache.get_many(self.model_name, texts)
        missing = list(dict.fromkeys(text for text in texts if text not in cached))
        generated: dict[str, str] = {}
        if missing:
            with self._inference_lock:
                cached.update(self.cache.get_many(self.model_name, missing))
                missing = [text for text in missing if text not in cached]
                for offset in range(0, len(missing), self.batch_size):
                    batch = missing[offset : offset + self.batch_size]
                    translated = self._generate(batch)
                    generated.update(zip(batch, translated, strict=True))
                self.cache.put_many(self.model_name, generated)
        cached.update(generated)
        return [cached[text] for text in texts]


_TRANSLATOR: NllbTranslator | None = None
_TRANSLATOR_LOCK = threading.Lock()


def get_nllb_translator() -> NllbTranslator:
    global _TRANSLATOR
    if _TRANSLATOR is None:
        with _TRANSLATOR_LOCK:
            if _TRANSLATOR is None:
                _TRANSLATOR = NllbTranslator()
    return _TRANSLATOR
