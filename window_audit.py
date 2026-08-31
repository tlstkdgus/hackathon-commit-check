# -*- coding: utf-8 -*-
"""구간별 커밋 감사 — 스냅샷이 아니라 **커밋 시각**으로 본다.

창 경계 스냅샷이 제때 안 찍혔을 때를 위한 것이다. 스냅샷 대조는 찍힌
시각에 기대지만 커밋 시각은 레포에 박혀 있어 나중에도 같은 답이 나온다.
(대신 force-push로 지워진 커밋은 이 방법으로 볼 수 없다.)

    python window_audit.py

구간:
  A 8/21 10:00 ~ 8/24 15:00  → 규정상 즉시 탈락
  B 8/24 15:00 ~ 8/25 00:00  → 복구만 허용된 창. 내용 심사 대상
  (8/25 이후는 행사가 끝나 문제 삼지 않는다)
"""
import datetime
import io
import json
import sys

import check

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

KST = check.KST
SRC = "제출물목록(상현님공유) (2).xlsx"
EXCLUDE = ["멋쟁이사자처럼"]

A_FROM = datetime.datetime(2026, 8, 21, 10, 0, 0, tzinfo=KST)
B_FROM = datetime.datetime(2026, 8, 24, 15, 0, 0, tzinfo=KST)
B_TO = datetime.datetime(2026, 8, 25, 0, 0, 0, tzinfo=KST)

MAX_BRANCHES = 60


def commits_in_window(owner, name):
    """A·B 구간에 걸친 커밋. (A리스트, B리스트). 접근 불가면 None."""
    try:
        repo, _ = check.api(f"/repos/{owner}/{name}")
    except check.NotFound:
        return None
    except Exception:
        return None
    pushed = check.parse_time(repo.get("pushed_at"))
    if not pushed or pushed < A_FROM:
        return [], []          # 창 전에 멈춘 레포 — 호출을 아낀다
    try:
        brs, _ = check.api_all(f"/repos/{owner}/{name}/branches")
    except Exception:
        return None
    names = sorted((b["name"] for b in brs),
                   key=lambda n: n != repo.get("default_branch"))[:MAX_BRANCHES]
    seen, a, b = set(), [], []
    for bn in names:
        try:
            got, _ = check.api_all(
                f"/repos/{owner}/{name}/commits",
                {"sha": bn, "since": check.api_time(A_FROM),
                 "until": check.api_time(B_TO)}, max_pages=3)
        except Exception:
            continue
        for c in got:
            if c["sha"] in seen:
                continue
            seen.add(c["sha"])
            cm = c.get("commit", {})
            when = check.parse_time(cm.get("committer", {}).get("date"))
            if not when:
                continue
            rec = {"sha": c["sha"][:8], "branch": bn, "when": when.isoformat(),
                   "author": (cm.get("author", {}).get("name")
                              or (c.get("author") or {}).get("login") or "?"),
                   "msg": (cm.get("message") or "").splitlines()[0][:70]}
            (b if when >= B_FROM else a).append(rec)
    return a, b


def main():
    check.TOKEN = check.get_token()
    rows, bad = check.load_repos(SRC)
    rows = [r for r in rows if not check.is_excluded(r[0], EXCLUDE)]
    print(f"검사 대상 {len(rows)}개 레포", file=sys.stderr)

    def work(row):
        team, owner, name, _ = row
        got = commits_in_window(owner, name)
        if got is None:
            return {"team": team, "repo": f"{owner}/{name}", "err": "접근 불가"}
        a, b = got
        return {"team": team, "repo": f"{owner}/{name}", "A": a, "B": b}

    out = check.run_concurrent(rows, work, 8, "감사")
    data = [o for o in out if isinstance(o, dict)]
    with io.open("구간감사.json", "w", encoding="utf-8") as f:
        json.dump({"A_from": A_FROM.isoformat(), "B_from": B_FROM.isoformat(),
                   "B_to": B_TO.isoformat(), "repos": data},
                  f, ensure_ascii=False, indent=1)
    nA = sum(1 for d in data if d.get("A"))
    nB = sum(1 for d in data if d.get("B"))
    print(f"\nA구간(탈락) 커밋이 있는 레포: {nA}")
    print(f"B구간(복구 창) 커밋이 있는 레포: {nB}")
    print("→ 구간감사.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
