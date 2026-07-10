"""Tests for the convention-learning stages that run without an LLM:
identifier extraction/statistics (L1) and the code-side threshold gate."""

from pathlib import Path

from pumpkins.conventions import (
    ConventionRule,
    LearnResult,
    apply_threshold_gate,
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
