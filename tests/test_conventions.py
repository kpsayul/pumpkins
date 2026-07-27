"""Tests for the convention-learning stages that run without an LLM:
identifier extraction/statistics (L1) and the code-side threshold gate."""

from pathlib import Path

from pumpkins.conventions import (
    ConventionRule,
    LearnResult,
    apply_threshold_gate,
    casing_matches,
    extract_stats,
    split_pattern,
)

SAMPLE_CPP = """\
#include <mutex>
#include <vector>

class ThreadPool {
public:
    void submitTask(int id);
    int pendingCount() const;

private:
    std::mutex m_mutex;          // guards m_queue
    std::vector<int> m_queue;
    int m_capacity = 8;
    bool m_running;
};

struct WorkerGroup {
    int m_size;
};

void runLoop(ThreadPool& pool) {
    if (true) {
        pool.submitTask(1);      // call — must NOT count as a definition
    }
}
"""


def _stats_by_category(repo: Path) -> dict:
    return {s.category: s for s in extract_stats(repo)}


def test_extractor_categories_and_counts(tmp_path):
    (tmp_path / "pool.h").write_text(SAMPLE_CPP, encoding="utf-8")
    stats = _stats_by_category(tmp_path)

    members = stats["member_variable"]
    assert members.total == 5
    assert members.prefix_counts == {"m_": 5}

    classes = stats["class_type"]
    assert classes.total == 2
    assert classes.casing_counts == {"UpperCamel": 2}
    assert set(classes.samples) == {"ThreadPool", "WorkerGroup"}

    functions = stats["function"]
    assert set(functions.samples) == {"submitTask", "pendingCount", "runLoop"}
    assert functions.casing_counts == {"lowerCamel": 3}


def test_extractor_skips_vendored_dirs(tmp_path):
    (tmp_path / "third_party").mkdir()
    (tmp_path / "third_party" / "lib.h").write_text(SAMPLE_CPP, encoding="utf-8")
    assert sum(s.total for s in extract_stats(tmp_path)) == 0


def test_split_pattern_facets():
    assert split_pattern("m_maxCount") == ("m_", "(none)", "lowerCamel")
    assert split_pattern("queue_") == ("(none)", "_", "single_lower")
    assert split_pattern("kMaxSize") == ("k", "(none)", "UpperCamel")
    assert split_pattern("parse_diff_text") == ("(none)", "(none)", "lower_snake")
    assert split_pattern("HTTP_TIMEOUT") == ("(none)", "(none)", "UPPER_SNAKE")


def test_split_pattern_detects_underscoreless_hungarian_prefix():
    """Some codebases name members `mFoo`, not `m_foo`. Without this the repo
    reads as prefix-less and the learner adopts the opposite of the real rule."""
    assert split_pattern("mItemCount") == ("m", "(none)", "UpperCamel")
    assert split_pattern("kMaxRetryCount")[0] == "k"
    # a lowercase word merely starting with m/k is not a prefix
    assert split_pattern("max")[0] == "(none)"
    assert split_pattern("mutex")[0] == "(none)"
    assert split_pattern("kind")[0] == "(none)"


def test_casing_ambiguous_names_excluded_from_statistics(tmp_path):
    """`dump` satisfies lowerCamel and lower_snake equally. Counting it as its
    own style split fmt's single real convention 66/33 and hid it."""
    (tmp_path / "a.h").write_text(
        "void dump();\nvoid flush();\nvoid parse_one();\nvoid parse_two();\n",
        encoding="utf-8",
    )
    functions = _stats_by_category(tmp_path)["function"]
    assert functions.total == 4
    assert functions.casing_ambiguous == 2          # dump, flush
    assert functions.casing_informative == 2        # parse_one, parse_two
    assert functions.casing_counts == {"lower_snake": 2}   # 100%, not 50%


def test_casing_matches_treats_single_word_as_compatible():
    assert casing_matches("single_lower", "lowerCamel")
    assert casing_matches("single_lower", "lower_snake")
    assert casing_matches("lowerCamel", "lowerCamel")
    assert not casing_matches("lower_snake", "lowerCamel")
    assert not casing_matches("single_lower", "UpperCamel")


def test_constants_counted_apart_from_members(tmp_path):
    """Constants and mutable members follow different conventions; mixing them
    dragged a real repo's dominant member prefix below the threshold."""
    (tmp_path / "a.h").write_text(
        "class Pool {\n"
        "private:\n"
        "    static constexpr int kMaxSize = 8;\n"
        "    static const int kRetries = 3;\n"
        "    int mCount = 0;\n"
        "    const std::string& mName;\n"   # const ref member, not a constant
        "};\n",
        encoding="utf-8",
    )
    stats = _stats_by_category(tmp_path)
    assert stats["constant"].total == 2
    assert stats["constant"].prefix_counts == {"k": 2}
    assert stats["member_variable"].total == 2
    assert stats["member_variable"].prefix_counts == {"m": 2}


def test_template_parameters_and_macros_are_not_identifiers(tmp_path):
    """fmt read as 41% UpperCamel types because `template <class Char>` counted
    Char as a class and macros counted as functions."""
    (tmp_path / "a.h").write_text(
        "#define FMT_API inline\n"
        "#define FMT_ASSERT(cond, msg) ((void)0)\n"
        "template <typename T, class Char> struct value_holder {};\n"
        "template <typename OutputIt> auto write_one(OutputIt out) -> int;\n",
        encoding="utf-8",
    )
    stats = _stats_by_category(tmp_path)
    assert set(stats["class_type"].samples) == {"value_holder"}
    assert set(stats["function"].samples) == {"write_one"}


def _rule(occurrences: int, coverage: float) -> ConventionRule:
    return ConventionRule(
        id="member-prefix-m_",
        category="member_variable",
        description="멤버 변수는 m_ 접두사를 사용한다",
        coverage=coverage,
        occurrences=occurrences,
        confidence="high",
    )


def test_threshold_gate_demotes_weak_rules():
    result = LearnResult(rules=[_rule(187, 0.92), _rule(5, 0.99), _rule(100, 0.60)])
    gated = apply_threshold_gate(result)
    assert len(gated.rules) == 1
    assert gated.rules[0].occurrences == 187
    # demoted rules are preserved as rejected candidates, with the gate as reason
    assert len(gated.rejected) == 2
    assert all("threshold gate" in r.reason for r in gated.rejected)
