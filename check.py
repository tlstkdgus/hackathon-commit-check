# -*- coding: utf-8 -*-
"""마감 후 커밋 검사 — 제출된 깃허브 레포에 마감 시각 이후 커밋이 있는지 본다.

의존성 0. 토큰은 `GITHUB_TOKEN` 환경변수 또는 `gh auth token`에서 가져온다.

    python check.py repos.txt                     # 검사
    python check.py repos.txt --snapshot s.json   # 마감 직후 스냅샷 저장
    python check.py --compare s.json              # 스냅샷과 대조 (force-push까지 잡음)

repos.txt 형식 — 빈 줄과 `#` 주석은 무시:
    https://github.com/owner/repo
    팀이름, https://github.com/o/front, https://github.com/o/back

왜 스냅샷이 필요한가:
    사후 검사만으로는 `git push --force`를 못 잡는다. 히스토리를 다시 쓰면
    마감 후 커밋이 사라지고 남은 커밋은 전부 마감 전으로 보인다.
    커밋 날짜도 `git commit --date`로 위조된다.
    마감 직후에 브랜치 SHA를 찍어두면 이 둘이 모두 드러난다.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"

# KST는 서머타임이 없어 UTC+9 고정. zoneinfo를 쓰면 윈도우에서 tzdata가
# 필요해지는데, 고정 오프셋이면 같은 결과를 의존성 0으로 얻는다.
KST = datetime.timezone(datetime.timedelta(hours=9), "KST")
UTC = datetime.timezone.utc

# 결과물 제출 마감은 8/21(금) 09:59:59 → 10:00:00부터가 마감 후.
DEFAULT_DEADLINE = "2026-08-21 10:00"


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
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read().decode("utf-8")), dict(res.headers)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(path)
            # 레이트리밋: 리셋까지 기다린다. 토큰 없이 돌리면 시간당 60회라
            # 60팀×3레포에서 바로 걸린다 — 그래서 토큰을 권장한다.
            if e.code in (403, 429):
                reset = e.headers.get("X-RateLimit-Reset")
                if reset and e.headers.get("X-RateLimit-Remaining") == "0":
                    wait = max(0, int(reset) - int(time.time())) + 2
                    print(f"    레이트리밋 — {wait}초 대기", file=sys.stderr)
                    time.sleep(min(wait, 900))
                    continue
            if e.code >= 500 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError(f"3회 실패: {path}")


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
    """입력 파일 → [(팀명, owner, name, 원문)]. 팀 안에서 중복 URL은 제거한다.

    한 레포로 개발한 팀은 프론트·백엔드 칸에 같은 URL을 넣게 되어 있어서
    (그렇게 안내됐다) 중복이 정상적으로 들어온다.
    """
    rows, bad = [], []
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
            seen = set()
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
            "branches": {}, "commits": []}
    try:
        repo, _ = api(f"/repos/{owner}/{name}")
    except NotFound:
        # 삭제됐거나 비공개거나 오타. 토큰 권한 밖의 비공개 레포도 404다.
        info["err"] = "접근 불가 (삭제·비공개·오타)"
        return info
    except Exception as e:
        info["err"] = f"조회 실패: {e}"
        return info

    info.update(ok=True, private=repo.get("private"),
                default_branch=repo.get("default_branch"),
                pushed_at=repo.get("pushed_at"))

    for b in api_all(f"/repos/{owner}/{name}/branches"):
        info["branches"][b["name"]] = b["commit"]["sha"]

    # pushed_at이 마감 전이면 마감 후 커밋이 있을 수 없다 — 호출을 아낀다.
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
    lines = [f"마지막 푸시 {fmt(pushed)}"]

    if not pushed or pushed < deadline:
        return "정상", f"마감 전 푸시 ({fmt(pushed)})", []

    if not info["commits"]:
        # 푸시는 마감 후인데 마감 후 커밋이 없다. 브랜치·태그 삭제,
        # 또는 커밋 날짜를 마감 전으로 위조한 force-push일 수 있다.
        lines.append("마감 후 푸시 기록이 있으나 마감 후 커밋은 없음")
        lines.append("→ 브랜치 삭제 / force-push / 커밋 날짜 조작 가능성")
        return "확인필요", "마감 후 푸시, 커밋은 확인 안 됨", lines

    for c in sorted(info["commits"], key=lambda x: x["committed"] or ""):
        a, cm = parse_time(c["authored"]), parse_time(c["committed"])
        note = ""
        if a and cm and a < deadline <= cm:
            # 작성 시각은 마감 전인데 커밋 시각은 마감 후 —
            # 리베이스·체리픽이면 정상, --date 위조여도 이렇게 보인다.
            note = f"  ⚠ 작성 {fmt(a)} / 커밋 {fmt(cm)} 불일치"
        lines.append(f"{fmt(cm)}  {c['sha']}  [{c['branch']}]  "
                     f"{c['author']}  {c['message']}{note}")
    n = len(info["commits"])
    return "위반", f"마감 후 커밋 {n}건", lines


# ── 스냅샷 대조 ──────────────────────────────────────────────────

def compare_one(owner, name, before, deadline):
    """스냅샷 이후 브랜치가 어떻게 변했는지. force-push까지 잡는 경로다."""
    now = fetch(owner, name, deadline, want_commits=False)
    if not now["ok"]:
        return "오류", now["err"], []

    old, new = before.get("branches", {}), now["branches"]
    lines, worst = [], "정상"

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
            worst = "확인필요"
            continue
        status = cmp_.get("status")
        if status == "ahead":
            lines.append(f"[{b}] 커밋 {cmp_.get('ahead_by', 0)}건 추가")
            for c in cmp_.get("commits", []):
                cm = c.get("commit", {})
                lines.append(
                    f"    {fmt(parse_time(cm.get('committer', {}).get('date')))}"
                    f"  {c['sha'][:8]}  {cm.get('author', {}).get('name', '?')}"
                    f"  {(cm.get('message') or '').splitlines()[0][:50]}")
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
    return worst, "스냅샷 이후 " + ", ".join(bits or ["변경 감지"]), lines


# ── 리포트 ───────────────────────────────────────────────────────

RANK = {"위반": 0, "확인필요": 1, "오류": 2, "정상": 3}
MARK = {"위반": "[위반]", "확인필요": "[확인]", "오류": "[오류]", "정상": "[정상]"}


def report(results, deadline, mode):
    out = [
        "=" * 66,
        f"마감 후 커밋 검사 — {mode}",
        f"기준: {deadline.astimezone(KST):%Y-%m-%d %H:%M:%S} KST 이후를 마감 후로 봄",
        f"검사: {len(results)}개 레포",
        "=" * 66, "",
    ]
    counts = {}
    for grade, *_ in results:
        counts[grade] = counts.get(grade, 0) + 1
    out.append("  ".join(f"{MARK[g]} {counts[g]}건"
                         for g in sorted(counts, key=lambda g: RANK[g])))
    out.append("")

    for grade, team, repo, summary, lines in sorted(
            results, key=lambda r: (RANK[r[0]], r[1])):
        if grade == "정상":
            continue
        out.append(f"{MARK[grade]} {team}")
        out.append(f"       {repo}")
        out.append(f"       {summary}")
        for ln in lines:
            out.append(f"         {ln}")
        out.append("")

    if not any(g != "정상" for g, *_ in results):
        out.append("마감 후 변경이 확인된 레포가 없습니다.")
        out.append("")

    out += [
        "-" * 66,
        "이 리포트는 근거만 제시합니다. 탈락 여부는 운영진이 판단하세요.",
        "리베이스·머지처럼 악의 없는 작업으로도 커밋 시각이 밀립니다.",
    ]
    if mode == "사후 검사":
        out.append("force-push로 히스토리를 다시 쓰면 이 검사로는 잡히지 않습니다.")
        out.append("--snapshot으로 마감 직후 상태를 찍어두면 --compare로 잡힙니다.")
    return "\n".join(out)


# ── CLI ──────────────────────────────────────────────────────────

def main():
    global TOKEN
    p = argparse.ArgumentParser(description="제출 레포의 마감 후 커밋 검사")
    p.add_argument("repos", nargs="?", help="레포 목록 파일 (URL 한 줄에 하나)")
    p.add_argument("--deadline", default=DEFAULT_DEADLINE,
                   help=f"마감 시각, KST 기준 (기본 {DEFAULT_DEADLINE})")
    p.add_argument("--snapshot", metavar="FILE",
                   help="현재 브랜치 상태를 파일로 저장 (마감 직후에 실행)")
    p.add_argument("--compare", metavar="FILE",
                   help="저장해둔 스냅샷과 대조")
    p.add_argument("-o", "--out", help="리포트를 파일로도 저장")
    args = p.parse_args()

    deadline = parse_time(args.deadline)
    TOKEN = get_token()
    if not TOKEN:
        print("경고: 토큰이 없습니다. 시간당 60회로 제한돼 곧 막힙니다.\n"
              "      `gh auth login` 하거나 GITHUB_TOKEN을 설정하세요.\n",
              file=sys.stderr)

    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            snap = json.load(f)
        rows = [(r["team"], r["owner"], r["name"]) for r in snap["repos"]]
        before = {(r["owner"], r["name"]): r for r in snap["repos"]}
        print(f"스냅샷 {args.compare} ({snap['taken_at']}) 대조 — {len(rows)}개\n",
              file=sys.stderr)
        results = []
        for i, (team, owner, name) in enumerate(rows, 1):
            print(f"  [{i}/{len(rows)}] {owner}/{name}", file=sys.stderr)
            g, s, ln = compare_one(owner, name, before[(owner, name)], deadline)
            results.append((g, team, f"{owner}/{name}", s, ln))
        text = report(results, deadline, "스냅샷 대조")
    else:
        if not args.repos:
            p.error("레포 목록 파일이 필요합니다 (또는 --compare)")
        rows, bad = load_repos(args.repos)
        for lineno, line in bad:
            print(f"경고: {lineno}행을 읽지 못했습니다 — {line}", file=sys.stderr)
        if not rows:
            p.error("읽을 수 있는 레포 주소가 없습니다")
        print(f"{len(rows)}개 레포 검사 중...\n", file=sys.stderr)

        results, snap_rows = [], []
        for i, (team, owner, name, _raw) in enumerate(rows, 1):
            print(f"  [{i}/{len(rows)}] {owner}/{name}", file=sys.stderr)
            info = fetch(owner, name, deadline, want_commits=not args.snapshot)
            snap_rows.append({"team": team, "owner": owner, "name": name,
                              "ok": info["ok"], "pushed_at": info["pushed_at"],
                              "default_branch": info["default_branch"],
                              "branches": info["branches"]})
            if not args.snapshot:
                g, s, ln = judge(info, deadline)
                results.append((g, team, f"{owner}/{name}", s, ln))

        if args.snapshot:
            with open(args.snapshot, "w", encoding="utf-8") as f:
                json.dump({"taken_at": datetime.datetime.now(KST).isoformat(),
                           "deadline": deadline.isoformat(),
                           "repos": snap_rows}, f, ensure_ascii=False, indent=2)
            n_ok = sum(1 for r in snap_rows if r["ok"])
            print(f"\n스냅샷 저장: {args.snapshot}")
            print(f"  {n_ok}/{len(snap_rows)}개 레포, "
                  f"브랜치 {sum(len(r['branches']) for r in snap_rows)}개")
            print("  나중에 `python check.py --compare "
                  f"{args.snapshot}`로 대조하세요.")
            return 0
        text = report(results, deadline, "사후 검사")

    print("\n" + text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\n리포트 저장: {args.out}", file=sys.stderr)
    return 1 if any(g == "위반" for g, *_ in results) else 0


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
