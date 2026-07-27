"""Tests for reading the project's declared minimum C++ standard.

The portability profile judges constructs *relative to* this number, so getting
it wrong is worse than not having it: a minimum that is too high silences real
defects, one that is too low flags every modern construct in a modern project.
Hence "lowest declared wins" and "say nothing when nothing is declared".
"""

from pumpkins.analysis.cxx_standard import detect_cxx_standard


def _cmake(tmp_path, text: str):
    (tmp_path / "CMakeLists.txt").write_text(text, encoding="utf-8")
    return tmp_path


def _workflow(tmp_path, name: str, text: str):
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_reads_target_compile_features(tmp_path):
    """The strongest signal — the project stating its own contract."""
    repo = _cmake(tmp_path, "target_compile_features(fmt PUBLIC cxx_std_11)\n")
    result = detect_cxx_standard(repo)
    assert result.minimum == 11
    assert result.sources == ["CMakeLists.txt"]


def test_reads_cmake_cxx_standard(tmp_path):
    repo = _cmake(tmp_path, "set(CMAKE_CXX_STANDARD 17)\n")
    assert detect_cxx_standard(repo).minimum == 17


def test_reads_std_flag(tmp_path):
    repo = _cmake(tmp_path, 'set(CMAKE_CXX_FLAGS "-std=c++14 -Wall")\n')
    assert detect_cxx_standard(repo).minimum == 14


def test_reads_a_ci_matrix(tmp_path):
    repo = _workflow(tmp_path, "linux.yml", "    matrix:\n      std: [11, 14, 17]\n")
    assert detect_cxx_standard(repo).minimum == 11


def test_lowest_declared_wins(tmp_path):
    """A repo building tools at C++20 must still compile its library at 11 —
    the low number is the one that breaks CI."""
    repo = _cmake(tmp_path, "target_compile_features(lib PUBLIC cxx_std_11)\n"
                            "target_compile_features(tool PUBLIC cxx_std_20)\n")
    _workflow(repo, "ci.yml", "        std: [17, 20, 23]\n")
    result = detect_cxx_standard(repo)
    assert result.minimum == 11


def test_says_nothing_when_the_project_declares_nothing(tmp_path):
    """Guessing a default would make every modern construct a false positive."""
    assert detect_cxx_standard(tmp_path) is None
    assert detect_cxx_standard(_cmake(tmp_path, "project(thing)\n")) is None


def test_ignores_unresolved_cmake_variables(tmp_path):
    """`-DCMAKE_CXX_STANDARD=${{matrix.std}}` carries no number."""
    repo = _workflow(
        tmp_path, "ci.yml", "        run: cmake -DCMAKE_CXX_STANDARD=${{matrix.std}} ..\n"
    )
    assert detect_cxx_standard(repo) is None


def test_ignores_numbers_that_are_not_c_standards(tmp_path):
    repo = _cmake(tmp_path, "set(SOME_VERSION 99)\nset(CMAKE_CXX_STANDARD 17)\n")
    assert detect_cxx_standard(repo).minimum == 17


def test_describe_names_the_evidence(tmp_path):
    repo = _cmake(tmp_path, "target_compile_features(fmt PUBLIC cxx_std_11)\n")
    described = detect_cxx_standard(repo).describe()
    assert "C++11" in described and "CMakeLists.txt" in described
