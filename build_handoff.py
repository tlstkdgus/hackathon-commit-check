# -*- coding: utf-8 -*-
"""검사 결과 → 운영진 핸드오프 문서.

입력은 result-사후.json이다. CSV는 근거를 ' / '로 이어 붙여 되읽으면
근거 줄 자체의 '/'에서 깨진다.

리포트(report-사후.md)는 레포 단위라 600행에서 팀이 흩어진다. 운영진이
실제로 하는 일은 **팀에 연락하고 판정하는 것**이라 팀 단위로 다시 묶고,
연락에 필요한 정보(서비스명·팀원·제출 페이지)를 제출표에서 붙인다.

    python build_handoff.py > 핸드오프-4회차.md
"""
import csv
import datetime
import json
import sys
from collections import defaultdict

import check

SRC = "제출물목록(상현님공유) (2).xlsx"
RES = "result-사후.json"
CSVOUT = "result-사후.csv"
SNAP = "snapshot-0821.json"
EXCLUDE = ["멋쟁이사자처럼"]

out = []
w = out.append


gone, nearmiss = [], []


def meta_table():
    """제출표에서 팀별 연락 정보. 레포가 없는 행도 함께 잡아 둔다."""
    meta, norepo = {}, []
    for ln, cells in check._source_rows(SRC):
        if ln == 1 or len(cells) < 13:
            continue
        g = lambda i: cells[i].strip() if i < len(cells) else ""
        label = " · ".join(x for x in (g(0), g(3)) if x)
        if not label:
            continue
        if check.is_excluded(label, EXCLUDE):
            gone.append((ln, label, check.find_repo_urls(" ".join(cells))))
            continue
        if any(x in label for x in EXCLUDE):
            # 이름이 겹칠 뿐 정확히 같지 않아 남은 팀. 부분 일치로 걸렀다면
            # 사라졌을 팀이라, 제외가 과했는지 문서에서 눈으로 확인하게 남긴다
            nearmiss.append(label)
        meta[label] = {"서비스": g(1), "트랙": g(2), "팀원": g(4), "페이지": g(12)}
        if not check.find_repo_urls(" ".join(cells)):
            norepo.append((ln, label, meta[label]))
    return meta, norepo


meta, norepo = meta_table()
res = json.load(open(RES, encoding="utf-8"))
rows = [{"등급": r["grade"], "팀": r["team"], "레포": r["repo"],
         "요약": r["summary"], "근거": r["lines"]} for r in res["results"]]
snap = json.load(open(SNAP, encoding="utf-8"))
by_team = defaultdict(list)
for r in rows:
    by_team[r["팀"]].append(r)

teams = lambda g: sorted({r["팀"] for r in rows if r["등급"] == g})
viol, warn, priv = teams("위반"), teams("확인필요"), teams("비공개")
n_branch = sum(len(s.get("branches") or {}) for s in snap["repos"])


def head(t):
    m = meta.get(t, {})
    w(f"### {t}")
    w("")
    w(f"- **서비스** {m.get('서비스','?')} · {m.get('트랙','?')}")
    w(f"- **팀원** {m.get('팀원','?')}")
    w(f"- **제출 페이지** {m.get('페이지','?')}")
    w("")


w("# 4회차 마감 후 커밋 검사 — 운영진 핸드오프")
w("")
w(f"- **마감** 2026-08-21 09:59:59 KST (10:00:00부터 마감 후)")
w(f"- **검사 대상** {len(rows)}개 레포 / {len({r['팀'] for r in rows})}팀")
w(f"- **스냅샷** {snap['taken_at'][:19].replace('T',' ')} · 브랜치 {n_branch:,}개 기록")
w(f"- **작성** {datetime.datetime.now(check.KST):%Y-%m-%d %H:%M} KST")
w("")
w("| 등급 | 건수 | 뜻 |")
w("|---|---|---|")
w(f"| 위반 | {len(viol)}팀 | 마감 후 커밋이 실제로 확인됨 |")
w(f"| 확인 | {len(warn)}팀 | 마감 후 푸시는 있는데 커밋이 안 잡힘 |")
w(f"| 비공개 | {len(priv)}팀 | 레포가 비공개라 검사 자체가 불가능 |")
w(f"| 미제출 | {len(norepo)}팀 | 레포 주소 칸이 비어 있음 |")
w(f"| 정상 | {sum(1 for r in rows if r['등급']=='정상')}건 | 마감 후 변경 없음 |")
w("")
w("> 이 문서는 **근거만 제시**한다. 탈락 여부는 운영진이 판단한다.")
w("> 리베이스·머지처럼 악의 없는 작업으로도 커밋 시각이 밀리고,")
w("> 위반으로 찍힌 것이 README 오타 수정일 수도 있다.")
w("")
w("---")
w("")
w("## A. 지금 연락해야 하는 팀")
w("")
w("비공개 레포는 **스냅샷도 비어 있다.** 나중에 공개로 바꿔도 대조할 과거")
w("상태가 없어서, 늦게 연락할수록 검증 가능성이 영영 사라진다.")
w("")
# 가나다순으로 두면 최우선 팀이 가운데 묻힌다. 공개 레포가 하나도 없는
# 팀 — 아무것도 검증할 수 없는 팀 — 을 맨 위로 올린다.
priv.sort(key=lambda t: (any(r["등급"] == "정상" for r in by_team[t]), t))

for t in priv:
    pub = [r for r in by_team[t] if r["등급"] == "정상"]
    hidden = [r for r in by_team[t] if r["등급"] == "비공개"]
    head(t)
    if not pub:
        w("**공개 레포가 하나도 없다 — 아무것도 검증할 수 없다. 최우선.**")
    else:
        w(f"공개 레포 {len(pub)}개는 검사됐고, 아래 {len(hidden)}개만 못 본다.")
    w("")
    for r in hidden:
        w(f"- 🔒 `{r['레포']}` — {r['요약']}")
    for r in pub:
        w(f"- ✅ `{r['레포']}` — {r['요약']}")
    w("")
w("---")
w("")
w("## B. 판정이 필요한 팀 — 마감 후 커밋")
w("")
for t in viol:
    head(t)
    for r in by_team[t]:
        if r["등급"] == "정상":
            w(f"- ✅ `{r['레포']}`")
            continue
        w(f"- ⚠️ `{r['레포']}` — **{r['요약']}**")
        for ln in r["근거"]:
            w(f"    - {ln}")
    w("")
w("---")
w("")
w("## C. 확인이 필요한 팀")
w("")
for t in warn:
    head(t)
    for r in by_team[t]:
        if r["등급"] == "정상":
            continue
        w(f"- ❓ `{r['레포']}` — **{r['요약']}**")
        for ln in r["근거"]:
            w(f"    - {ln}")
    w("")
w("---")
w("")
w("## D. 레포를 내지 않은 팀")
w("")
w("제출 페이지의 레포 링크 칸이 `https://`로만 채워져 있다.")
w("제출은 했으니 주소만 못 받은 것일 수 있다.")
w("")
for ln, label, m in norepo:
    w(f"### {label}  *(표 {ln}행)*")
    w("")
    w(f"- **서비스** {m['서비스']} · {m['트랙']}")
    w(f"- **팀원** {m['팀원']}")
    w(f"- **제출 페이지** {m['페이지']}")
    w("")
w("---")
w("")
w("## E. 검사 범위와 한계")
w("")
w("**본 것**")
w("")
w(f"- 제출표의 모든 칸에서 깃허브 주소를 찾아 {len(rows)}개 레포")
w("- 각 레포의 **모든 브랜치** — 평가 기준은 main이지만 다른 브랜치에")
w("  올린 것도 레포 수정이다")
w("- 마감(10:00:00) 이후의 커밋 시각, 그리고 작성 시각과 커밋 시각의 불일치")
w("")
w("**못 본 것**")
w("")
w("- **비공개 레포 안쪽** — 남에게 404로 보인다. 계정이 실제로 있는지를")
w("  한 번 더 물어 비공개와 주소 오타를 갈라내지만 확실하지 않다.")
w("  레포 이름만 오타를 냈어도 `비공개`로 찍힌다.")
w("- **마감 ~ 스냅샷 사이(10:00:00 ~ 10:47:47)의 force-push** — 그 구간에")
w("  히스토리를 다시 쓴 팀은 이미 정리된 상태로 스냅샷에 들어갔다.")
w("  사후 검사가 남아 있는 커밋의 시각은 보지만, 지워진 커밋은 복원할 수 없다.")
w("- 브랜치가 30개를 넘으면 앞의 30개만 본다. 이번에 잘린 레포는 1개였고")
w("  (`14thlikelion-centralthon-mju2team/FE`, 브랜치 74개)")
w("  **74개 전수로 다시 확인했으나 결과는 같았다.**")
w("")
w("**앞으로 잡히는 것** — 스냅샷을 찍어뒀으므로 지금부터 심사 기간 중의")
w("force-push, 브랜치 삭제, Public → Private 전환은 대조로 잡힌다.")
w("")
w("---")
w("")
w("## F. 재검사")
w("")
w("심사 기간 중 아무 때나, 여러 번 돌려도 된다.")
w("")
w("```bash")
w(f"python check.py --compare {SNAP} --exclude {' --exclude '.join(EXCLUDE)}"
  f" --csv 재검사.csv --json 재검사.json -o 재검사.md")
w("```")
w("")
w(f"**`{SNAP}`을 지우면 force-push 검사가 통째로 불가능해진다.**")
w("")
w("---")
w("")
w("## G. 검사에서 뺀 행")
w("")
w(f"`--exclude {' '.join(EXCLUDE)}` — 대학명·팀명이 **통째로 같은** 행만 뺐다.")
w(f"운영 계정의 테스트 제출 {len(gone)}건이다.")
w("")
for ln, label, repos in gone:
    what = ", ".join(f"`{o}/{n}`" for (o, n), _ in repos) or "레포 없음"
    w(f"- `{label}` (표 {ln}행 — {what})")
w("")
if nearmiss:
    w("이름이 겹치지만 **뺀 것이 아닌** 팀 — 부분 일치로 걸렀다면 조용히")
    w("사라졌을 팀이다. 정상 검사됐는지 확인하려면 여기를 본다.")
    w("")
    for label in nearmiss:
        got = [r for r in rows if r["팀"] == label]
        for r in got:
            w(f"- `{label}` → [{r['등급']}] `{r['레포']}`")
w("")
w("---")
w("")
w("## H. 파일")
w("")
w("| 파일 | 내용 |")
w("|---|---|")
w(f"| `{SNAP}` | 10:47 스냅샷 — 브랜치 {n_branch:,}개 SHA. **지우면 안 됨** |")
w("| `report-사후.md` | 레포 단위 리포트 |")
w(f"| `{CSVOUT}` | 전체 {len(rows)}행 — 스프레드시트에서 정렬·분담 |")
w(f"| `{RES}` | 같은 내용, 구조 보존 (이 문서의 입력) |")
w("| `핸드오프-4회차.md` | 이 문서 — `python build_handoff.py`로 다시 만든다 |")
w("")
w("모두 학생 팀명·레포 주소가 들어가 `.gitignore` 대상이다.")
w("")
print("\n".join(out))
