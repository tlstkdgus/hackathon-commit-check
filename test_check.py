# -*- coding: utf-8 -*-
"""회귀 테스트. 네트워크를 타지 않는 부분만 검사한다.

    python test_check.py      (의존성 0)
    pytest test_check.py

입력 파싱(txt/csv/xlsx)과 판정 로직만 본다. API 호출은 테스트하지 않는다 —
깃허브 응답을 흉내 낸 딕셔너리를 judge()에 직접 넣는다.
"""

import datetime
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check import (KST, find_repo_urls, is_excluded, judge, load_repos,
                   mark_shared_repos, parse_repo_url, parse_time)

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


def test_url_rejects_slashy_text():
    """슬래시가 들어간 아무 문자열이나 레포로 읽으면 안 된다.

    엑셀 메모 칸의 "제출 마감 8/21 09:59:59"가 레포로 잡혀
    [오류] 행이 됐던 적이 있다. 600행짜리 표에서 이런 게 수십 건 나오면
    진짜 오류가 그 속에 묻힌다. 깃허브 이름은 영숫자와 . _ - 뿐이다.
    """
    for raw in ["제출 마감 8/21 09:59:59", "8/21", "확인 완료/미완료",
                "OO대/XX대 공동", "2026/08/21", "https://github.com/한글/레포"]:
        assert parse_repo_url(raw) is None, raw


def test_url_with_note_appended():
    """주소 뒤에 설명을 붙여 낸 팀 — `.../frontend(프론트엔드)`.

    실제 4회차 제출에서 나왔다. 괄호를 이름의 일부로 읽으면 그 팀은
    레포가 하나도 안 잡혀 검사에서 통째로 빠진다.
    """
    assert parse_repo_url(
        "https://github.com/0-SEAM/frontend(프론트엔드)") == ("0-SEAM", "frontend")


def test_repo_name_may_start_with_hyphen():
    """레포 이름은 계정과 규칙이 다르다 — `-MORU`처럼 하이픈으로 시작할 수 있다."""
    assert parse_repo_url(
        "https://github.com/seunghyeon-L/-MORU") == ("seunghyeon-L", "-MORU")
    # 계정은 하이픈으로 시작할 수 없다
    assert parse_repo_url("https://github.com/-owner/repo") is None


def test_two_repos_in_one_cell():
    """한 칸에 프론트·백엔드를 나란히 적은 팀. 뒤엣것이 사라지면 안 된다."""
    got = find_repo_urls("https://github.com/t/front https://github.com/t/back")
    assert [g[0] for g in got] == [("t", "front"), ("t", "back")]


def test_repo_after_deploy_url_in_same_cell():
    """`배포주소, 레포주소` 순으로 적으면 앞의 것만 보고 포기하면 안 된다."""
    got = find_repo_urls("https://campus.onrender.com/, https://github.com/p/campus.git")
    assert [g[0] for g in got] == [("p", "campus")]


def test_prose_without_github_stays_empty():
    """서술형 칸을 훑어도 없는 주소를 만들어내지 않는다."""
    assert find_repo_urls("우리 팀은 8/21 09:59에 제출했고 확인 완료/미완료") == []


def test_exclude_matches_whole_field_only():
    """제외는 칸 단위로 정확히 같을 때만. 부분 일치면 진짜 팀이 딸려 나간다.

    `--exclude 멋쟁이사자처럼`으로 운영 계정 테스트 제출을 뺐더니
    '충남대 멋쟁이사자처럼 3팀'까지 검사에서 사라진 적이 있다.
    """
    pat = ["멋쟁이사자처럼"]
    assert is_excluded("멋쟁이사자처럼 · 멋쟁이사자처럼", pat)
    assert not is_excluded("충남대학교 · 충남대 멋쟁이사자처럼 3팀", pat)
    assert not is_excluded("숙명여자대학교 · 금연한사자처럼", pat)
    # 못 읽은 행은 칸이 쉼표로 이어 붙어 온다
    assert is_excluded("멋쟁이사자처럼, 테스트, SJF Track, 테스트12", pat)
    assert not is_excluded("아무 팀", pat)
    assert not is_excluded("멋쟁이사자처럼 · 멋쟁이사자처럼", [])


def test_compare_404_no_common_ancestor_is_a_violation():
    """두 커밋이 다 살아 있는데 compare가 404 = 공통 조상이 없다.

    브랜치를 아예 다른 히스토리로 갈아치웠다는 뜻이다. 갈아치운 커밋의
    날짜가 마감 전이면 사후 검사에는 완벽히 정상으로 보인다.
    '대조 실패'로 뭉뚱그리면 가장 확실한 증거가 [확인필요]에 묻힌다.
    """
    import check as C
    real = C.api
    try:
        C.api = lambda path, params=None: ({}, {})   # 두 커밋 다 존재
        assert C._why_compare_failed("o", "r", "a" * 40, "b" * 40)[0] == "위반"

        def gone(path, params=None):
            raise C.NotFound(path)
        C.api = gone                                  # 스냅샷 커밋이 사라짐
        g, why = C._why_compare_failed("o", "r", "a" * 40, "b" * 40)
        assert g == "위반" and "사라" in why
    finally:
        C.api = real


def test_compare_404_stays_cautious_when_unsure():
    """조회 자체가 안 되면 단정하지 않는다. 억울한 위반보다 확인이 낫다."""
    import check as C
    real = C.api
    try:
        def boom(path, params=None):
            raise RuntimeError("network")
        C.api = boom
        assert C._why_compare_failed("o", "r", "a" * 40, "b" * 40)[0] == "확인필요"
    finally:
        C.api = real


def test_url_keeps_case():
    """깃허브는 대소문자를 보존한다. 임의로 소문자화하면 표시가 틀어진다."""
    assert parse_repo_url("https://github.com/MyTeam/MyProj") == ("MyTeam", "MyProj")


# ── 텍스트·CSV 입력 ──────────────────────────────────────────────

def _write(lines, suffix=".txt"):
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                     encoding="utf-8", newline="") as f:
        f.write("\n".join(lines) + "\n")
        return f.name


def test_load_repos():
    """팀명 + 여러 URL, 주석, 빈 줄, 중복 URL을 한 파일에서 처리한다."""
    path = _write([
        "# 주석",
        "",
        "팀하나, https://github.com/a/front, https://github.com/a/back",
        "https://github.com/b/solo",
        "한레포팀, https://github.com/c/mono, https://github.com/c/mono",
        "팀명,프론트엔드,백엔드",                    # 머리글 — 조용히 넘겨야 한다
        "깨진팀, https://github.com/owner만있음",    # 주소를 적으려다 실패 — 보고
    ])
    rows, bad = load_repos(path)
    got = [(t, o, n) for t, o, n, _ in rows]
    assert ("팀하나", "a", "front") in got, got
    assert ("팀하나", "a", "back") in got, got
    assert ("b/solo", "b", "solo") in got, "팀명이 없으면 owner/repo로 채운다"
    # 프론트·백엔드 칸에 같은 레포를 넣으라고 안내했으므로 중복이 정상 유입된다
    assert sum(1 for t, o, n in got if n == "mono") == 1, "중복 URL은 한 번만"
    assert len(bad) == 1, f"실패한 주소만 보고해야 한다: {bad}"
    assert "owner만있음" in bad[0][1]


def test_quoted_comma_in_team_name():
    """팀명에 쉼표가 들어가면 따옴표로 묶여 온다. split(',')로는 행이 깨진다."""
    path = _write(['"멋사, 서울", https://github.com/x/y'], ".csv")
    rows, _ = load_repos(path)
    assert rows and rows[0][0] == "멋사, 서울", rows


def test_header_row_is_silent():
    """600행짜리 엑셀의 머리글까지 경고하면 진짜 오류가 그 속에 묻힌다."""
    path = _write(["팀명,프론트,백엔드", "", "메모: 확인 요망"])
    rows, bad = load_repos(path)
    assert rows == [] and bad == [], (rows, bad)


# ── 엑셀 입력 ────────────────────────────────────────────────────

def _make_xlsx(path, rows, hyperlink=None):
    """openpyxl 없이 최소 xlsx를 만든다. 읽기 쪽을 진짜 파일로 검증하려고."""
    import zipfile
    from xml.sax.saxutils import escape

    shared, idx = [], {}
    for r in rows:
        for c in r:
            if c not in idx:
                idx[c] = len(shared)
                shared.append(c)

    body = []
    for ri, r in enumerate(rows, 1):
        cs = "".join(f'<c r="{chr(ord("A") + ci)}{ri}" t="s"><v>{idx[c]}</v></c>'
                     for ci, c in enumerate(r))
        body.append(f'<row r="{ri}">{cs}</row>')

    links = ""
    rels = ('<?xml version="1.0"?><Relationships xmlns='
            '"http://schemas.openxmlformats.org/package/2006/relationships">')
    if hyperlink:
        ref, target = hyperlink
        links = f'<hyperlinks><hyperlink ref="{ref}" r:id="rId9"/></hyperlinks>'
        rels += ('<Relationship Id="rId9" Type="http://schemas.openxmlformats.org'
                 '/officeDocument/2006/relationships/hyperlink" '
                 f'Target="{escape(target)}" TargetMode="External"/>')
    rels += "</Relationships>"

    sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats'
             '.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats'
             '.org/officeDocument/2006/relationships"><sheetData>'
             + "".join(body) + "</sheetData>" + links + "</worksheet>")
    ss = ('<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org'
          f'/spreadsheetml/2006/main" count="{len(shared)}" '
          f'uniqueCount="{len(shared)}">'
          + "".join(f"<si><t>{escape(x)}</t></si>" for x in shared) + "</sst>")

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"/>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships"/>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0"?><workbook xmlns="http://schemas.'
                   'openxmlformats.org/spreadsheetml/2006/main"/>')
        z.writestr("xl/sharedStrings.xml", ss)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        if hyperlink:
            z.writestr("xl/worksheets/_rels/sheet1.xml.rels", rels)
    return path


def test_xlsx_uses_header_to_find_team_column():
    """머리글에 '팀명'이 있으면 그 열을 쓴다.

    제출 플랫폼 표는 첫 열이 '대학명'이고 팀명은 네 번째다. 머리글을 안 보고
    '주소 아닌 첫 칸'을 쓰면 리포트가 전부 대학명으로 찍혀서, 같은 학교의
    여러 팀을 구분할 수 없다. 실제 파일에서 그렇게 나왔다.
    """
    path = tempfile.mktemp(suffix=".xlsx")
    _make_xlsx(path, [
        ["대학명", "서비스명", "트랙", "팀명", "GitHub BE", "GitHub FE"],
        ["OO대", "서비스가", "AAC", "알파팀",
         "https://github.com/a/back", "https://github.com/a/front"],
        ["OO대", "서비스나", "SJF", "베타팀", "https://github.com/b/only", ""],
    ])
    rows, bad = load_repos(path)
    got = [(t, o, n) for t, o, n, _ in rows]
    assert ("OO대 · 알파팀", "a", "back") in got, got
    assert ("OO대 · 알파팀", "a", "front") in got, got
    # 같은 대학의 다른 팀이 구분돼야 한다
    assert ("OO대 · 베타팀", "b", "only") in got, got
    assert bad == [], bad


def test_same_repo_from_different_teams_is_kept():
    """서로 다른 팀이 같은 레포를 냈으면 둘 다 남겨야 한다.

    전역 중복제거를 하면 뒤에 나온 팀이 검사 목록에서 조용히 사라진다.
    실제 파일에서 42개 제출이 7개로 줄어 29개 팀-레포가 증발했다.
    한 팀 안에서의 중복(프론트·백엔드에 같은 주소)은 그대로 걸러야 한다.
    """
    path = tempfile.mktemp(suffix=".xlsx")
    _make_xlsx(path, [
        ["대학명", "팀명", "GitHub BE", "GitHub FE"],
        ["OO대", "알파팀", "https://github.com/shared/repo",
         "https://github.com/shared/repo"],                    # 한 팀 안 중복
        ["XX대", "베타팀", "https://github.com/shared/repo", ""],  # 다른 팀, 같은 레포
    ])
    rows, _ = load_repos(path)
    got = [(t, o, n) for t, o, n, _ in rows]
    assert len(got) == 2, f"팀별로 하나씩 남아야 한다: {got}"
    assert ("OO대 · 알파팀", "shared", "repo") in got, got
    assert ("XX대 · 베타팀", "shared", "repo") in got, got


def test_xlsx_empty_cells_do_not_shift_columns():
    """빈 셀은 XML에 아예 없다. 순서대로 담으면 열이 밀린다.

    밀리면 머리글로 잡은 '팀명' 열이 엉뚱한 값을 가리키는데,
    리포트가 그럴듯해 보여서 틀린 줄도 모른다.
    """
    import zipfile
    from xml.sax.saxutils import escape
    path = tempfile.mktemp(suffix=".xlsx")
    # A1,B1,C1 머리글 / 2행은 B가 통째로 빠지고 A,C만 있다
    shared = ["대학명", "팀명", "레포", "OO대", "https://github.com/x/y"]
    sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.'
             'openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             '<row r="1"><c r="A1" t="s"><v>0</v></c>'
             '<c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
             '<row r="2"><c r="A2" t="s"><v>3</v></c>'
             '<c r="C2" t="s"><v>4</v></c></row>'   # B2 없음
             '</sheetData></worksheet>')
    ss = ('<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org'
          f'/spreadsheetml/2006/main" count="{len(shared)}">'
          + "".join(f"<si><t>{escape(x)}</t></si>" for x in shared) + "</sst>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/sharedStrings.xml", ss)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    rows, _ = load_repos(path)
    # 팀명(B열)이 비었으므로 대학명만 남아야 한다. 열이 밀리면
    # 주소가 팀명 자리로 들어와 라벨이 URL이 된다.
    assert rows and rows[0][0] == "OO대", rows
    assert (rows[0][1], rows[0][2]) == ("x", "y"), rows


def test_xlsx_reads_hyperlink_target():
    """셀엔 '레포 링크'만 보이고 실제 주소는 관계 파일에 있는 경우.

    구글시트에서 내보낸 xlsx가 이렇다. 셀 값만 읽으면 그 팀이 통째로 빠지는데,
    빠진 줄도 모른다 — 경고 없이 검사 대상에서 사라진다.
    """
    path = tempfile.mktemp(suffix=".xlsx")
    _make_xlsx(path, [["링크팀", "레포 링크"]],
               hyperlink=("B1", "https://github.com/hidden/repo"))
    rows, _ = load_repos(path)
    got = [(o, n) for _, o, n, _ in rows]
    assert ("hidden", "repo") in got, got


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


def test_many_commits_are_capped():
    """레포 하나가 커밋 200건을 쏟아내면 리포트를 못 읽는다."""
    many = [{"sha": f"sha{i:05d}", "branch": "main",
             "authored": "2026-08-21T04:00:00Z",
             "committed": f"2026-08-21T04:{i:02d}:00Z",
             "author": "학생", "message": f"작업 {i}"} for i in range(40)]
    g, s, lines = judge(_info(pushed_at="2026-08-21T05:00:00Z", commits=many),
                        DEADLINE)
    assert "40건" in s, "요약에는 전체 건수가 남아야 한다"
    assert len(lines) < 20, f"근거 줄이 잘려야 한다: {len(lines)}"
    assert any("외 30건" in ln for ln in lines), lines


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
    assert g == "비공개" and "비공개" in s


def test_private_gets_its_own_grade_not_error():
    """비공개와 오타는 운영진이 할 일이 다르다.

    오타는 팀에 주소를 다시 받으면 되지만 비공개는 규정 위반이다.
    한 칸에 섞어 놓으면 600행에서 골라내지 못한다.
    """
    g, s, lines = judge(
        _info(ok=False, maybe_private=True,
              err="비공개로 보임 (계정은 있는데 레포가 안 보임)"), DEADLINE)
    assert g == "비공개" and "비공개" in s
    assert any("Public" in ln for ln in lines)


def test_unreachable_repo():
    """계정마저 없으면 비공개가 아니라 주소가 틀린 것이다."""
    g, s, _ = judge(_info(ok=False, maybe_private=False,
                          err="접근 불가 (삭제·오타 — 계정도 안 보임)"), DEADLINE)
    assert g == "오류" and "접근" in s


def test_private_outranks_normal_but_not_violation():
    """정렬 순서 — 위반이 맨 위, 비공개는 오류보다 위, 정상은 맨 아래."""
    from check import RANK
    assert RANK["위반"] < RANK["확인필요"] < RANK["비공개"] < RANK["오류"] < RANK["정상"]


def test_missing_pushed_at_is_not_a_violation():
    """시각을 모르면 위반으로 몰지 않는다. 억울한 탈락보다 누락이 낫다."""
    assert judge(_info(pushed_at=None), DEADLINE)[0] == "정상"


# ── 팀 간 레포 중복 ──────────────────────────────────────────────

def test_shared_repo_across_teams_is_flagged():
    """다른 두 팀이 같은 레포를 내면 양쪽 다 확인 대상으로 올린다.

    한 팀이 프론트·백엔드에 같은 주소를 넣는 건 정상(한 레포로 개발)이라
    load_repos에서 이미 걸러진다. 여기 오는 중복은 팀이 다른 경우뿐이다.
    """
    results = [
        ("정상", "OO대 · 알파팀", "shared/repo", "마감 전 푸시", []),
        ("정상", "XX대 · 베타팀", "shared/repo", "마감 전 푸시", []),
        ("정상", "YY대 · 감마팀", "own/repo", "마감 전 푸시", []),
    ]
    out = mark_shared_repos(results)
    by_team = {t: (g, s, ln) for g, t, _, s, ln in out}

    g, summary, lines = by_team["OO대 · 알파팀"]
    assert g == "확인필요", "정상이어도 리포트에 올라와야 한다"
    assert "레포 중복" in summary, summary
    assert any("XX대 · 베타팀" in ln for ln in lines), lines

    g, _, lines = by_team["XX대 · 베타팀"]
    assert g == "확인필요"
    assert any("OO대 · 알파팀" in ln for ln in lines), "상대 팀 이름이 나와야 한다"

    assert by_team["YY대 · 감마팀"][0] == "정상", "겹치지 않는 팀은 그대로"


def test_shared_repo_keeps_worse_grade():
    """이미 위반인 항목을 중복 표시 때문에 등급이 내려가면 안 된다."""
    out = mark_shared_repos([
        ("위반", "A팀", "shared/repo", "마감 후 커밋 3건", ["증거"]),
        ("정상", "B팀", "shared/repo", "마감 전 푸시", []),
    ])
    grades = {t: g for g, t, _, _, _ in out}
    assert grades["A팀"] == "위반", grades
    assert grades["B팀"] == "확인필요", grades


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
