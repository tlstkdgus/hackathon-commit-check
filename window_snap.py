# -*- coding: utf-8 -*-
"""수정 허용 창(8/24 15:00~24:00) 경계에서 스냅샷을 찍고 직전과 대조한다.

공지한 기준이 두 개다.

- 8/21 10:00 ~ 8/24 15:00 사이 커밋 → 즉시 탈락
- 8/24 24:00 이후 커밋·배포 → 즉시 탈락
- 그 사이(15:00~24:00)는 **복구만** 허용. 기능 추가·UI 변경은 안 된다.

그래서 창이 열리기 직전과 닫히는 순간의 상태를 찍어 두고, 두 스냅샷
사이의 차이를 사람이 읽는다. 무엇이 바뀌었는지가 곧 심사 자료가 된다.

    python window_snap.py --label 1458 --against snapshot-0821.json
    python window_snap.py --label 2400 --against 스냅샷-0824-1458.json

**창이 열리기 전 스냅샷은 15:00보다 앞서 끝나야 한다.** 8/21에 마감
10:00에 시작해 10:47에 찍는 바람에 47분이 통째로 사각지대가 됐다.
562개에 약 62초가 걸리므로 14:58에 시작하면 14:59에 끝난다.
"""
import argparse
import datetime
import io
import os
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = "제출물목록(상현님공유) (2).xlsx"
EXCLUDE = ["멋쟁이사자처럼"]
OUTDIR = os.path.join(HERE, "일일검사")


def say(text):
    try:
        print(text, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(OUTDIR, exist_ok=True)
        with io.open(os.path.join(OUTDIR, "window-log.txt"), "a",
                     encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def run(args, what):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, os.path.join(HERE, "check.py")] + args,
                       cwd=HERE, env=env, capture_output=True,
                       text=True, encoding="utf-8")
    # check.py는 위반이 있으면 1로 끝난다. 실패와 구분해야 한다.
    say(f"  {what}: exit {r.returncode}")
    if r.returncode not in (0, 1):
        say((r.stderr or "")[-1200:])
    return r


def main():
    p = argparse.ArgumentParser(description="창 경계 스냅샷 + 직전 대조")
    p.add_argument("--label", required=True,
                   help="스냅샷 이름표 (예: 1458, 2400)")
    p.add_argument("--against", help="대조할 직전 스냅샷 파일")
    p.add_argument("--deadline", help="대조 리포트의 기준 시각 (KST)")
    a = p.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    now = datetime.datetime.now()
    snap = os.path.join(HERE, f"스냅샷-{now:%m%d}-{a.label}.json")

    say("=" * 66)
    say(f"{now:%Y-%m-%d %H:%M:%S}  창 경계 스냅샷 [{a.label}]")

    args = [SRC, "--snapshot", snap]
    for x in EXCLUDE:
        args += ["--exclude", x]
    if a.deadline:
        args += ["--deadline", a.deadline]
    run(args, f"스냅샷 → {os.path.basename(snap)}")

    if a.against and os.path.exists(os.path.join(HERE, a.against)):
        base = os.path.join(OUTDIR, f"창대조-{now:%m%d}-{a.label}")
        args = ["--compare", a.against, "-o", base + ".md",
                "--csv", base + ".csv", "--json", base + ".json"]
        for x in EXCLUDE:
            args += ["--exclude", x]
        if a.deadline:
            args += ["--deadline", a.deadline]
        run(args, f"대조 {a.against} → {os.path.basename(base)}.md")
    elif a.against:
        say(f"  [경고] 대조 대상 {a.against} 이 없습니다 — 대조를 건너뜁니다")

    say(f"  끝 {datetime.datetime.now():%H:%M:%S}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
