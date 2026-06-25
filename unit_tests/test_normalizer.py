"""Unit tests for backend/normalizer.py"""

import pytest
from backend.normalizer import normalize_llm_text, normalize_code_quotes, reencode_unicode_escapes


# ---------------------------------------------------------------------------
# normalize_llm_text
# ---------------------------------------------------------------------------

class TestNormalizeLlmText:
    """Tests for normalize_llm_text — JSON-unsafe quote normalization."""

    # -- Straight quote conversions --

    def test_straight_double_quote_becomes_left_curly(self):
        """Straight double quote " becomes left double curly \u201c."""
        assert normalize_llm_text('"') == '\u201c'

    def test_straight_single_quote_becomes_right_curly(self):
        """Straight single quote ' becomes right single curly \u2019."""
        assert normalize_llm_text("'") == '\u2019'

    def test_multiple_straight_double_quotes(self):
        """Multiple straight double quotes all become left curly."""
        assert normalize_llm_text('"hello"') == '\u201chello\u201c'

    def test_multiple_straight_single_quotes(self):
        """Multiple straight single quotes all become right curly."""
        assert normalize_llm_text("it's a 'test'") == "it\u2019s a \u2019test\u2019"

    def test_mixed_single_and_double_quotes(self):
        """Mixed straight quotes are each mapped to their curly counterpart."""
        result = normalize_llm_text("""She said "it's fine".""")
        assert result == 'She said \u201cit\u2019s fine\u201c.'

    # -- Already-curly quotes pass through or are canonicalized --

    def test_left_double_curly_passes_through(self):
        """Left double curly \u201c is unchanged (not in mapping table)."""
        assert normalize_llm_text('\u201c') == '\u201c'

    def test_right_double_curly_passes_through(self):
        """Right double curly \u201d is unchanged (not in mapping table)."""
        assert normalize_llm_text('\u201d') == '\u201d'

    def test_right_single_curly_passes_through(self):
        """Right single curly \u2019 is unchanged (not in mapping table)."""
        assert normalize_llm_text('\u2019') == '\u2019'

    def test_left_single_curly_becomes_right_single_curly(self):
        """Left single curly \u2018 is mapped to right single curly \u2019."""
        assert normalize_llm_text('\u2018') == '\u2019'

    def test_low_double_quote_becomes_left_double_curly(self):
        """Low double quote \u201e is mapped to left double curly \u201c."""
        assert normalize_llm_text('\u201e') == '\u201c'

    def test_reversed_single_quote_becomes_right_single_curly(self):
        """Reversed single quote \u201b is mapped to right single curly \u2019."""
        assert normalize_llm_text('\u201b') == '\u2019'

    def test_already_curly_quotes_in_sentence_unchanged(self):
        """A sentence with existing curly quotes keeps them (except mapped ones)."""
        text = '\u201cHello\u201d she said, \u201cit\u2019s me\u201d'
        # \u201c → \u201c (unchanged), \u201d → \u201d (unchanged),
        # \u2018 not present, \u2019 → \u2019 (unchanged)
        result = normalize_llm_text(text)
        assert result == text

    # -- Mixed content, edge cases --

    def test_no_quotes_unchanged(self):
        """Plain text without any quotes is returned unchanged."""
        assert normalize_llm_text('Hello, world!') == 'Hello, world!'

    def test_empty_string(self):
        """Empty string returns empty string."""
        assert normalize_llm_text('') == ''

    def test_none_returns_none(self):
        """None returns None (falsy guard)."""
        assert normalize_llm_text(None) is None

    def test_text_with_only_aspect_chars_unchanged(self):
        """Backticks, dashes, and other punctuation are unchanged."""
        assert normalize_llm_text('`code` -- emphasis') == '`code` -- emphasis'

    def test_quotes_inside_json_string_value(self):
        """Simulates a JSON string value with embedded straight quotes."""
        text = '{"tool": "say", "arg": "it\'s ok"}'
        result = normalize_llm_text(text)
        # all " → \u201c, all ' → \u2019
        expected = '{\u201ctool\u201c: \u201csay\u201c, \u201carg\u201c: \u201cit\u2019s ok\u201c}'
        assert result == expected


# ---------------------------------------------------------------------------
# normalize_code_quotes
# ---------------------------------------------------------------------------

class TestNormalizeCodeQuotes:
    """Tests for normalize_code_quotes — smart/curly quote → ASCII conversion."""

    # -- Curly → straight conversions --

    def test_left_double_curly_becomes_straight_double(self):
        """Left double curly \u201c becomes straight double quote."""
        assert normalize_code_quotes('\u201c') == '"'

    def test_right_double_curly_becomes_straight_double(self):
        """Right double curly \u201d becomes straight double quote."""
        assert normalize_code_quotes('\u201d') == '"'

    def test_left_single_curly_becomes_straight_single(self):
        """Left single curly \u2018 becomes straight apostrophe."""
        assert normalize_code_quotes('\u2018') == "'"

    def test_right_single_curly_becomes_straight_single(self):
        """Right single curly \u2019 becomes straight apostrophe."""
        assert normalize_code_quotes('\u2019') == "'"

    def test_mixed_curly_double_quotes_in_sentence(self):
        """Text with curly double quotes gets fully normalized."""
        text = '\u201cHello, world!\u201d'
        assert normalize_code_quotes(text) == '"Hello, world!"'

    def test_mixed_curly_single_quotes_in_sentence(self):
        """Text with curly single quotes gets fully normalized."""
        text = '\u2018It\u2019s fine\u2019'
        assert normalize_code_quotes(text) == "'It's fine'"

    def test_all_four_curly_quotes_together(self):
        """All four curly quote forms are normalized to straight."""
        text = '\u201c\u201d\u2018\u2019'
        assert normalize_code_quotes(text) == '""\'\''

    # -- Straight quotes pass through --

    def test_straight_double_quote_passes_through(self):
        """Straight double quote is unchanged."""
        assert normalize_code_quotes('"') == '"'

    def test_straight_single_quote_passes_through(self):
        """Straight single quote is unchanged."""
        assert normalize_code_quotes("'") == "'"

    def test_straight_quotes_in_code_snippet_unchanged(self):
        """A code snippet with straight quotes is unchanged."""
        code = 'const x = "hello";\nconst y = \'world\';'
        assert normalize_code_quotes(code) == code

    # -- No-op cases --

    def test_no_quotes_unchanged(self):
        """Plain text without any quotes is returned unchanged."""
        assert normalize_code_quotes('Hello, world!') == 'Hello, world!'

    def test_empty_string(self):
        """Empty string returns empty string."""
        assert normalize_code_quotes('') == ''

    def test_none_returns_none(self):
        """None returns None (falsy guard)."""
        assert normalize_code_quotes(None) is None

    def test_whitespace_only(self):
        """Whitespace-only string passes through unchanged."""
        assert normalize_code_quotes('   \t\n  ') == '   \t\n  '

    # -- Mixed scenarios --

    def test_mixed_curly_and_straight_quotes(self):
        """Only curly quotes are converted; straight quotes remain."""
        text = '\u201cStraight " and curly\u201d'
        result = normalize_code_quotes(text)
        assert result == '"Straight " and curly"'


# ---------------------------------------------------------------------------
# reencode_unicode_escapes
# ---------------------------------------------------------------------------

class TestReencodeUnicodeEscapes:
    """Tests for reencode_unicode_escapes — non-ASCII → \\uXXXX encoding."""

    # -- BMP characters --

    def test_single_bmp_char(self):
        """A single BMP character (é) becomes its \\u escape."""
        assert reencode_unicode_escapes('\u00e9') == '\\u00e9'

    def test_bmp_char_at_lower_boundary(self):
        """First non-ASCII BMP char (U+0080) is re-encoded."""
        assert reencode_unicode_escapes('\u0080') == '\\u0080'

    def test_bmp_char_at_upper_boundary(self):
        """Last BMP char (U+FFFF) is re-encoded."""
        assert reencode_unicode_escapes('\uffff') == '\\uffff'

    def test_multiple_bmp_chars(self):
        """Multiple non-ASCII BMP characters are each re-encoded."""
        assert reencode_unicode_escapes('\u00e9\u00f1') == '\\u00e9\\u00f1'

    def test_bullet_character(self):
        """Bullet (U+2022) is re-encoded."""
        assert reencode_unicode_escapes('\u2022') == '\\u2022'

    def test_non_latin_script(self):
        """Cyrillic characters are re-encoded."""
        assert reencode_unicode_escapes('\u041f\u0440\u0438\u0432\u0435\u0442') == \
            '\\u041f\\u0440\\u0438\\u0432\\u0435\\u0442'

    # -- Supplementary / surrogate pairs --

    def test_single_emoji_becomes_surrogate_pair(self):
        """Emoji 😀 (U+1F600) becomes surrogate pair \\ud83d\\ude00."""
        assert reencode_unicode_escapes('\U0001f600') == '\\ud83d\\ude00'

    def test_another_emoji(self):
        """Emoji 🚀 (U+1F680) becomes surrogate pair."""
        assert reencode_unicode_escapes('\U0001f680') == '\\ud83d\\ude80'

    def test_emoji_at_high_supplementary(self):
        """Character near top of supplementary range (U+1FFFF) is encoded."""
        assert reencode_unicode_escapes('\U0001ffff') == '\\ud83f\\udfff'

    def test_multiple_emojis(self):
        """Multiple supplementary characters produce multiple pairs."""
        text = '\U0001f600\U0001f680'
        assert reencode_unicode_escapes(text) == '\\ud83d\\ude00\\ud83d\\ude80'

    # -- ASCII-only (fast path) --

    def test_ascii_only_unchanged(self):
        """ASCII-only string hits the isascii() fast path and is unchanged."""
        assert reencode_unicode_escapes('Hello, world!') == 'Hello, world!'

    def test_ascii_digits_and_punctuation_unchanged(self):
        """ASCII digits and punctuation are unchanged."""
        assert reencode_unicode_escapes('Price: $12.99 (10% off!)') == 'Price: $12.99 (10% off!)'

    def test_empty_string(self):
        """Empty string returns empty string."""
        assert reencode_unicode_escapes('') == ''

    def test_none_returns_none(self):
        """None returns None (falsy guard)."""
        assert reencode_unicode_escapes(None) is None

    def test_whitespace_only_ascii(self):
        """ASCII whitespace-only string passes through unchanged."""
        assert reencode_unicode_escapes('   \t\n  ') == '   \t\n  '

    # -- Mixed ASCII + non-ASCII --

    def test_mixed_ascii_and_bmp(self):
        """ASCII chars are kept verbatim; non-ASCII BMP are re-encoded."""
        result = reencode_unicode_escapes('caf\u00e9')
        assert result == 'caf\\u00e9'

    def test_mixed_ascii_and_emoji(self):
        """ASCII + emoji: ASCII stays, emoji becomes surrogate pair."""
        result = reencode_unicode_escapes('Hello \U0001f600')
        assert result == 'Hello \\ud83d\\ude00'

    def test_mixed_ascii_bmp_and_supplementary(self):
        """ASCII, BMP, and supplementary characters all handled correctly."""
        text = 'A\u00e9\U0001f600Z'
        result = reencode_unicode_escapes(text)
        assert result == 'A\\u00e9\\ud83d\\ude00Z'

    def test_newline_in_mixed_content(self):
        """Newline (ASCII control) passes through; non-ASCII is re-encoded."""
        result = reencode_unicode_escapes('line1\nl\u00e9ne2')
        assert result == 'line1\nl\\u00e9ne2'

    # -- Edge cases --

    def test_zero_width_non_joiner(self):
        """Zero-width non-joiner (U+200C) is re-encoded."""
        assert reencode_unicode_escapes('\u200c') == '\\u200c'

    def test_unicode_escape_in_source_itself(self):
        """The literal backslash-u sequence \u00e9 in source becomes \\u00e9 in output."""
        result = reencode_unicode_escapes('\u00e9')
        assert result == '\\u00e9'
        assert len(result) == 6  # backslash, u, 0, 0, e, 9

    def test_reencoded_output_is_pure_ascii(self):
        """The output of reencode_unicode_escapes is always ASCII-safe."""
        text = 'caf\u00e9 \U0001f600 end'
        result = reencode_unicode_escapes(text)
        assert result.isascii()
        assert 'caf\\u00e9 \\ud83d\\ude00 end' == result
