from scripts.production_shape_smoke import sized_prompt


def test_sized_prompt_matches_real_character_and_utf8_shape_without_original_text():
    prompt = sized_prompt(6024, 6766)

    assert len(prompt) == 6024
    assert len(prompt.encode("utf-8")) == 6766
    assert "Return exactly one JSON object" in prompt
