"""Tests for the C++ syntax layer and per-repo extension overrides.

Access tracking exists because visibility is a real convention boundary that was
being averaged away: spdlog suffixes private members with `_` 100% of the time
and public fields 13% of the time, so measured together they read 72% and the
threshold gate rejected a rule that was actually two rules.
"""

import pytest

from pumpkins.languages import cpp, cpp_extensions, cpp_tu_extensions


def _categories(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for category, name in cpp.scan(text):
        out.setdefault(category, []).append(name)
    return out


# --------------------------------------------------------- access specifiers

def test_class_body_starts_private():
    found = _categories("class pool {\n    int count_;\n};\n")
    assert found["private_member"] == ["count_"]
    assert "public_field" not in found


def test_struct_body_starts_public():
    found = _categories("struct config {\n    int size;\n};\n")
    assert found["public_field"] == ["size"]
    assert "private_member" not in found


def test_specifier_switches_the_bucket():
    found = _categories(
        "class pool {\n"
        "public:\n"
        "    int capacity;\n"
        "private:\n"
        "    int count_;\n"
        "};\n"
    )
    assert found["public_field"] == ["capacity"]
    assert found["private_member"] == ["count_"]


def test_protected_counts_as_private():
    """Both are internal to the type, and projects that suffix one suffix both.
    Keeping them apart would only split the smaller denominator further."""
    found = _categories("class pool {\nprotected:\n    int count_;\n};\n")
    assert found["private_member"] == ["count_"]


def test_access_resets_for_each_class():
    found = _categories(
        "class a {\npublic:\n    int x;\n};\n"
        "class b {\n    int y_;\n};\n"  # 새 class → 다시 private
    )
    assert found["public_field"] == ["x"]
    assert found["private_member"] == ["y_"]


def test_constants_are_not_split_by_visibility():
    """Constants are named for their constness, not their visibility; splitting
    them would push the denominator under the occurrence threshold."""
    found = _categories(
        "class pool {\npublic:\n    static constexpr int kMax = 1;\n"
        "private:\n    static constexpr int kMin = 2;\n};\n"
    )
    assert sorted(found["constant"]) == ["kMax", "kMin"]


def test_unknown_visibility_falls_back_to_the_generic_category():
    """A diff hunk may show no specifier. Guessing is how false positives are
    made, so the member stays in the generic bucket."""
    assert cpp.match_identifiers("    int count;", True, None) == [
        ("member_variable", "count")
    ]


# ------------------------------------------------------- extension overrides

def test_defaults_without_a_repo_config(tmp_path):
    assert ".cpp" in cpp_extensions(tmp_path)
    assert ".ipp" not in cpp_extensions(tmp_path)


def test_repo_can_add_extensions(tmp_path):
    (tmp_path / ".pumpkins.yml").write_text(
        "languages:\n  cpp:\n    extra_extensions: ['.ipp', 'tcc']\n", encoding="utf-8"
    )
    extensions = cpp_extensions(tmp_path)
    assert ".ipp" in extensions and ".tcc" in extensions  # 점 없이 써도 정규화됨
    assert ".cpp" in extensions


def test_repo_can_remove_extensions(tmp_path):
    """A repo using `.tc` for YAML test cases must be able to say so — reading
    those as C++ produced findings against files that were never source."""
    (tmp_path / ".pumpkins.yml").write_text(
        "languages:\n  cpp:\n    exclude_extensions: ['.inl']\n", encoding="utf-8"
    )
    assert ".inl" not in cpp_extensions(tmp_path)


def test_removing_an_extension_also_removes_it_from_the_tu_set(tmp_path):
    (tmp_path / ".pumpkins.yml").write_text(
        "languages:\n  cpp:\n    exclude_extensions: ['.cc']\n", encoding="utf-8"
    )
    assert ".cc" not in cpp_tu_extensions(tmp_path)
    assert ".cpp" in cpp_tu_extensions(tmp_path)


def test_added_extensions_are_not_assumed_compilable(tmp_path):
    """Adding `.ipp` means "more header", not "more .cpp" — clang-tidy cannot
    analyze it standalone, so it must not enter the TU set by accident."""
    (tmp_path / ".pumpkins.yml").write_text(
        "languages:\n  cpp:\n    extra_extensions: ['.ipp']\n", encoding="utf-8"
    )
    assert ".ipp" in cpp_extensions(tmp_path)
    assert ".ipp" not in cpp_tu_extensions(tmp_path)


@pytest.mark.parametrize("content", ["languages: [not, a, map]", ": : broken yaml : :"])
def test_a_broken_config_is_ignored_not_fatal(tmp_path, content):
    (tmp_path / ".pumpkins.yml").write_text(content, encoding="utf-8")
    assert ".cpp" in cpp_extensions(tmp_path)
