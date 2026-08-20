# -*- coding: utf-8 -*-
"""회귀 테스트. 네트워크를 타지 않는 부분만 검사한다.

    python test_check.py      (의존성 0)
    pytest test_check.py

URL 파싱과 판정 로직만 본다. API 호출은 테스트하지 않는다 —
깃허브 응답을 흉내 낸 딕셔너리를 judge()에 직접 넣는다.
"""

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check import KST, judge, load_repos, parse_repo_url, parse_time

DEADLINE = datetime.datetime(2026, 8, 21, 10, 0, 0, tzinfo=KST)


# ── URL 파싱 ─────────────────────────────────────────────────────

def test_url_forms():
    """학생이 어떻게 적어 내든 같은 (owner, repo)로 떨어져야 한다.

    제출 폼은 자유 입력이라 형태가 제각각으로 들어온다.
    하나라도 못 읽으면 그 팀은 조용히 검사에서 빠진다.
    """
    for raw in [
        "https://github.com/team/proj",
        "https://github.com/team/proj/",
        "https://github.com/team/proj.git",
        "http://github.com/team/proj",
        "https://www.github.com/team/proj",
        "https://github.com/team/proj/tree/main",
        "https://github.com/team/proj/tree/main/src",
        "https://github.com/team/proj?tab=readme",
        "git@github.com:team/proj.git",
        "github.com/team/proj",
        "team/proj",
        "  https://github.com/team/proj  ",
        "<https://github.com/team/proj>",
    ]:
        assert parse_repo_url(raw) == ("team", "proj"), raw


def test_url_rejects_non_github():
    for raw in ["", "   ", "https://gitlab.com/a/b", "https://github.com/team",
                "그냥 텍스트", "https://bitbucket.org/a/b"]:
        assert parse_repo_url(raw) is None, raw


def test_url_keeps_case():
    """깃허브는 대소문자를 보존한다. 임의로 소문자화하면 표시가 틀어진다."""
    assert parse_repo_url("https://github.com/MyTeam/MyProj") == ("MyTeam", "MyProj")


def test_load_repos(tmp_path=None):
    """팀명 + 여러 URL, 주석, 빈 줄, 중복 URL을 한 파일에서 처리한다."""
    import tempfile
    body = (
        "# 주석\n"
        "\n"
        "팀하나, https://github.com/a/front, https://github.com/a/back\n"
        "https://github.com/b/solo\n"
        "한레포팀, https://github.com/c/mono, https://github.com/c/mono\n"
        "이건 주소가 없는 줄\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(body)
        path = f.name
    rows, bad = load_repos(path)
    got = [(t, o, n) for t, o, n, _ in rows]
    assert ("팀하나", "a", "front") in got
    assert ("팀하나", "a", "back") in got
    assert ("b/solo", "b", "solo") in got, "팀명이 없으면 owner/repo로 채운다"
    # 프론트·백엔드 칸에 같은 레포를 넣으라고 안내했으므로 중복이 정상 유입된다
    assert sum(1 for t, o, n in got if n == "mono") == 1, "중복 URL은 한 번만"
    assert len(bad) == 1, "읽지 못한 줄은 보고해야 한다"


# ── 판정 ─────────────────────────────────────────────────────────

def _info(**kw):
    base = {"owner": "t", "name": "r", "ok": True, "err": None, "private": False,
            "default_branch": "main", "pushed_at": None, "branches": {},
            "commits": []}
    base.update(kw)
    return base


def test_clean_repo():
    g, s, _ = judge(_info(pushed_at="2026-08-21T00:30:00Z"), DEADLINE)  # 09:30 KST
    assert g == "정상", s


def test_boundary_is_ten_oclock():
    """마감은 09:59:59다. 09:59:59는 세이프, 10:00:00은 아웃.

    경계를 한 칸 잘못 잡으면 정시에 낸 팀이 위반으로 찍힌다.
    """
    safe = judge(_info(pushed_at="2026-08-21T00:59:59Z"), DEADLINE)[0]
    assert safe == "정상", "09:59:59 KST는 마감 전"
    out = judge(_info(pushed_at="2026-08-21T01:00:00Z"), DEADLINE)[0]
    assert out == "확인필요", "10:00:00 KST는 마감 후"


def test_commits_after_deadline():
    g, s, lines = judge(_info(
        pushed_at="2026-08-21T05:00:00Z",
        commits=[{"sha": "abc12345", "branch": "main",
                  "authored": "2026-08-21T04:00:00Z",
                  "committed": "2026-08-21T04:00:00Z",
                  "author": "학생", "message": "버그 수정"}],
    ), DEADLINE)
    assert g == "위반"
    assert "1건" in s
    assert any("abc12345" in ln for ln in lines)


def test_pushed_after_but_no_commits():
    """푸시는 마감 후인데 마감 후 커밋이 없다 = force-push/브랜치 삭제 의심.

    이 분기가 조용히 '정상'으로 빠지면 이력을 다시 쓴 팀을 놓친다.
    """
    g, _, lines = judge(_info(pushed_at="2026-08-21T05:00:00Z"), DEADLINE)
    assert g == "확인필요"
    assert any("force-push" in ln for ln in lines)


def test_backdated_commit_is_flagged():
    """작성 시각은 마감 전, 커밋 시각은 마감 후 → 근거에 불일치를 남긴다.

    리베이스면 정상이지만 `git commit --date` 위조도 똑같이 보인다.
    기계가 판단할 수 없으니 표시만 하고 사람에게 넘긴다.
    """
    _, _, lines = judge(_info(
        pushed_at="2026-08-21T05:00:00Z",
        commits=[{"sha": "dead0001", "branch": "main",
                  "authored": "2026-08-20T01:00:00Z",   # 마감 전
                  "committed": "2026-08-21T04:00:00Z",  # 마감 후
                  "author": "학생", "message": "수정"}],
    ), DEADLINE)
    assert any("불일치" in ln for ln in lines), lines


def test_private_repo_flagged():
    """Public 유지가 제출 요건이라 비공개는 그 자체로 확인 대상이다."""
    g, s, _ = judge(_info(private=True, pushed_at="2026-08-01T00:00:00Z"), DEADLINE)
    assert g == "확인필요" and "비공개" in s


def test_unreachable_repo():
    g, s, _ = judge(_info(ok=False, err="접근 불가 (삭제·비공개·오타)"), DEADLINE)
    assert g == "오류" and "접근" in s


def test_missing_pushed_at_is_not_a_violation():
    """시각을 모르면 위반으로 몰지 않는다. 억울한 탈락보다 누락이 낫다."""
    assert judge(_info(pushed_at=None), DEADLINE)[0] == "정상"


# ── 시각 ─────────────────────────────────────────────────────────

def test_parse_time_defaults_to_kst():
    assert parse_time("2026-08-21 10:00").utcoffset().total_seconds() == 9 * 3600


def test_parse_time_respects_explicit_utc():
    """GitHub이 주는 Z 표기를 KST로 오해하면 9시간이 통째로 밀린다."""
    assert parse_time("2026-08-21T01:00:00Z") == DEADLINE


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"[OK ] {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"[FAIL] {name}: {e}")
    print(f"\n=== {len(tests) - len(failed)}/{len(tests)} 통과 ===")
    sys.exit(1 if failed else 0)
