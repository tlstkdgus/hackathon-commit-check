# -*- coding: utf-8 -*-
"""진출 45팀 최종 판정 자료.

구간감사.json(커밋 시각)과 스냅샷(실제 변경 시점)을 **둘 다** 본다.
어느 하나만으로는 틀린다.

- 커밋 시각만 보면: 대회가 끝난 뒤 올린 로컬 작업이 창 안 커밋으로 잡힌다.
  실제로 한 팀이 8/22 날짜 커밋을 8/26에 푸시해 위반으로 몰릴 뻔했다.
- 스냅샷만 보면: 창 열림 전 스냅샷이 16:01에 찍혀(예정 14:58) 15:00~16:01
  구간이 기준선에 이미 들어가 있다. 그 사이 변경은 대조로 안 잡힌다.

    python build_verdict.py > 심사/최종판정-45팀.md
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
A_FROM = datetime.datetime(2026, 8, 21, 10, 0, tzinfo=KST)
B_FROM = datetime.datetime(2026, 8, 24, 15, 0, tzinfo=KST)
B_TO = datetime.datetime(2026, 8, 25, 0, 0, tzinfo=KST)

out = []
w = out.append


def load_adv():
    adv = []
    for l in io.open("심사/진출45팀.txt", encoding="utf-8").read().splitlines():
        if not l.strip():
            continue
        p = l.split("|")
        adv.append({"track": p[0], "univ": p[-1].strip(), "team": p[-2].strip(),
                    "svc": "|".join(p[1:-2]).strip()})
    return adv


def main():
    adv = load_adv()
    audit = json.load(io.open("구간감사.json", encoding="utf-8"))
    s21 = {(r["owner"], r["name"]): r
           for r in json.load(io.open("snapshot-0821.json", encoding="utf-8"))["repos"]}
    s24 = {(r["owner"], r["name"]): r
           for r in json.load(io.open("스냅샷-0824-1458.json", encoding="utf-8"))["repos"]}

    # 제출표에서 연락 정보
    meta = {}
    for ln, cells in check._source_rows("제출물목록(상현님공유) (2).xlsx"):
        if ln == 1 or len(cells) < 5:
            continue
        g = lambda i: cells[i].strip() if i < len(cells) else ""
        meta[f"{g(0)} · {g(3)}"] = {"팀원": g(4), "페이지": g(12)}

    by = {}
    for r in audit["repos"]:
        by.setdefault(r["team"], []).append(r)

    def changed_during_contest(repo):
        """스냅샷이 대회 기간 중 실제 변경을 증명하는가."""
        owner, name = repo.split("/")
        a = (s21.get((owner, name)) or {}).get("branches") or {}
        b = (s24.get((owner, name)) or {}).get("branches") or {}
        return a != b

    f = lambda s: check.fmt(check.parse_time(s))

    w("# 진출 45팀 최종 판정 자료")
    w("")
    w(f"- **작성** {datetime.datetime.now(KST):%Y-%m-%d %H:%M} KST")
    w("- **A구간** 8/21 10:00 ~ 8/24 15:00 — 규정상 커밋 발견 시 즉시 탈락")
    w("- **B구간** 8/24 15:00 ~ 24:00 — 복구만 허용. 기능 추가·UI 변경 금지")
    w("- 8/25 이후는 행사가 끝나 문제 삼지 않는다")
    w("")
    w("> 이 문서는 **근거만 제시**한다. 탈락·감점은 운영진이 판단한다.")
    w("")
    w("## 판정에 두 가지를 함께 쓴 이유")
    w("")
    w("**커밋 시각만 보면 틀린다.** 대회가 끝난 뒤 올린 로컬 작업이 창 안 커밋으로")
    w("잡힌다. 실제로 한 팀이 8/22 날짜 커밋을 **8/26에** 푸시해 위반으로 몰릴")
    w("뻔했다. 그래서 스냅샷으로 *그 시점에 레포가 실제로 변했는지* 를 함께 본다.")
    w("")
    w("**스냅샷만 보면 빠진다.** 창 열림 전 스냅샷이 예정(14:58)보다 늦은 **16:01**에")
    w("찍혀 15:00~16:01 구간이 기준선에 이미 들어가 있고, 창 닫힘(24:00) 스냅샷은")
    w("PC 종료로 **찍히지 못했다**. 그 구간은 커밋 시각으로만 볼 수 있다.")
    w("")
    w("**남은 사각지대** — 창 안에서 force-push로 지운 커밋은 두 방법 모두 볼 수 없다.")
    w("")
    w("---")
    w("")

    flagged = []
    for a in adv:
        lb = f'{a["univ"]} · {a["team"]}'
        repos = by.get(lb, [])
        A = [(r, c) for r in repos for c in r.get("A") or []]
        B = [(r, c) for r in repos for c in r.get("B") or []]
        errs = [r for r in repos if r.get("err")]
        if A or B or errs:
            flagged.append((a, lb, repos, A, B, errs))

    w(f"## 확인이 필요한 팀 — {len(flagged)}팀 / 45팀")
    w("")
    w(f"나머지 **{45 - len(flagged)}팀은 A·B 구간 모두 활동이 없다.**")
    w("")

    for a, lb, repos, A, B, errs in flagged:
        w(f'### [{a["track"]}] {a["svc"]} — {a["team"]} ({a["univ"]})')
        w("")
        m = meta.get(lb, {})
        w(f'- **팀원** {m.get("팀원","?")}')
        if m.get("페이지"):
            w(f'- **제출 페이지** {m["페이지"]}')
        w("")
        for r in repos:
            if r.get("err"):
                w(f'- 🔒 `{r["repo"]}` — {r["err"]} (검증 불가)')
        for r in repos:
            ra = r.get("A") or []
            if ra:
                proof = ("스냅샷도 대회 기간 중 변경을 확인"
                         if changed_during_contest(r["repo"])
                         else "⚠️ **스냅샷은 그대로 — 대회 후 푸시로 보임**")
                w(f'- **A구간** `{r["repo"]}` {len(ra)}건 — {proof}')
                for c in sorted(ra, key=lambda x: x["when"]):
                    w(f'    - `{f(c["when"])}` [{c["branch"]}] {c["msg"]}')
        for r in repos:
            rb = r.get("B") or []
            if rb:
                w(f'- **B구간(복구 창)** `{r["repo"]}` {len(rb)}건')
                for c in sorted(rb, key=lambda x: x["when"]):
                    w(f'    - `{f(c["when"])}` [{c["branch"]}] {c["msg"]}')
        w("")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
