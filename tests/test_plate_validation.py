import pytest
from backend.main import clean_and_validate_plate, is_ocr_fuzzy_match, OCR_CONFUSIONS


class TestExactMatch:
    """Exact whitelist matches must always return status ALLOWED."""

    def test_exact_match_allowed(self):
        whitelist = {"MP20CK7416"}
        res = clean_and_validate_plate("MP20CK7416", whitelist)
        assert res.plate == "MP20CK7416"
        assert res.status == "ALLOWED"

    def test_exact_match_with_whitespace_and_lowercase(self):
        whitelist = {"MP20CK7416"}
        res = clean_and_validate_plate("  mp 20 ck 7416 ", whitelist)
        assert res.plate == "MP20CK7416"
        assert res.status == "ALLOWED"

    def test_exact_match_with_ind_prefix(self):
        whitelist = {"MP20CK7416"}
        res = clean_and_validate_plate("IND MP20CK7416", whitelist)
        assert res.plate == "MP20CK7416"
        assert res.status == "ALLOWED"

    def test_exact_match_multiple_in_whitelist(self):
        whitelist = {"MH20DV2366", "MP20CK7416", "DL3CAQ0001"}
        res = clean_and_validate_plate("MH20DV2366", whitelist)
        assert res.plate == "MH20DV2366"
        assert res.status == "ALLOWED"


class TestFuzzyOcrConfusions:
    """
    Fuzzy matching applies ONLY to strings of the same length where every differing
    character pair is a recognized OCR confusion pair:
    (0/O, 0/Q, 1/I, 1/L, 2/Z, 5/S, 6/G, 8/B).
    Must return status NEEDS_CONFIRM, not ALLOWED.
    """

    def test_confusion_0_O(self):
        whitelist = {"MP20CK7416"}
        # 'O' instead of '0'
        res = clean_and_validate_plate("MP2OCK7416", whitelist)
        assert res.plate == "MP20CK7416"
        assert res.status == "NEEDS_CONFIRM"

    def test_confusion_0_Q(self):
        whitelist = {"MP20CK7416"}
        # 'Q' instead of '0'
        res = clean_and_validate_plate("MP2QCK7416", whitelist)
        assert res.plate == "MP20CK7416"
        assert res.status == "NEEDS_CONFIRM"

    def test_confusion_1_I(self):
        whitelist = {"MP20CK7416"}
        # 'I' instead of '1'
        res = clean_and_validate_plate("MP20CK74I6", whitelist)
        assert res.plate == "MP20CK7416"
        assert res.status == "NEEDS_CONFIRM"

    def test_confusion_1_L(self):
        whitelist = {"MP20CK7416"}
        # 'L' instead of '1'
        res = clean_and_validate_plate("MP20CK74L6", whitelist)
        assert res.plate == "MP20CK7416"
        assert res.status == "NEEDS_CONFIRM"

    def test_confusion_2_Z(self):
        whitelist = {"MP20CK7416"}
        # 'Z' instead of '2'
        res = clean_and_validate_plate("MPZ0CK7416", whitelist)
        assert res.plate == "MP20CK7416"
        assert res.status == "NEEDS_CONFIRM"

    def test_confusion_5_S(self):
        whitelist = {"MP20CK7415"}
        # 'S' instead of '5'
        res = clean_and_validate_plate("MP20CK741S", whitelist)
        assert res.plate == "MP20CK7415"
        assert res.status == "NEEDS_CONFIRM"

    def test_confusion_6_G(self):
        whitelist = {"MP20CK7416"}
        # 'G' instead of '6'
        res = clean_and_validate_plate("MP20CK741G", whitelist)
        assert res.plate == "MP20CK7416"
        assert res.status == "NEEDS_CONFIRM"

    def test_confusion_8_B(self):
        whitelist = {"MP20CK7418"}
        # 'B' instead of '8'
        res = clean_and_validate_plate("MP20CK741B", whitelist)
        assert res.plate == "MP20CK7418"
        assert res.status == "NEEDS_CONFIRM"

    def test_multiple_valid_confusions_same_string(self):
        whitelist = {"MP20CK7418"}
        # Both 'O'->'0' and 'B'->'8' are known confusions
        res = clean_and_validate_plate("MP2OCK741B", whitelist)
        assert res.plate == "MP20CK7418"
        assert res.status == "NEEDS_CONFIRM"


class TestSecurityNonConfusionEditDistance1:
    """
    Security check: Levenshtein distance 1 with non-confusion characters
    MUST NOT match the whitelist as ALLOWED or NEEDS_CONFIRM.
    """

    def test_different_number_tail_rejected(self):
        whitelist = {"MP20CK7416"}
        # '7' vs '6' is distance 1, but NOT an OCR confusion!
        res = clean_and_validate_plate("MP20CK7417", whitelist)
        assert res.status != "ALLOWED"
        assert res.status != "NEEDS_CONFIRM"
        assert res.status == "UNKNOWN"
        assert res.plate == "MP20CK7417"

    def test_non_confusion_letter_rejected(self):
        whitelist = {"MP20CK7416"}
        # 'A' vs '4' or 'D' vs 'C'
        res = clean_and_validate_plate("MP20DK7416", whitelist)
        assert res.status != "ALLOWED"
        assert res.status != "NEEDS_CONFIRM"


class TestNoInsertionsOrDeletions:
    """Fuzzy matching must not apply across different string lengths (no insertions/deletions)."""

    def test_deletion_rejected(self):
        whitelist = {"MP20CK7416"}
        # MP20CK741 has 9 chars vs 10 chars (deletion of '6')
        res = clean_and_validate_plate("MP20CK741", whitelist)
        assert res.status != "ALLOWED"
        assert res.status != "NEEDS_CONFIRM"
        assert res.plate != "MP20CK7416"

    def test_insertion_rejected(self):
        whitelist = {"MP20CK7416"}
        # MP20CK74160 has 11 chars vs 10 chars (insertion of '0')
        res = clean_and_validate_plate("MP20CK74160", whitelist)
        assert res.status != "ALLOWED"
        assert res.status != "NEEDS_CONFIRM"
        assert res.plate != "MP20CK7416"

    def test_prefix_deletion_rejected(self):
        whitelist = {"MP20CK7416"}
        # Missing state code first letter 'M' -> P20CK7416
        res = clean_and_validate_plate("P20CK7416", whitelist)
        assert res.status != "ALLOWED"
        assert res.status != "NEEDS_CONFIRM"
        assert res.plate != "MP20CK7416"


class TestMultipleWhitelistMatchesAmbiguity:
    """If more than one whitelist plate matches fuzzy criteria, return no match."""

    def test_multiple_fuzzy_matches_return_no_match(self):
        # MP20CK74I6 ('I') and MP20CK74L6 ('L') both confuse with candidate '1'
        whitelist = {"MP20CK74I6", "MP20CK74L6"}
        res = clean_and_validate_plate("MP20CK7416", whitelist)
        # Ambiguous match must NOT grant or confirm
        assert res.status != "ALLOWED"
        assert res.status != "NEEDS_CONFIRM"

    def test_exact_match_precedes_fuzzy(self):
        # If one plate is an exact match and another is fuzzy, exact match takes precedence
        whitelist = {"MP2OCK7416", "MP20CK7416"}
        res = clean_and_validate_plate("MP2OCK7416", whitelist)
        assert res.plate == "MP2OCK7416"
        assert res.status == "ALLOWED"


class TestInvalidAndUnregisteredPlates:
    """Verify handling of completely invalid or empty input."""

    def test_empty_string(self):
        res = clean_and_validate_plate("", {"MP20CK7416"})
        assert res.plate is None
        assert res.status is None
        assert not res

    def test_random_noise(self):
        res = clean_and_validate_plate("XYZ123", {"MP20CK7416"})
        assert res.plate is None
        assert res.status is None
        assert not res

    def test_unregistered_valid_plate(self):
        # Valid plate format but not in whitelist
        res = clean_and_validate_plate("DL3CAQ1111", {"MP20CK7416"})
        assert res.plate == "DL3CAQ1111"
        assert res.status == "UNKNOWN"
