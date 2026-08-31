# -*- coding: utf-8 -*-
"""매일 아침 9시에 도는 정기 검사.

심사 기간 내내 같은 스냅샷과 대조한다. 매일 같은 숫자를 다시 읽는 건
의미가 없으므로 **전날 결과와 달라진 것만** 앞에 세운다.

    python daily_check.py

결과는 `일일검사/` 아래에 날짜별로 쌓이고, `일일검사/log.txt`에 한 줄씩
요약이 붙는다. 변화가 있으면 종료 코드 1 — 작업 스케줄러에서 마지막
실행 결과로 걸러 볼 수 있다.
"""
import datetime
import glob
import io
import json
import os
import subprocess
import sys

# 작업 스케줄러에서 돌 때는 콘솔이 없고, 있어도 cp949라 한글을 찍다가
# UnicodeEncodeError로 죽는다. 여기서 못 박아 둔다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "일일검사")
SRC = "제출물목록(상현님공유) (2).xlsx"
# 기준 스냅샷은 고정하지 않는다. 창(8/24 15:00~24:00) 경계에서 새로
# 찍으면 그때부터는 그쪽이 기준이 되어야 한다 — 24:00 이후 변경을 보는데
# 8/21 스냅샷과 대면 창 안의 허용된 복구까지 전부 위반으로 올라온다.
FALLBACK_SNAPSHOT = "snapshot-0821.json"
EXCLUDE = ["멋쟁이사자처럼"]
LOG = os.path.join(OUTDIR, "log.txt")


def newest_snapshot():
    """가장 최근 스냅샷. 창 경계에서 찍은 것이 있으면 그쪽을 쓴다."""
    cands = glob.glob(os.path.join(HERE, "스냅샷-*.json"))
    cands += glob.glob(os.path.join(HERE, "snapshot-*.json"))
    cands = [c for c in cands if os.path.isfile(c)]
    if not cands:
        return os.path.join(HERE, FALLBACK_SNAPSHOT)
    return max(cands, key=os.path.getmtime)


def scan_after(deadline):
    """시각 기준 스캔. 창이 닫힌 뒤의 커밋은 스냅샷 대조와 별개로
    **커밋 시각만으로도** 잡힌다. 대조는 브랜치가 움직여야 보이지만
    이쪽은 새 브랜치에 몰래 올린 것도 시각으로 걸러낸다.
    """
    stamp = datetime.datetime.now().strftime("%m%d-%H%M")
    base = os.path.join(OUTDIR, f"창후스캔-{stamp}")
    cmd = [sys.executable, os.path.join(HERE, "check.py"), SRC,
           "--deadline", deadline, "--json", base + ".json",
           "--csv", base + ".csv", "-o", base + ".md"]
    for x in EXCLUDE:
        cmd += ["--exclude", x]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    subprocess.run(cmd, cwd=HERE, env=env, capture_output=True,
                   text=True, encoding="utf-8")
    if not os.path.exists(base + ".json"):
        say(f"  [창후 스캔] 실행 실패")
        return 0
    res = json.load(io.open(base + ".json", encoding="utf-8"))["results"]
    bad = [r for r in res if r["grade"] == "위반"]
    say(f"  [창후 스캔] 기준 {deadline} 이후 커밋이 있는 레포: {len(bad)}개")
    for r in bad:
        say(f"     {r['team']}  {r['repo']}  {r['summary']}")
        for ln in r["lines"][1:4]:
            say(f"        {ln[:100]}")
    return len(bad)


def previous_json(before):
    """직전 실행의 결과 파일. 없으면 None."""
    found = sorted(glob.glob(os.path.join(OUTDIR, "재검사-*.json")))
    found = [f for f in found if os.path.basename(f) != os.path.basename(before)]
    return found[-1] if found else None


def load(path):
    with io.open(path, encoding="utf-8") as f:
        return {r["repo"]: r for r in json.load(f)["results"]}


def run():
    os.makedirs(OUTDIR, exist_ok=True)
    # last-run.txt는 '직전 실행'만 담는다. 계속 붙이면 어디까지가
    # 오늘 것인지 알 수 없다. 누적 기록은 log.txt가 맡는다.
    try:
        os.remove(os.path.join(OUTDIR, "last-run.txt"))
    except OSError:
        pass
    now = datetime.datetime.now()
    stamp = now.strftime("%m%d-%H%M")
    base = os.path.join(OUTDIR, f"재검사-{stamp}")

    snapshot = newest_snapshot()
    cmd = [sys.executable, os.path.join(HERE, "check.py"),
           "--compare", snapshot,
           "--csv", base + ".csv", "--json", base + ".json",
           "-o", base + ".md"]
    for x in EXCLUDE:
        cmd += ["--exclude", x]

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run(cmd, cwd=HERE, env=env,
                       capture_output=True, text=True, encoding="utf-8")

    # check.py는 위반이 있으면 1로 끝난다. 실행 자체가 실패한 것과
    # 구분해야 한다 — 파일이 안 나왔으면 진짜 실패다.
    if not os.path.exists(base + ".json"):
        line = f"{now:%Y-%m-%d %H:%M}  실행 실패 (exit {r.returncode})"
        say(line)
        say((r.stderr or "").strip()[-1500:])
        log(line)
        return 2

    now_res = load(base + ".json")
    counts = {}
    for v in now_res.values():
        counts[v["grade"]] = counts.get(v["grade"], 0) + 1
    summary = " ".join(f"{g} {counts[g]}" for g in
                       ("위반", "확인필요", "비공개", "오류", "정상")
                       if g in counts)

    prev_path = previous_json(base + ".json")
    say("=" * 64)
    say(f"{now:%Y-%m-%d %H:%M}  스냅샷 대조 — {len(now_res)}개 레포")
    say(f"  기준 스냅샷: {os.path.basename(snapshot)}")
    say(f"  {summary}")

    # 스냅샷 이후 브랜치가 실제로 바뀐 레포 — 이게 이 검사의 본론이다
    moved = [v for v in now_res.values()
             if any(k in ln for ln in v["lines"]
                    for k in ("추가", "이력 변경", "삭제", "새 브랜치"))]
    say(f"  스냅샷 이후 브랜치가 바뀐 레포: {len(moved)}개")
    for v in moved:
        say(f"     [{v['grade']}] {v['team']}  {v['repo']}  {v['summary']}")

    changed = 0
    if prev_path:
        prev = load(prev_path)
        say(f"  직전 실행({os.path.basename(prev_path)}) 대비:")
        for repo, v in sorted(now_res.items()):
            old = prev.get(repo)
            if old is None:
                say(f"     + 새 레포 {repo} [{v['grade']}]")
                changed += 1
            elif old["grade"] != v["grade"]:
                say(f"     ~ {v['team']}  {repo}: "
                    f"{old['grade']} → {v['grade']}  ({v['summary']})")
                changed += 1
        for repo in sorted(set(prev) - set(now_res)):
            say(f"     - 빠진 레포 {repo}")
            changed += 1
        if not changed:
            say("     변화 없음")
    else:
        say("  (첫 실행 — 비교 대상 없음)")

    after = 0
    if SCAN_DEADLINE:
        after = scan_after(SCAN_DEADLINE)

    log(f"{now:%Y-%m-%d %H:%M}  {summary}  "
        f"브랜치변경 {len(moved)}  전일대비 {changed}"
        + (f"  창후커밋 {after}" if SCAN_DEADLINE else ""))
    return 1 if (moved or changed or after) else 0


def say(text):
    """화면과 기록 양쪽에. 스케줄러로 돌면 화면이 없어서 파일만 남는다."""
    try:
        print(text, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(OUTDIR, exist_ok=True)
        with io.open(os.path.join(OUTDIR, "last-run.txt"), "a",
                     encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def log(line):
    os.makedirs(OUTDIR, exist_ok=True)
    with io.open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


SCAN_DEADLINE = None

if __name__ == "__main__":
    # --scan-after "2026-08-25 00:00" 을 주면 시각 기준 스캔도 함께 한다.
    if "--scan-after" in sys.argv:
        SCAN_DEADLINE = sys.argv[sys.argv.index("--scan-after") + 1]
    sys.exit(run())
