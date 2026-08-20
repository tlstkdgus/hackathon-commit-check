# -*- coding: utf-8 -*-
"""마감 후 커밋 검사 — 제출된 깃허브 레포에 마감 시각 이후 커밋이 있는지 본다.

의존성 0. 토큰은 `GITHUB_TOKEN` 환경변수 또는 `gh auth token`에서 가져온다.

    python check.py repos.txt --snapshot s.json   # 마감 직후 스냅샷 저장
    python check.py --compare s.json              # 스냅샷과 대조 (force-push까지 잡음)
    python check.py repos.txt                     # 스냅샷 없이 사후 검사

repos.txt 형식 — 빈 줄과 `#` 주석은 무시:
    https://github.com/owner/repo
    팀이름, https://github.com/o/front, https://github.com/o/back

왜 스냅샷이 필요한가:
    사후 검사만으로는 `git push --force`를 못 잡는다. 히스토리를 다시 쓰면
    마감 후 커밋이 사라지고 남은 커밋은 전부 마감 전으로 보인다.
    커밋 날짜도 `git commit --date`로 위조된다.
    마감 직후에 브랜치 SHA를 찍어두면 이 둘이 모두 드러난다.

600개 규모에서 달라지는 것:
    - 순차로 돌면 20분이 넘어 "10시 스냅샷"이 10시 20분 스냅샷이 된다.
      그 사이 푸시한 팀은 이미 수정된 상태로 찍혀 대조로는 영영 안 잡힌다.
      그래서 동시 실행(--workers)으로 시간을 줄이고, 스냅샷 시점의
      pushed_at을 함께 저장해 그 틈에 들어온 푸시를 따로 잡아낸다.
    - 중간에 죽으면 처음부터 다시 돌 여유가 없다. 50개마다 중간 저장하고
      --resume으로 이어받는다.
    - 레이트리밋(토큰 있을 때 시간당 5000회)에 걸릴 수 있으니 남은 횟수를
      리포트에 찍는다. pushed_at으로 먼저 걸러서 호출을 아낀다.
"""

import argparse
import csv
import datetime
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://api.github.com"

# KST는 서머타임이 없어 UTC+9 고정. zoneinfo를 쓰면 윈도우에서 tzdata가
# 필요해지는데, 고정 오프셋이면 같은 결과를 의존성 0으로 얻는다.
KST = datetime.timezone(datetime.timedelta(hours=9), "KST")
UTC = datetime.timezone.utc

# 결과물 제출 마감은 8/21(금) 09:59:59 → 10:00:00부터가 마감 후.
DEFAULT_DEADLINE = "2026-08-21 10:00"

# 동시 실행 수. 너무 올리면 2차 레이트리밋(남용 감지)에 걸려 오히려 느려진다.
DEFAULT_WORKERS = 8

# 레포 하나가 마감 후 커밋 200건을 쏟아내면 리포트를 못 읽는다.
MAX_COMMIT_LINES = 10


# ── 시각 ─────────────────────────────────────────────────────────

def parse_time(text, default_tz=KST):
    """`2026-08-21 10:00` / `...T01:00:00Z` → aware datetime.

    시간대를 안 적으면 KST로 본다. naive datetime이 하나라도 섞이면
    비교할 때 TypeError가 나므로 시각은 전부 이 함수를 거친다.
    """
    if not text:
        return None
    s = text.strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=default_tz)


def api_time(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt(dt):
    return dt.astimezone(KST).strftime("%m/%d %H:%M:%S") if dt else "-"


# ── 깃허브 API ───────────────────────────────────────────────────

class NotFound(Exception):
    pass


def get_token():
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var, "").strip():
            return os.environ[var].strip()
    try:  # gh CLI가 로그인돼 있으면 그 토큰을 빌린다
        r = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


TOKEN = None

# 레이트리밋에 걸리면 모든 워커가 함께 멈춰야 한다. 한 스레드만 자고
# 나머지가 계속 때리면 2차 제한까지 걸려서 더 오래 막힌다.
_gate = threading.Event()
_gate.set()
_gate_lock = threading.Lock()
_last_remaining = [None]


def _pause(seconds, why):
    """모든 워커를 함께 재운다. 이미 다른 스레드가 재우고 있으면 편승한다."""
    seconds = max(1, min(int(seconds), 900))
    if not _gate_lock.acquire(blocking=False):
        _gate.wait(timeout=seconds + 5)
        return
    try:
        _gate.clear()
        print(f"    [대기] {why} — {seconds}초", file=sys.stderr, flush=True)
        time.sleep(seconds)
    finally:
        _gate.set()
        _gate_lock.release()


def api(path, params=None):
    """GitHub REST 호출. 페이지네이션은 호출부에서 처리한다."""
    # 경로에 한글·공백이 들어오면 urlopen이 ascii로 인코딩하려다 죽는다.
    # 오타난 레포 주소 하나가 검사 전체를 멈추면 안 된다.
    url = API + urllib.parse.quote(path, safe="/")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hackathon-commit-check",
        **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
    })
    for attempt in range(4):
        _gate.wait()
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                rem = res.headers.get("X-RateLimit-Remaining")
                if rem is not None:
                    _last_remaining[0] = int(rem)
                return json.loads(res.read().decode("utf-8")), dict(res.headers)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(path)
            if e.code in (403, 429):
                # Retry-After는 2차 레이트리밋(동시 요청 과다)일 때 온다.
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    _pause(int(retry_after), "2차 레이트리밋")
                    continue
                reset = e.headers.get("X-RateLimit-Reset")
                if reset and e.headers.get("X-RateLimit-Remaining") == "0":
                    _pause(int(reset) - int(time.time()) + 2, "레이트리밋 소진")
                    continue
                _pause(60, f"403 (권한/남용 감지) {path}")
                continue
            if e.code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            # 600개를 도는 동안 네트워크가 한 번 흔들리는 건 정상이다.
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError(f"4회 실패: {path}")


def api_all(path, params=None):
    """페이지를 끝까지 따라가며 합친다."""
    out, page = [], 1
    while True:
        data, _ = api(path, {**(params or {}), "per_page": 100, "page": page})
        if not isinstance(data, list) or not data:
            return out
        out.extend(data)
        if len(data) < 100:
            return out
        page += 1
        if page > 20:  # 브랜치 2000개는 비정상 — 무한루프 방지
            return out


# ── 입력 ─────────────────────────────────────────────────────────

def parse_repo_url(raw):
    """어떤 형태로 적혀 오든 (owner, name)으로. 못 읽으면 None.

    받는 형태: https://github.com/o/r[.git][/tree/main][?x=1], owner/repo,
              git@github.com:o/r.git, www./http/대문자 섞인 것
    """
    s = raw.strip().strip("<>\"'").rstrip("/,")
    if not s:
        return None
    s = s.split("#")[0].split("?")[0].strip()
    if s.startswith("git@"):
        s = s.split(":", 1)[-1]
    for prefix in ("https://", "http://", "ssh://", "git://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    if s.lower().startswith("www."):
        s = s[4:]
    if s.lower().startswith("github.com/"):
        s = s[len("github.com/"):]
    elif "/" in s and "." in s.split("/")[0]:
        return None  # 깃허브가 아닌 호스트
    parts = [p for p in s.split("/") if p]
    if len(parts) < 2:
        return None
    owner, name = parts[0], parts[1]
    if name.lower().endswith(".git"):
        name = name[:-4]
    if not owner or not name:
        return None
    return owner, name


def load_repos(path):
    """입력 파일 → [(팀명, owner, name, 원문)]. 중복 레포는 전체에서 한 번만.

    한 레포로 개발한 팀은 프론트·백엔드 칸에 같은 URL을 넣게 되어 있어서
    (그렇게 안내됐다) 중복이 정상적으로 들어온다. 600개 중 상당수가
    이런 중복이라 여기서 걸러야 호출 수가 줄어든다.
    """
    rows, bad, seen = [], [], set()
    with open(path, encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cells = [c.strip() for c in line.split(",")]
            team, urls = "", []
            for c in cells:
                if parse_repo_url(c):
                    urls.append(c)
                elif not team and c:
                    team = c
            if not urls:
                bad.append((lineno, line))
                continue
            for u in urls:
                owner, name = parse_repo_url(u)
                key = (owner.lower(), name.lower())
                if key in seen:
                    continue
                seen.add(key)
                rows.append((team or f"{owner}/{name}", owner, name, u))
    return rows, bad


# ── 수집 ─────────────────────────────────────────────────────────

def fetch(owner, name, deadline, want_commits=True):
    """레포 하나의 상태. 네트워크가 닿는 유일한 곳이다."""
    info = {"owner": owner, "name": name, "ok": False, "err": None,
            "private": None, "default_branch": None, "pushed_at": None,
            "branches": {}, "commits": [], "seen_at": None}
    try:
        repo, _ = api(f"/repos/{owner}/{name}")
    except NotFound:
        # 삭제됐거나 비공개거나 오타. 토큰 권한 밖의 비공개 레포도 404다.
        info["err"] = "접근 불가 (삭제·비공개·오타)"
        return info
    except Exception as e:
        info["err"] = f"조회 실패: {type(e).__name__} {e}"
        return info

    info.update(ok=True, private=repo.get("private"),
                default_branch=repo.get("default_branch"),
                pushed_at=repo.get("pushed_at"),
                seen_at=datetime.datetime.now(UTC).isoformat())

    try:
        for b in api_all(f"/repos/{owner}/{name}/branches"):
            info["branches"][b["name"]] = b["commit"]["sha"]
    except Exception as e:
        # 브랜치를 못 받으면 스냅샷이 반쪽이라 대조가 무의미해진다.
        # 조용히 빈 스냅샷을 남기지 말고 실패로 표시해 --resume이 다시 받게 한다.
        info["ok"] = False
        info["err"] = f"브랜치 조회 실패: {type(e).__name__} {e}"
        return info

    # pushed_at이 마감 전이면 마감 후 커밋이 있을 수 없다 — 호출을 아낀다.
    # 600개 중 대부분이 여기서 걸러져 레이트리밋이 크게 절약된다.
    pushed = parse_time(info["pushed_at"])
    if want_commits and pushed and pushed >= deadline:
        seen = set()
        for branch in info["branches"]:
            try:
                for c in api_all(f"/repos/{owner}/{name}/commits",
                                 {"sha": branch, "since": api_time(deadline)}):
                    if c["sha"] in seen:
                        continue
                    seen.add(c["sha"])
                    cm = c.get("commit", {})
                    info["commits"].append({
                        "sha": c["sha"][:8],
                        "branch": branch,
                        "authored": cm.get("author", {}).get("date"),
                        "committed": cm.get("committer", {}).get("date"),
                        "author": (cm.get("author", {}).get("name")
                                   or (c.get("author") or {}).get("login") or "?"),
                        "message": (cm.get("message") or "").splitlines()[0][:60],
                    })
            except Exception:
                continue  # 브랜치 하나가 실패해도 나머지는 본다
    return info


def run_concurrent(items, work, workers, label):
    """동시에 처리하며 진행 상황을 찍는다. 결과 순서는 입력 순으로 복원한다."""
    total = len(items)
    done = [0]
    lock = threading.Lock()
    t0 = time.time()

    def wrapped(pair):
        idx, item = pair
        try:
            out = work(item)
        except Exception as e:   # 하나가 터져도 나머지 599개는 끝내야 한다
            out = ("__error__", f"{type(e).__name__}: {e}")
        with lock:
            done[0] += 1
            n = done[0]
            if n % 25 == 0 or n == total:
                el = time.time() - t0
                eta = (total - n) * el / n if n else 0
                print(f"  {label} {n}/{total}  ({el:.0f}초 경과, "
                      f"남은 시간 약 {eta:.0f}초)", file=sys.stderr, flush=True)
        return idx, out

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(wrapped, enumerate(items)))
    results.sort(key=lambda x: x[0])
    return [r for _, r in results]


def normalize(results):
    """run_concurrent이 삼킨 예외를 리포트가 읽을 수 있는 형태로."""
    out = []
    for r in results:
        if r and r[0] == "__error__":
            out.append(("오류", "?", "?", r[1], []))
        else:
            out.append(r)
    return out


# ── 판정 ─────────────────────────────────────────────────────────

def judge(info, deadline):
    """수집 결과 → (등급, 한 줄 요약, 근거 줄들). 네트워크를 타지 않는다.

    등급은 위반을 '확정'하지 않는다. 근거만 내놓고 판단은 운영진이 한다 —
    리베이스·머지처럼 악의 없는 원인으로도 시각이 밀리기 때문이다.
    """
    if not info["ok"]:
        return "오류", info["err"], []
    if info["private"]:
        return "확인필요", "비공개 레포 (Public 유지 필수)", []

    pushed = parse_time(info["pushed_at"])

    if not pushed or pushed < deadline:
        return "정상", f"마감 전 푸시 ({fmt(pushed)})", []

    if not info["commits"]:
        # 푸시는 마감 후인데 마감 후 커밋이 없다. 브랜치·태그 삭제,
        # 또는 커밋 날짜를 마감 전으로 위조한 force-push일 수 있다.
        return "확인필요", "마감 후 푸시, 커밋은 확인 안 됨", [
            f"마지막 푸시 {fmt(pushed)}",
            "→ 브랜치 삭제 / force-push / 커밋 날짜 조작 가능성",
        ]

    lines = [f"마지막 푸시 {fmt(pushed)}"]
    ordered = sorted(info["commits"], key=lambda x: x["committed"] or "")
    for c in ordered[:MAX_COMMIT_LINES]:
        a, cm = parse_time(c["authored"]), parse_time(c["committed"])
        note = ""
        if a and cm and a < deadline <= cm:
            # 작성 시각은 마감 전인데 커밋 시각은 마감 후 —
            # 리베이스·체리픽이면 정상, --date 위조여도 이렇게 보인다.
            note = f"  [작성 {fmt(a)} / 커밋 {fmt(cm)} 불일치]"
        lines.append(f"{fmt(cm)}  {c['sha']}  [{c['branch']}]  "
                     f"{c['author']}  {c['message']}{note}")
    if len(ordered) > MAX_COMMIT_LINES:
        lines.append(f"... 외 {len(ordered) - MAX_COMMIT_LINES}건 (CSV로 전체 확인)")
    return "위반", f"마감 후 커밋 {len(ordered)}건", lines


# ── 스냅샷 대조 ──────────────────────────────────────────────────

def compare_one(owner, name, before, deadline):
    """스냅샷 이후 브랜치가 어떻게 변했는지. force-push까지 잡는 경로다."""
    now = fetch(owner, name, deadline, want_commits=False)
    if not now["ok"]:
        return "오류", now["err"], []

    old, new = before.get("branches", {}), now["branches"]
    lines, worst = [], "정상"

    # 600개를 찍는 데 몇 분이 걸리므로 마감(10:00)과 스냅샷 사이에 사각지대가
    # 생긴다. 그 틈에 푸시한 팀은 '수정 후' 상태로 스냅샷에 들어가 대조로는
    # 영영 안 잡힌다. 스냅샷 당시 pushed_at이 이미 마감 후였다면 여기서 잡는다.
    snap_pushed = parse_time(before.get("pushed_at"))
    gap = snap_pushed is not None and snap_pushed >= deadline
    if gap:
        lines.append(f"스냅샷 시점에 이미 마감 후 푸시 있음 ({fmt(snap_pushed)})")
        lines.append("→ 마감과 스냅샷 사이에 푸시됨. 대조로는 안 잡히니 직접 확인 필요")
        worst = "확인필요"

    for b, old_sha in old.items():
        if b not in new:
            lines.append(f"[{b}] 브랜치가 삭제됨 (스냅샷 {old_sha[:8]})")
            worst = "위반"
            continue
        if new[b] == old_sha:
            continue
        try:
            cmp_, _ = api(f"/repos/{owner}/{name}/compare/{old_sha}...{new[b]}")
        except Exception as e:
            lines.append(f"[{b}] {old_sha[:8]} → {new[b][:8]} (대조 실패: {e})")
            if worst != "위반":
                worst = "확인필요"
            continue
        status = cmp_.get("status")
        if status == "ahead":
            commits = cmp_.get("commits", [])
            lines.append(f"[{b}] 커밋 {cmp_.get('ahead_by', len(commits))}건 추가")
            for c in commits[:MAX_COMMIT_LINES]:
                cm = c.get("commit", {})
                lines.append(
                    f"    {fmt(parse_time(cm.get('committer', {}).get('date')))}"
                    f"  {c['sha'][:8]}  {cm.get('author', {}).get('name', '?')}"
                    f"  {(cm.get('message') or '').splitlines()[0][:50]}")
            if len(commits) > MAX_COMMIT_LINES:
                lines.append(f"    ... 외 {len(commits) - MAX_COMMIT_LINES}건")
            worst = "위반"
        else:
            # diverged/behind = 스냅샷의 커밋이 더 이상 도달 불가.
            # 히스토리를 다시 썼다는 뜻이라 사후 검사로는 절대 안 잡힌다.
            lines.append(f"[{b}] 이력 변경됨 — force-push/rebase "
                         f"(스냅샷 {old_sha[:8]} → 현재 {new[b][:8]}, {status})")
            worst = "위반"

    for b in new:
        if b not in old:
            lines.append(f"[{b}] 마감 후 새 브랜치 생성 ({new[b][:8]})")
            if worst == "정상":
                worst = "확인필요"

    if worst == "정상":
        return "정상", "스냅샷과 동일", []
    # 요약은 세어서 따로 만든다. lines[0]을 그대로 쓰면 리포트에서
    # 같은 줄이 요약과 근거로 두 번 찍힌다.
    changed = sum(1 for b, sha in old.items() if new.get(b) != sha)
    added = sum(1 for b in new if b not in old)
    bits = []
    if changed:
        bits.append(f"브랜치 {changed}개 변경")
    if added:
        bits.append(f"새 브랜치 {added}개")
    if not bits:
        return worst, "스냅샷 시점에 이미 마감 후 푸시", lines
    return worst, "스냅샷 이후 " + ", ".join(bits), lines


# ── 리포트 ───────────────────────────────────────────────────────

RANK = {"위반": 0, "확인필요": 1, "오류": 2, "정상": 3}
MARK = {"위반": "[위반]", "확인필요": "[확인]", "오류": "[오류]", "정상": "[정상]"}


def report(results, deadline, mode):
    out = [
        "=" * 70,
        f"마감 후 커밋 검사 — {mode}",
        f"기준: {deadline.astimezone(KST):%Y-%m-%d %H:%M:%S} KST 이후를 마감 후로 봄",
        f"검사: {len(results)}개 레포",
        "=" * 70, "",
    ]
    counts = {}
    for grade, *_ in results:
        counts[grade] = counts.get(grade, 0) + 1
    out.append("  ".join(f"{MARK[g]} {counts[g]}건"
                         for g in sorted(counts, key=lambda g: RANK[g])))
    out.append("")

    shown = 0
    for grade, team, repo, summary, lines in sorted(
            results, key=lambda r: (RANK[r[0]], str(r[1]))):
        if grade == "정상":
            continue
        shown += 1
        out.append(f"{MARK[grade]} {team}")
        out.append(f"       {repo}")
        out.append(f"       {summary}")
        for ln in lines:
            out.append(f"         {ln}")
        out.append("")

    if not shown:
        out.append("마감 후 변경이 확인된 레포가 없습니다.")
        out.append("")

    out += [
        "-" * 70,
        "이 리포트는 근거만 제시합니다. 탈락 여부는 운영진이 판단하세요.",
        "리베이스·머지처럼 악의 없는 작업으로도 커밋 시각이 밀립니다.",
    ]
    if mode == "사후 검사":
        out.append("force-push로 히스토리를 다시 쓰면 이 검사로는 잡히지 않습니다.")
        out.append("--snapshot으로 마감 직후 상태를 찍어두면 --compare로 잡힙니다.")
    if _last_remaining[0] is not None:
        out.append(f"남은 API 호출: {_last_remaining[0]}회 (토큰 있을 때 시간당 5000회)")
    return "\n".join(out)


def write_csv(path, results):
    """600행은 눈으로 못 읽는다. 정렬·필터·담당자 분배는 스프레드시트에서."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["등급", "팀", "레포", "요약", "근거"])
        for grade, team, repo, summary, lines in sorted(
                results, key=lambda r: (RANK[r[0]], str(r[1]))):
            w.writerow([grade, team, repo, summary, " / ".join(lines)])


# ── 스냅샷 ───────────────────────────────────────────────────────

def do_snapshot(rows, deadline, path, workers, resume):
    have = {}
    if resume and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
        # 실패한 레포는 다시 받는다. 성공분만 건너뛴다.
        have = {(r["owner"], r["name"]): r for r in old["repos"] if r.get("ok")}
        print(f"이어받기: 이미 받은 {len(have)}개는 건너뜁니다.", file=sys.stderr)

    todo = [r for r in rows if (r[1], r[2]) not in have]
    if not todo:
        print("받을 것이 없습니다. 전부 이미 스냅샷에 있습니다.", file=sys.stderr)
        return 0

    started = datetime.datetime.now(KST)
    collected = list(have.values())
    lock = threading.Lock()

    def save():
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"taken_at": started.isoformat(),
                       "finished_at": datetime.datetime.now(KST).isoformat(),
                       "deadline": deadline.isoformat(),
                       "repos": collected}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)   # 중간에 죽어도 파일이 깨지지 않게 원자적 교체

    def work(row):
        team, owner, name, _ = row
        info = fetch(owner, name, deadline, want_commits=False)
        rec = {"team": team, "owner": owner, "name": name, "ok": info["ok"],
               "err": info["err"], "private": info["private"],
               "pushed_at": info["pushed_at"], "seen_at": info["seen_at"],
               "default_branch": info["default_branch"],
               "branches": info["branches"]}
        with lock:
            collected.append(rec)
            if len(collected) % 50 == 0:   # 중간 저장 — 죽어도 처음부터 안 감
                save()
        return rec

    run_concurrent(todo, work, workers, "스냅샷")
    save()

    ok = sum(1 for r in collected if r.get("ok"))
    late = [r for r in collected
            if r.get("ok") and parse_time(r.get("pushed_at"))
            and parse_time(r["pushed_at"]) >= deadline]
    took = (datetime.datetime.now(KST) - started).total_seconds()

    print(f"\n스냅샷 저장: {path}")
    print(f"  {ok}/{len(collected)}개 레포, "
          f"브랜치 {sum(len(r.get('branches') or {}) for r in collected)}개")
    print(f"  소요 {took:.0f}초 ({started:%H:%M:%S} ~ "
          f"{datetime.datetime.now(KST):%H:%M:%S} KST)")
    if len(collected) - ok:
        print(f"  [실패 {len(collected) - ok}개] "
              f"`--snapshot {path} --resume`으로 다시 받으세요")
    if late:
        print(f"  [주의] 스냅샷 시점에 이미 마감 후 푸시가 있는 레포 {len(late)}개")
        print("     마감과 스냅샷 사이에 들어온 푸시라 대조로는 안 잡힙니다.")
        for r in late[:10]:
            print(f"       {r['owner']}/{r['name']}  "
                  f"{fmt(parse_time(r['pushed_at']))}")
        if len(late) > 10:
            print(f"       ... 외 {len(late) - 10}개")
    print(f"\n  대조: python check.py --compare {path}")
    return 0


# ── CLI ──────────────────────────────────────────────────────────

def main():
    global TOKEN
    p = argparse.ArgumentParser(description="제출 레포의 마감 후 커밋 검사")
    p.add_argument("repos", nargs="?", help="레포 목록 파일 (URL 한 줄에 하나)")
    p.add_argument("--deadline", default=DEFAULT_DEADLINE,
                   help=f"마감 시각, KST 기준 (기본 {DEFAULT_DEADLINE})")
    p.add_argument("--snapshot", metavar="FILE",
                   help="현재 브랜치 상태를 파일로 저장 (마감 직후에 실행)")
    p.add_argument("--resume", action="store_true",
                   help="--snapshot과 함께: 이미 받은 레포는 건너뛴다")
    p.add_argument("--compare", metavar="FILE", help="저장해둔 스냅샷과 대조")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"동시 실행 수 (기본 {DEFAULT_WORKERS})")
    p.add_argument("-o", "--out", help="리포트를 파일로도 저장")
    p.add_argument("--csv", help="결과를 CSV로 저장 (600개는 스프레드시트가 편하다)")
    args = p.parse_args()

    deadline = parse_time(args.deadline)
    TOKEN = get_token()
    if not TOKEN:
        print("경고: 토큰이 없습니다. 시간당 60회로 막혀 600개를 못 돕니다.\n"
              "      `gh auth login` 하거나 GITHUB_TOKEN을 설정하세요.\n",
              file=sys.stderr)

    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            snap = json.load(f)
        before = {(r["owner"], r["name"]): r for r in snap["repos"]}
        rows = [(r["team"], r["owner"], r["name"]) for r in snap["repos"]]
        print(f"스냅샷 {args.compare} ({snap['taken_at'][:19]}) 대조 — "
              f"{len(rows)}개, 동시 {args.workers}\n", file=sys.stderr)

        def work(row):
            team, owner, name = row
            g, s, ln = compare_one(owner, name, before[(owner, name)], deadline)
            return (g, team, f"{owner}/{name}", s, ln)

        results = normalize(run_concurrent(rows, work, args.workers, "대조"))
        text = report(results, deadline, "스냅샷 대조")
    else:
        if not args.repos:
            p.error("레포 목록 파일이 필요합니다 (또는 --compare)")
        rows, bad = load_repos(args.repos)
        for lineno, line in bad:
            print(f"경고: {lineno}행을 읽지 못했습니다 — {line}", file=sys.stderr)
        if not rows:
            p.error("읽을 수 있는 레포 주소가 없습니다")

        if args.snapshot:
            return do_snapshot(rows, deadline, args.snapshot,
                               args.workers, args.resume)

        print(f"{len(rows)}개 레포 검사 중 (동시 {args.workers})...\n",
              file=sys.stderr)

        def work(row):
            team, owner, name, _ = row
            info = fetch(owner, name, deadline, want_commits=True)
            g, s, ln = judge(info, deadline)
            return (g, team, f"{owner}/{name}", s, ln)

        results = normalize(run_concurrent(rows, work, args.workers, "검사"))
        text = report(results, deadline, "사후 검사")

    print("\n" + text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\n리포트 저장: {args.out}", file=sys.stderr)
    if args.csv:
        write_csv(args.csv, results)
        print(f"CSV 저장: {args.csv}", file=sys.stderr)
    return 1 if any(g == "위반" for g, *_ in results) else 0


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
