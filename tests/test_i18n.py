import re
import string
from pathlib import Path

from services.i18n import (
    STRINGS,
    get_current_lang,
    resolve_user_lang,
    set_current_lang,
    t,
)


def _extract_placeholders(template: str) -> set[str]:
    formatter = string.Formatter()
    return {
        field_name
        for _, field_name, _, _ in formatter.parse(template)
        if field_name is not None
    }


def test_every_key_has_fa_and_en() -> None:
    assert len(STRINGS) > 80
    for key, translations in STRINGS.items():
        assert "fa" in translations, f"Key '{key}' is missing 'fa' translation"
        assert "en" in translations, f"Key '{key}' is missing 'en' translation"
        assert translations["fa"].strip(), f"Key '{key}' has empty 'fa' translation"
        assert translations["en"].strip(), f"Key '{key}' has empty 'en' translation"


def test_placeholder_parity_between_fa_and_en() -> None:
    for key, translations in STRINGS.items():
        fa_vars = _extract_placeholders(translations["fa"])
        en_vars = _extract_placeholders(translations["en"])
        assert fa_vars == en_vars, (
            f"Placeholder mismatch in key '{key}': fa={fa_vars} vs en={en_vars}"
        )


def test_t_formatting_and_fallbacks() -> None:
    token = set_current_lang("en")
    try:
        assert get_current_lang() == "en"
        assert "Position #3" in t("queue_position", pos=3) or "#3" in t(
            "queue_position", pos=3
        )
        # Explicit lang parameter overrides contextvar
        assert "نفر 3 ام" in t("queue_position", lang="fa", pos=3)
        # Unknown language falls back to 'fa'
        assert "نفر 3 ام" in t("queue_position", lang="de", pos=3)
        # Unknown key returns the key itself without raising
        assert t("non_existent_key_xyz") == "non_existent_key_xyz"
    finally:
        from services.i18n import current_lang_var

        current_lang_var.reset(token)


def test_resolve_user_lang_priority() -> None:
    # 1. Explicit stored preference always wins
    assert resolve_user_lang(stored_lang="fa", default_lang="en") == "fa"
    assert resolve_user_lang(stored_lang="en", default_lang="fa") == "en"

    # 2. No choice yet -> DEFAULT_LANG
    assert resolve_user_lang(stored_lang=None, default_lang="en") == "en"
    assert resolve_user_lang(stored_lang=None, default_lang="fa") == "fa"
    assert resolve_user_lang() == "en"

    # 3. Invalid values are ignored
    assert resolve_user_lang(stored_lang="de", default_lang="xx") == "en"


def test_no_unintended_persian_in_handlers_or_services() -> None:
    arabic_script_re = re.compile(r"[\u0600-\u06FF]")
    repo_root = Path(__file__).resolve().parent.parent

    allowed_llm_prompt_files = {
        "services/i18n.py",
        "services/gemini_extractor.py",
        "services/subtitle_service.py",
    }

    for folder in ("handlers", "services"):
        for py_file in (repo_root / folder).glob("*.py"):
            rel_path = f"{folder}/{py_file.name}"
            if rel_path in allowed_llm_prompt_files:
                continue
            content = py_file.read_text(encoding="utf-8")
            matches = arabic_script_re.findall(content)
            assert not matches, (
                f"Found unexpected Persian/Arabic characters in {rel_path}"
            )
