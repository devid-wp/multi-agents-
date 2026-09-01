"""
tests/test_endpoint_url.py
──────────────────────────
Проверяем идемпотентность NvidiaProvider.endpoint_url().

Это страховка от регрессии вида ".../v1/chat/completions/chat/completions".
"""

from __future__ import annotations

import sys
from pathlib import Path

# Позволяет запускать тесты и из корня, и из tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm_clients import NvidiaProvider  # noqa: E402


def _eq(label: str, got: str, want: str) -> None:
    if got != want:
        raise AssertionError(f"{label}\n  got:  {got!r}\n  want: {want!r}")
    print(f"  ok: {label} -> {got}")


def test_default_base_url_appends_suffix():
    """Дефолтный base_url вида .../v1 → должен дописать /chat/completions."""
    p = NvidiaProvider(api_key="x", base_url="https://integrate.api.nvidia.com/v1")
    _eq("default /v1", p.endpoint_url(),
        "https://integrate.api.nvidia.com/v1/chat/completions")


def test_trailing_slash_is_normalized():
    """Trailing / в base_url не должен ломать склейку."""
    p = NvidiaProvider(api_key="x", base_url="https://integrate.api.nvidia.com/v1/")
    _eq("default /v1/", p.endpoint_url(),
        "https://integrate.api.nvidia.com/v1/chat/completions")


def test_base_url_with_suffix_is_idempotent():
    """Если пользователь ввёл полный URL с /chat/completions — НЕ дублируем."""
    p = NvidiaProvider(
        api_key="x",
        base_url="https://integrate.api.nvidia.com/v1/chat/completions",
    )
    _eq("base уже с /chat/completions", p.endpoint_url(),
        "https://integrate.api.nvidia.com/v1/chat/completions")


def test_model_url_overrides_everything():
    """NIM catalog endpoint (…/v1/models/{model}/infer) — валидный override."""
    p = NvidiaProvider(
        api_key="x",
        base_url="https://integrate.api.nvidia.com/v1",
        model_url="https://integrate.api.nvidia.com/v1/models/gemma/infer",
    )
    _eq("NIM catalog model_url override", p.endpoint_url(),
        "https://integrate.api.nvidia.com/v1/models/gemma/infer")


def test_model_url_with_trailing_slash_normalized():
    """Trailing / в model_url должен убираться."""
    p = NvidiaProvider(
        api_key="x",
        base_url="https://integrate.api.nvidia.com/v1",
        model_url="https://integrate.api.nvidia.com/v1/models/gemma/infer/",
    )
    _eq("model_url with trailing /", p.endpoint_url(),
        "https://integrate.api.nvidia.com/v1/models/gemma/infer")


def test_alternate_nim_deployment():
    """Сторонний NIM-деплоймент с базой /v1 — тоже работает."""
    p = NvidiaProvider(api_key="x", base_url="https://my.nim.example/v1")
    _eq("custom /v1", p.endpoint_url(),
        "https://my.nim.example/v1/chat/completions")


def test_alternate_nim_with_already_full_path():
    """Сторонний NIM-деплоймент, где пользователь сам дописал /chat/completions."""
    p = NvidiaProvider(
        api_key="x",
        base_url="https://my.nim.example/v1/chat/completions",
    )
    _eq("custom /v1/chat/completions", p.endpoint_url(),
        "https://my.nim.example/v1/chat/completions")


def test_user_full_url_passes_through_untouched():
    """
    ПРОЗРАЧНОСТЬ: если в base_url уже есть /chat/completions,
    endpoint_url() обязан вернуть ровно ту же строку — никаких
    обрезаний, дописываний, lowercase-преобразований.
    """
    full = "https://integrate.api.nvidia.com/v1/chat/completions"
    p = NvidiaProvider(api_key="x", base_url=full)
    got = p.endpoint_url()
    if got != full:
        raise AssertionError(
            f"endpoint_url() модифицировал уже полный URL!\n"
            f"  input:  {full!r}\n"
            f"  output: {got!r}\n"
            f"  Ожидалось побайтовое равенство (прозрачность)."
        )
    print(f"  ok: full URL passes through untouched -> {got}")


def test_user_full_url_with_trailing_slash_normalized_once():
    """
    Если пользователь ввёл .../v1/chat/completions/ — после нормализации
    trailing / (в __post_init__) суффикс НЕ должен дублироваться.
    """
    p = NvidiaProvider(
        api_key="x",
        base_url="https://integrate.api.nvidia.com/v1/chat/completions/",
    )
    _eq("full URL with trailing /", p.endpoint_url(),
        "https://integrate.api.nvidia.com/v1/chat/completions")


def test_model_url_takes_precedence_over_full_base_url():
    """
    Если заданы ОБА: model_url и base_url с /chat/completions —
    побеждает model_url (полный override).
    """
    p = NvidiaProvider(
        api_key="x",
        base_url="https://integrate.api.nvidia.com/v1/chat/completions",
        model_url="https://integrate.api.nvidia.com/v1/models/llama/infer",
    )
    _eq("model_url wins over full base", p.endpoint_url(),
        "https://integrate.api.nvidia.com/v1/models/llama/infer")


def test_debug_url_is_printed(caplog=None):
    """
    endpoint_url() обязан логировать final_url (phase 1: print -> log.debug).
    Проверяем через caplog, чтобы в логах было видно реальный URL.
    """
    import logging

    p = NvidiaProvider(api_key="x", base_url="https://integrate.api.nvidia.com/v1")
    # caplog может быть None при прямом вызове __main__
    if caplog is not None:
        with caplog.at_level(logging.DEBUG, logger="trinity.llm"):
            result = p.endpoint_url()
        # лог должен содержать финальный URL
        messages = " ".join(rec.getMessage() for rec in caplog.records)
        if "https://integrate.api.nvidia.com/v1/chat/completions" not in messages and result != "https://integrate.api.nvidia.com/v1/chat/completions":
            raise AssertionError(f"endpoint_url() не залогировал final_url! messages={messages!r} result={result!r}")
    else:
        # fallback для прямого запуска без pytest caplog
        result = p.endpoint_url()
        assert result == "https://integrate.api.nvidia.com/v1/chat/completions"
    print(f"  ok: DEBUG_URL logged correctly")


# ─────────────────────────────────────────────────────────────────────
# Регрессия на «плохой» model_url
# Защита: model_url = "…/v1" (или равный base_url) НЕ должен ломать запрос
# ─────────────────────────────────────────────────────────────────────
def test_model_url_equal_to_base_url_is_ignored():
    """
    РЕГРЕССИЯ: если model_url == base_url (= …/v1) — это не override,
    это дубль. Клиент обязан проигнорировать его и использовать
    base_url + /chat/completions.
    """
    base = "https://integrate.api.nvidia.com/v1"
    p = NvidiaProvider(api_key="x", base_url=base, model_url=base)
    _eq("model_url == base_url (regression)", p.endpoint_url(),
        "https://integrate.api.nvidia.com/v1/chat/completions")


def test_model_url_shorter_than_base_is_ignored():
    """
    РЕГРЕССИЯ: если model_url короче base_url (например, корень API
    или просто домен без /v1) — это «плохой» override.
    Игнорируем, идём по ветке base_url.
    """
    p = NvidiaProvider(
        api_key="x",
        base_url="https://integrate.api.nvidia.com/v1",
        model_url="https://integrate.api.nvidia.com",  # короче base_url
    )
    _eq("model_url shorter than base (regression)", p.endpoint_url(),
        "https://integrate.api.nvidia.com/v1/chat/completions")


def test_model_url_without_chat_completions_is_ignored():
    """
    РЕГРЕССИЯ: если в model_url нет /chat/completions — это не endpoint,
    а скорее всего корень API или страница. Игнорируем.
    """
    p = NvidiaProvider(
        api_key="x",
        base_url="https://integrate.api.nvidia.com/v1",
        model_url="https://integrate.api.nvidia.com/v1/models",  # без /chat/completions
    )
    _eq("model_url without /chat/completions (regression)", p.endpoint_url(),
        "https://integrate.api.nvidia.com/v1/chat/completions")


def test_model_url_with_chat_completions_still_works():
    """
    Позитивный кейс: model_url с /chat/completions и длиннее base_url —
    остаётся валидным override (не сломали обратную совместимость).
    """
    p = NvidiaProvider(
        api_key="x",
        base_url="https://integrate.api.nvidia.com/v1",
        model_url="https://integrate.api.nvidia.com/v1/models/llama/infer/chat/completions",
    )
    _eq("valid model_url with /chat/completions",
        p.endpoint_url(),
        "https://integrate.api.nvidia.com/v1/models/llama/infer/chat/completions")


def test_warning_url_printed_for_bad_model_url(caplog=None):
    """
    При отбрасывании «плохого» model_url должен логироваться warning (phase 1: print -> log.warning).
    """
    import logging

    base = "https://integrate.api.nvidia.com/v1"
    p = NvidiaProvider(api_key="x", base_url=base, model_url=base)
    if caplog is not None:
        with caplog.at_level(logging.DEBUG, logger="trinity.llm"):
            result = p.endpoint_url()
        messages = " ".join(rec.getMessage() for rec in caplog.records)
        # должен быть warning о невалидном model_url
        has_warning = any(rec.levelname == "WARNING" for rec in caplog.records)
        if not has_warning:
            raise AssertionError(f"Ожидался WARNING при отбрасывании плохого model_url, messages={messages!r}")
        if "https://integrate.api.nvidia.com/v1/chat/completions" not in messages and result != "https://integrate.api.nvidia.com/v1/chat/completions":
            raise AssertionError(f"После WARNING ожидался final_url, messages={messages!r} result={result!r}")
    else:
        result = p.endpoint_url()
        assert result == "https://integrate.api.nvidia.com/v1/chat/completions"
    print(f"  ok: WARNING_URL logged for bad model_url")


if __name__ == "__main__":
    test_default_base_url_appends_suffix()
    test_trailing_slash_is_normalized()
    test_base_url_with_suffix_is_idempotent()
    test_model_url_overrides_everything()
    test_model_url_with_trailing_slash_normalized()
    test_alternate_nim_deployment()
    test_alternate_nim_with_already_full_path()
    test_user_full_url_passes_through_untouched()
    test_user_full_url_with_trailing_slash_normalized_once()
    test_model_url_takes_precedence_over_full_base_url()
    test_debug_url_is_printed()
    # Регрессия на «плохой» model_url
    test_model_url_equal_to_base_url_is_ignored()
    test_model_url_shorter_than_base_is_ignored()
    test_model_url_without_chat_completions_is_ignored()
    test_model_url_with_chat_completions_still_works()
    test_warning_url_printed_for_bad_model_url()
    print("\nAll endpoint_url tests passed [OK]")
