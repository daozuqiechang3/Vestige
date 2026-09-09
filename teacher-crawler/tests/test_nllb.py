from pathlib import Path

import pytest

from crawler.nllb import NllbTranslator, TranslationCache


def test_translation_cache_round_trip(tmp_path: Path) -> None:
    cache = TranslationCache(tmp_path / "translations.sqlite3")
    cache.put_many("model", {"English": "中文", "Title": "标题"})

    assert cache.get_many("model", ["Title", "missing", "English"]) == {
        "Title": "标题",
        "English": "中文",
    }


def test_nllb_batches_only_uncached_unique_texts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ACL_NLLB_BATCH_SIZE", "2")
    translator = NllbTranslator(cache_path=tmp_path / "translations.sqlite3")
    translator.cache.put_many(translator.model_name, {"cached": "已缓存"})
    batches: list[list[str]] = []

    def generate(values: list[str]) -> list[str]:
        batches.append(values)
        return [f"译:{value}" for value in values]

    monkeypatch.setattr(translator, "_generate", generate)

    assert translator.translate_many(["cached", "one", "two", "one", "three"]) == [
        "已缓存",
        "译:one",
        "译:two",
        "译:one",
        "译:three",
    ]
    assert batches == [["one", "two"], ["three"]]

    batches.clear()
    assert translator.translate_many(["one", "three"]) == ["译:one", "译:three"]
    assert batches == []


def test_nllb_rejects_empty_text_without_loading_model(tmp_path: Path) -> None:
    translator = NllbTranslator(cache_path=tmp_path / "translations.sqlite3")

    with pytest.raises(ValueError, match="不能为空"):
        translator.translate_many([""])
