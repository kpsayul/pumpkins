"""남의 코드를 리포에 물어봐서 가려내기.

이름 목록(`third_party`, `vendor`…)은 두 번 놓쳤다 — fmt 의 `test/gtest/`,
yaml-cpp 의 `src/contrib/dragonbox.h`. 목록을 늘리는 건 다음 번 누락을 미루는
것뿐이라, 리포가 스스로 말해 주는 신호를 쓴다."""

import subprocess

import pytest

from pumpkins.conventions import select_files, vendored


def _git_repo(tmp_path, commits=60):
    """커밋 이력이 있는 리포. 이력이 짧으면 churn 신호는 아무 말도 하지 않는다."""
    run = lambda *a: subprocess.run(
        ["git", "-C", str(tmp_path), *a], capture_output=True, check=True
    )
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (tmp_path / "src").mkdir()
    for i in range(commits):
        for n in range(6):
            (tmp_path / "src" / f"own{n}.h").write_text(
                f"class Own{n}_{i} {{ int m_v; }};\n" * 3, encoding="utf-8"
            )
        run("add", "-A")
        run("commit", "-qm", f"c{i}")
    return tmp_path


def _clear_cache():
    vendored._CACHE.clear()


# ------------------------------------------------------- 파일이 스스로 밝힌다

def test_a_different_copyright_holder_is_detected(tmp_path):
    """실측: dragonbox.h 가 `SPDX-FileCopyrightText: Junekey Jeon` 을 달고 있었다.
    파일이 직접 출처를 선언하는 것이라 추측이 아니다."""
    repo = _git_repo(tmp_path)
    (repo / "src" / "borrowed.h").write_text(
        "// SPDX-FileCopyrightText: 2020-2024 Someone Else\n"
        "// SPDX-License-Identifier: BSL-1.0\nclass B {};\n",
        encoding="utf-8",
    )
    _clear_cache()
    (found,) = vendored.detect(repo, sorted((repo / "src").iterdir()))
    assert found.path == "src/borrowed.h"
    assert "Someone Else" in found.reasons[0]


def test_the_repos_own_copyright_is_not_foreign(tmp_path):
    """모든 파일에 자기 저작권 헤더를 다는 리포도 많다 — 그게 외부 코드일 리 없다."""
    repo = _git_repo(tmp_path)
    for name in ("a.h", "b.h", "c.h"):
        (repo / "src" / name).write_text(
            "// Copyright (c) 2024 ACME Corp\nclass X {};\n", encoding="utf-8"
        )
    (repo / "src" / "borrowed.h").write_text(
        "// Copyright (c) 2019 Other Project\nclass B {};\n", encoding="utf-8"
    )
    _clear_cache()
    found = vendored.detect(repo, sorted((repo / "src").iterdir()))
    assert [f.path for f in found] == ["src/borrowed.h"]   # 다수파는 안 건드린다


def test_a_repo_that_headers_only_part_of_its_own_code_is_not_accused(tmp_path):
    """헤더를 일부에만 단 리포가 자기 코드를 남의 것으로 신고당하면 안 된다.
    '다르다'가 아니라 '드물다'가 외부 코드의 신호다."""
    repo = _git_repo(tmp_path)          # own0..5 는 헤더 없음
    for name in ("a.h", "b.h", "c.h", "d.h"):
        (repo / "src" / name).write_text(
            "// Copyright (c) 2024 ACME Corp\nclass X {};\n", encoding="utf-8"
        )
    _clear_cache()
    assert vendored.detect(repo, sorted((repo / "src").iterdir())) == []


@pytest.mark.parametrize(
    "line, expected",
    [
        ("// SPDX-FileCopyrightText: 2020-2024 Junekey Jeon", "Junekey Jeon"),
        ("// Copyright (c) 2024 ACME Corp", "ACME Corp"),
        ("/* Copyright 2019 Foo Bar */", "Foo Bar"),
        ("// Copyright (c) 2024 ACME. All rights reserved.", "ACME"),
        ("class Foo {};", None),
        ("// 이 줄은 저작권과 무관", None),
    ],
)
def test_copyright_holder_parsing(line, expected):
    assert vendored.copyright_holder(line + "\nclass X {};\n") == expected


# --------------------------------------------------------- 아무도 안 건드린다

def test_a_large_never_edited_file_is_detected(tmp_path):
    """실측 격차: dragonbox.h 는 커밋당 40KB, 리포 중앙값은 300바이트 근처.
    150배 차이라 경계선 판단이 아니다."""
    repo = _git_repo(tmp_path)
    big = repo / "src" / "huge.h"
    big.write_text("\n".join(f"int v{i} = {i};" for i in range(9000)), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "import"], check=True, capture_output=True,
    )
    _clear_cache()
    found = vendored.detect(repo, sorted((repo / "src").iterdir()))
    assert [f.path for f in found] == ["src/huge.h"]
    assert "거의 수정되지 않음" in found[0].reasons[0]


def test_a_short_history_says_nothing(tmp_path):
    """커밋 3개짜리 리포에서는 모든 파일이 '아무도 안 건드린' 상태다.
    이력이 짧으면 판단하지 않는다."""
    repo = _git_repo(tmp_path, commits=3)
    big = repo / "src" / "huge.h"
    big.write_text("\n".join(f"int v{i} = {i};" for i in range(9000)), encoding="utf-8")
    _clear_cache()
    assert vendored.detect(repo, sorted((repo / "src").iterdir())) == []


def test_a_small_rarely_edited_file_is_not_flagged(tmp_path):
    """작은 파일이 커밋이 적은 건 평범하다. 붙여넣은 건 크고 안 건드린 파일이다."""
    repo = _git_repo(tmp_path)
    (repo / "src" / "tiny.h").write_text("class Tiny {};\n", encoding="utf-8")
    _clear_cache()
    assert vendored.detect(repo, sorted((repo / "src").iterdir())) == []


# ------------------------------------------------------------- 스캔에 반영

def test_the_scan_drops_vendored_files_by_default(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "src" / "borrowed.h").write_text(
        "// Copyright (c) 2019 Other Project\nclass B {};\n", encoding="utf-8"
    )
    _clear_cache()
    kept = {p.name for p in select_files(repo)}
    assert "borrowed.h" not in kept and "own0.h" in kept


def test_the_exclusion_can_be_overruled(tmp_path):
    """남의 리포에 대한 판단이니 사람이 뒤집을 수 있어야 한다."""
    repo = _git_repo(tmp_path)
    (repo / "src" / "borrowed.h").write_text(
        "// Copyright (c) 2019 Other Project\nclass B {};\n", encoding="utf-8"
    )
    _clear_cache()
    kept = {p.name for p in select_files(repo, skip_vendored=False)}
    assert "borrowed.h" in kept


def test_the_exclusion_is_reported_not_silent(tmp_path, caplog):
    """조용한 제외는 조용한 포함과 같은 실패다 — 더 조용할 뿐."""
    import logging

    repo = _git_repo(tmp_path)
    (repo / "src" / "borrowed.h").write_text(
        "// Copyright (c) 2019 Other Project\nclass B {};\n", encoding="utf-8"
    )
    _clear_cache()
    with caplog.at_level(logging.INFO):
        select_files(repo)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "외부 코드로 판단해" in logged
    assert "borrowed.h" in logged and "Other Project" in logged


def test_a_repo_without_git_still_scans(tmp_path):
    """git 이 없거나 이력이 없어도 스캔은 죽지 않는다."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.h").write_text("class A {};\n", encoding="utf-8")
    _clear_cache()
    assert [p.name for p in select_files(tmp_path)] == ["a.h"]
