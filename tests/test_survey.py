"""구조 요약 — 싼 모델에게 넘길 '리포의 생김새'. LLM 없이 만들어진다."""

import os

import pytest

from pumpkins.conventions import render_survey, survey_repo
from pumpkins.languages.cpp import ast as cpp_ast

_ALLOW_MISSING = os.environ.get("PUMPKINS_ALLOW_NO_TREE_SITTER") == "1"
requires_ts = pytest.mark.skipif(
    not cpp_ast.available(), reason="PUMPKINS_ALLOW_NO_TREE_SITTER=1 (명시적 건너뛰기)"
)


def _repo(tmp_path):
    (tmp_path / "src" / "core").mkdir(parents=True)
    (tmp_path / "src" / "ui").mkdir(parents=True)
    (tmp_path / "src" / "core" / "engine.h").write_text(
        "#pragma once\nclass Engine : public Base {\n"
        "  std::unique_ptr<Impl> m_impl;\n  int m_count;\n};\n",
        encoding="utf-8",
    )
    for i in range(3):
        (tmp_path / "src" / "ui" / f"w{i}.h").write_text(
            f'#pragma once\n#include "engine.h"\nclass W{i} {{ Engine* m_engine; }};\n',
            encoding="utf-8",
        )
    return tmp_path


def test_survey_finds_the_one_way_dependency(tmp_path):
    survey = survey_repo(_repo(tmp_path))
    assert survey.include_edges[("src/ui", "src/core")] == 3
    assert survey.include_edges[("src/core", "src/ui")] == 0   # 한 방향뿐
    assert survey.dir_counts["src/ui"] == 3


def test_external_headers_are_not_layering(tmp_path):
    """레포에 없는 헤더(<vector>, 남의 라이브러리)는 계층 이야기가 아니다."""
    repo = _repo(tmp_path)
    (repo / "src" / "core" / "util.h").write_text(
        "#pragma once\n#include <vector>\n#include <spdlog/spdlog.h>\n", encoding="utf-8"
    )
    survey = survey_repo(repo)
    assert not any("vector" in dst or "spdlog" in dst for _, dst in survey.include_edges)


@requires_ts
def test_survey_carries_the_ownership_shape(tmp_path):
    survey = survey_repo(_repo(tmp_path))
    assert survey.smart_pointer_members == 1     # Engine::m_impl
    assert survey.raw_pointer_members == 3       # W0..W2 의 m_engine
    engine = next(c for c in survey.classes if c.name == "Engine")
    assert engine.bases == ["Base"]
    assert any("unique_ptr" in t for t in engine.member_types)


def test_rendered_survey_is_small_and_shows_the_asymmetry(tmp_path):
    """이 요약이 커지면 2단계 설계의 근거(싼 1차는 싸다)가 사라진다."""
    text = render_survey(survey_repo(_repo(tmp_path)))
    assert "src/ui -> src/core" in text
    assert "(reverse: 0)" in text        # 역방향이 0이라는 사실이 표에 드러난다
    assert "class W0" not in text        # 본문은 넘기지 않는다
    assert len(text) < 2000


def test_an_empty_scan_renders_to_nothing(tmp_path):
    (tmp_path / "readme.md").write_text("no code", encoding="utf-8")
    assert render_survey(survey_repo(tmp_path)) == ""


def test_missing_ast_is_stated_not_implied(tmp_path, monkeypatch):
    """클래스 목록이 그냥 비어 있으면 '이 리포엔 클래스가 없다'로 읽힌다 —
    1차 훑기를 엉뚱한 방향으로 보내는 침묵."""
    monkeypatch.setattr("pumpkins.conventions.survey.cpp_ast.require", lambda p: False)
    monkeypatch.setattr("pumpkins.conventions.survey.cpp_ast.engine", lambda: "regex-fallback")
    text = render_survey(survey_repo(_repo(tmp_path)))
    assert "unavailable" in text
    assert "src/ui -> src/core" in text   # 계층 정보는 AST 없이도 그대로 나온다
