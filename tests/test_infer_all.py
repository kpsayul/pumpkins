"""전체 코드 읽기 (--infer-all) — 우리 쪽 사전 판단이 하나도 안 들어가는 유일한 모드.

훑기는 '어떤 파일이 흥미로운가'를 판단하고, 구조 요약은 '어떤 사실이 중요한가'를
판단한다. 둘 다 내 판단이다. 전부 읽으면 그 자리가 사라진다.

이게 성립하는 이유는 뒤에 있다: 관찰마다 검사가 붙어 있어서, 열두 묶음에서 나온
백 개의 관찰을 **말로 합칠 필요가 없다.** 각각을 리포 전체에 재보면 된다."""

import os

import pytest

from pumpkins.conventions import (
    InferredRule,
    InferredRuleSet,
    RuleCheck,
    RuleInferrer,
)
from pumpkins.conventions import proposer as proposer_mod
from pumpkins.conventions.proposer import FileSlice, chunk_files
from pumpkins.llm.provider import ParsedResult


class _CountingClient:
    """묶음마다 한 번씩 호출되는지, 무엇을 받았는지 기록한다."""

    def __init__(self, per_call_rules=1):
        self.calls: list[dict] = []
        self._n = per_call_rules

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        rules = [
            InferredRule(
                rule=f"관찰 {len(self.calls)}-{i}",
                check=RuleCheck(kind="none"),
            )
            for i in range(self._n)
        ]
        return ParsedResult(parsed=InferredRuleSet(rules=rules), input_tokens=100, output_tokens=20)


def _repo(tmp_path, dirs=2, per_dir=3, size=200):
    for d in range(dirs):
        sub = tmp_path / "src" / f"mod{d}"
        sub.mkdir(parents=True)
        for i in range(per_dir):
            body = f"// {'x' * size}\nclass C{d}{i} {{ int m_v; }};\n"
            (sub / f"f{i}.h").write_text(body, encoding="utf-8")
            (sub / f"f{i}.cpp").write_text(f'#include "f{i}.h"\n' + body, encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------ 묶음 자르기

def test_a_header_and_its_source_stay_together(tmp_path):
    """선언과 구현이 두 호출로 갈리면, 어느 쪽도 온전한 걸 못 본다."""
    repo = _repo(tmp_path, dirs=1, per_dir=6, size=3000)
    chunks = chunk_files(sorted((repo / "src" / "mod0").iterdir()), max_chars=9000)
    assert len(chunks) > 1                       # 실제로 갈렸는지 먼저 확인
    for chunk in chunks:
        stems = {s.path.stem for s in chunk}
        for stem in stems:
            same = {s.path.suffix for s in chunk if s.path.stem == stem}
            # 같은 stem 의 .h 와 .cpp 가 리포에 둘 다 있으면 같은 묶음에 있어야 한다
            assert same in ({".h", ".cpp"}, {".h"}, {".cpp"})
            if same == {".h"} or same == {".cpp"}:
                others = [c for c in chunks if any(s.path.stem == stem for s in c)]
                assert len(others) == 1, f"{stem} 이 여러 묶음에 흩어졌다"


def test_files_are_grouped_by_directory(tmp_path):
    repo = _repo(tmp_path, dirs=3, per_dir=2, size=8000)
    files = sorted(p for p in repo.rglob("*") if p.is_file())
    chunks = chunk_files(files, max_chars=40_000)
    for chunk in chunks:
        dirs = {s.path.parent.name for s in chunk}
        assert len(dirs) <= 2, f"한 묶음이 디렉터리 {dirs} 를 넘나든다"


def test_an_oversized_file_is_sliced_not_dropped(tmp_path):
    """생성된 헤더 하나가 예산의 몇 배인 경우가 실제로 있다 (yaml-cpp 의 243 KB
    벤더 헤더). 앞부분만 보내고 '전부 읽었다'고 하면 조용한 공백이 된다."""
    big = tmp_path / "huge.h"
    big.write_text("\n".join(f"int v{i} = {i};" for i in range(20_000)), encoding="utf-8")
    chunks = chunk_files([big], max_chars=50_000)
    assert len(chunks) > 1
    covered = sum(len(s.read()) for c in chunks for s in c)
    assert covered == len(big.read_text(encoding="utf-8"))   # 한 글자도 안 잃는다
    # 자른 자리가 프롬프트에 드러나야 한다
    assert any(not s.is_whole for c in chunks for s in c)


def test_every_byte_of_the_repo_is_covered(tmp_path):
    repo = _repo(tmp_path, dirs=3, per_dir=4, size=2000)
    files = sorted(p for p in repo.rglob("*") if p.is_file())
    chunks = chunk_files(files, max_chars=20_000)
    covered = sum(len(s.read()) for c in chunks for s in c)
    assert covered == sum(p.stat().st_size for p in files)


# ------------------------------------------------------------ 전체 읽기 동작

def test_every_chunk_is_read_and_observations_are_collected(tmp_path, monkeypatch):
    repo = _repo(tmp_path, dirs=2, per_dir=3, size=4000)
    client = _CountingClient(per_call_rules=2)
    monkeypatch.setattr(proposer_mod, "get_client", lambda: client)

    outcome = RuleInferrer(model="strong").infer_all(repo, max_chunk_chars=20_000)

    assert outcome.chunks_read == len(client.calls) > 1
    assert len(outcome.rules) == 2 * len(client.calls)   # 묶음마다 관찰이 쌓인다
    # 전부 읽기는 강한 모델로 읽는다 — 싼 모델은 검사를 안 쓴다는 게 실측으로 드러났다
    assert all(c["model"] == "strong" for c in client.calls)


def test_each_chunk_gets_the_whole_repo_map(tmp_path, monkeypatch):
    """조각은 전체를 모른다 — 그래서 지도를 같이 준다. 싸고(≈1천 토큰), 계층처럼
    총합에만 있는 사실은 지도에만 있다."""
    repo = _repo(tmp_path, dirs=2, per_dir=3, size=4000)
    client = _CountingClient()
    monkeypatch.setattr(proposer_mod, "get_client", lambda: client)

    RuleInferrer(model="strong").infer_all(repo, max_chunk_chars=20_000)

    for call in client.calls:
        assert "Whole-repository map" in call["user"]
        assert "Include edges" in call["user"]


def test_a_chunk_is_told_not_to_judge_repo_wide(tmp_path, monkeypatch):
    """확신을 요구하면 세 번 본 패턴을 잡음으로 보고 침묵한다. 열 묶음에서 세 번씩
    나온 게 바로 게이트가 확인해 줄 관행인데."""
    repo = _repo(tmp_path, dirs=1, per_dir=3, size=1000)
    client = _CountingClient()
    monkeypatch.setattr(proposer_mod, "get_client", lambda: client)

    RuleInferrer(model="strong").infer_all(repo)

    (call,) = client.calls
    assert "do NOT try to judge whether a pattern holds repo-wide" in call["user"]
    assert "weak observations are filtered" in call["user"]
    assert "part 1 of 1" in call["user"]


def test_the_scale_is_reported_before_anything_is_sent(tmp_path, monkeypatch, caplog):
    """리포 전체를 남에게 보내는 건 사용자의 결정이고, 규모를 봐야 결정할 수 있다."""
    import logging

    repo = _repo(tmp_path, dirs=2, per_dir=3, size=4000)
    client = _CountingClient()
    monkeypatch.setattr(proposer_mod, "get_client", lambda: client)

    with caplog.at_level(logging.WARNING):
        outcome = RuleInferrer(model="gpt-4o-mini").infer_all(
            repo, max_chunk_chars=20_000
        )

    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "전체 코드 전송" in warning
    assert "개 파일" in warning and "토큰" in warning
    assert outcome.estimate is not None and outcome.estimate.tokens > 0


def test_no_cpp_files_means_no_calls(tmp_path, monkeypatch):
    (tmp_path / "readme.md").write_text("not code", encoding="utf-8")
    client = _CountingClient()
    monkeypatch.setattr(proposer_mod, "get_client", lambda: client)

    outcome = RuleInferrer().infer_all(tmp_path)

    assert client.calls == [] and outcome.rules == []
