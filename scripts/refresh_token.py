#!/usr/bin/env python3
"""인스타 장기 토큰 연장 — 깃허브 액션(.github/workflows/refresh-token.yml)이 매주 실행해요.

장기 토큰은 발급(또는 마지막 연장) 후 60일이 지나면 만료돼요. 매주 연장하면 끊기지 않아요.
보통 같은 토큰의 기한만 늘어나는데, 가끔 새 토큰이 나와요.
  - 비밀값 GH_PAT(이 저장소의 Secrets 쓰기 권한만 준 토큰)이 있으면 새 토큰을 IG_ACCESS_TOKEN에 자동 저장
  - 없으면 실행을 실패로 끝내서 깃허브가 메일로 알려 줘요 → Meta 개발자 페이지에서 토큰을 다시 발급해 바꿔 주세요
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("IG_API_BASE", "https://graph.instagram.com").rstrip("/")


def summary(md):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def main():
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        raise SystemExit("IG_ACCESS_TOKEN 비밀값이 없어요")
    q = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": token})
    try:
        with urllib.request.urlopen(f"{API_BASE}/refresh_access_token?{q}", timeout=60) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            msg = json.loads(body).get("error", {}).get("message", body[:300])
        except ValueError:
            msg = body[:300]
        summary(f"### ❌ 토큰 연장 실패\n- {msg}\n- 토큰이 이미 만료됐다면 Meta 개발자 페이지에서 다시 발급해 IG_ACCESS_TOKEN을 바꿔 주세요.")
        raise SystemExit(f"토큰 연장 실패: HTTP {e.code} {msg}")
    new = data.get("access_token")
    days = int(data.get("expires_in", 0)) // 86400
    if not new:
        raise SystemExit("응답에 토큰이 없어요")
    if new == token:
        print(f"토큰 연장 완료 — 앞으로 약 {days}일")
        summary(f"### ✅ 토큰 연장 완료 (약 {days}일 남음)")
        return 0
    pat = os.environ.get("GH_PAT")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if pat and repo:
        # 토큰을 명령줄에 쓰지 않고 표준입력으로 넘겨요
        env = dict(os.environ, GH_TOKEN=pat)
        p = subprocess.run(["gh", "secret", "set", "IG_ACCESS_TOKEN", "--repo", repo], input=new.encode(),
                           env=env, capture_output=True)
        if p.returncode == 0:
            print(f"새 토큰을 받아 IG_ACCESS_TOKEN에 저장했어요 — 앞으로 약 {days}일")
            summary(f"### ✅ 새 토큰으로 교체 완료 (약 {days}일)")
            return 0
        print("새 토큰 저장 실패:", p.stderr.decode(errors="replace")[:300])
    summary("### ⚠ 새 토큰이 발급됐어요\n자동 저장을 못 했어요. Meta 개발자 페이지에서 토큰을 다시 발급해 IG_ACCESS_TOKEN을 바꿔 주세요. (비밀값 GH_PAT을 넣어 두면 다음부터 자동으로 바꿔요)")
    raise SystemExit("새 토큰이 발급됐는데 자동 저장을 못 했어요")


if __name__ == "__main__":
    sys.exit(main())
