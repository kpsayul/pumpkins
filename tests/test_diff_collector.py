"""Smoke tests for the diff parsing stage (pure, no git needed)."""

from pumpkins.diff import parse_diff_text

SAMPLE_DIFF = """\
diff --git a/src/worker.cpp b/src/worker.cpp
index 1111111..2222222 100644
--- a/src/worker.cpp
+++ b/src/worker.cpp
@@ -10,4 +10,6 @@ void Worker::run() {
     while (running_) {
         process();
+        counter_++;
+        flush();
     }
 }
@@ -40,2 +42,3 @@ void Worker::stop() {
     running_ = false;
+    cv_.notify_all();
 }
diff --git a/README.md b/README.md
index 3333333..4444444 100644
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # readme
+not a c++ file
"""


def test_parses_only_cpp_files():
    files = parse_diff_text(SAMPLE_DIFF)
    assert [f.path for f in files] == ["src/worker.cpp"]


def test_added_line_ranges():
    (f,) = parse_diff_text(SAMPLE_DIFF)
    ranges = [(r.start, r.end) for r in f.added_ranges]
    assert ranges == [(12, 13), (43, 43)]


def test_patch_text_kept_for_llm_context():
    (f,) = parse_diff_text(SAMPLE_DIFF)
    assert "cv_.notify_all();" in f.patch_text


def test_empty_diff():
    assert parse_diff_text("") == []
