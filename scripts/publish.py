#!/usr/bin/env python3
"""인스타 자동 게시 — queue/<게시일>/ 가운데 게시 시각이 지난 것 하나를 인스타 공식 API로 올려요.

깃허브 액션(.github/workflows/publish.yml)이 매시간 실행해요. 직접 돌릴 때:
  MODE=check    python3 scripts/publish.py   # 토큰·계정만 확인
  MODE=dry-run  python3 scripts/publish.py   # 올릴 게시물과 이미지 주소만 확인 (게시 안 함)
  MODE=publish  python3 scripts/publish.py   # 실제 게시

환경변수
  IG_ACCESS_TOKEN   (필수) Instagram API with Instagram Login 장기 토큰 — 저장소 비밀값에만 보관
  IG_USER_ID        (선택) 없으면 토큰으로 조회
  GITHUB_REPOSITORY, GITHUB_SHA   (액션이 자동으로 넣어 줌) 이미지 공개 주소를 만들 때 써요
  IG_API_BASE       기본 https://graph.instagram.com   (시험할 때만 바꿔요)
  IG_API_VERSION    기본 v23.0
  RAW_BASE          기본 https://raw.githubusercontent.com/<저장소>/<커밋>

대기열 폴더 규칙 (카드 스크립트 kit/insta_card_kit.py --queue 가 만들어요)
  queue/2026-10-01/01.jpg … 10.jpg   JPEG, 4:5, 최대 10장
  queue/2026-10-01/caption.txt       캡션 + 해시태그
  queue/2026-10-01/post.json         {"publish_at": "2026-10-01T12:00:00+09:00", "images": [...], "alt": [...], "hold": false}
  보류: post.json 의 "hold": true 또는 폴더에 HOLD 파일
게시가 끝나면 폴더를 posted/ 로 옮기고 result.json(게시 주소)을 남겨요. 3번 실패하면 자동으로 보류해요.

안전장치
  · posted/ 에 같은 이름 폴더(result.json)가 있으면 이미 올린 게시물이라 다시 올리지 않고 대기열에서 빼요
    (일부러 다시 올리려면 post.json 에 "repost": true)
  · 예정 시각보다 18시간 넘게 지난 게시물은 철 지난 정보일 수 있어 올리지 않고 자동 보류해요
  · 한 번 실행에 하나만 올려요 (밀린 게 있으면 다음 실행에서 이어서)
"""
import datetime
import glob
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

KST = datetime.timezone(datetime.timedelta(hours=9))
API_BASE = os.environ.get("IG_API_BASE", "https://graph.instagram.com").rstrip("/")
API_VERSION = os.environ.get("IG_API_VERSION", "v23.0").strip("/")
MODE = (os.environ.get("MODE") or "publish").strip().lower()
MAX_ITEMS = 10
MAX_ATTEMPTS = 3
MAX_LATE_HOURS = 18
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE = os.path.join(ROOT, "queue")
POSTED = os.path.join(ROOT, "posted")


class ApiError(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def summary(md):
    """깃허브 액션 실행 요약 칸에 한국어로 남겨요"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def api(method, path, params=None, token=None):
    params = dict(params or {})
    params["access_token"] = token or os.environ["IG_ACCESS_TOKEN"]
    base = f"{API_BASE}/{API_VERSION}" if API_VERSION else API_BASE
    url = f"{base}/{path.lstrip('/')}"
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(f"{url}?{data.decode()}", method="GET")
    else:
        req = urllib.request.Request(url, data=data, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                err = json.loads(body).get("error", {})
            except ValueError:
                err = {"message": body[:300]}
            transient = e.code >= 500 or err.get("is_transient")
            if transient and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            msg = err.get("error_user_msg") or err.get("message") or body[:300]
            raise ApiError(f"HTTP {e.code} code={err.get('code')} sub={err.get('error_subcode')}: {msg}")
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise ApiError(f"접속 실패: {e.reason}")


def whoami():
    me = api("GET", "me", {"fields": "user_id,username,account_type"})
    return me


def wait_ready(container_id, what, timeout=120):
    """컨테이너가 FINISHED 될 때까지 기다려요"""
    t0 = time.time()
    while True:
        st = api("GET", container_id, {"fields": "status_code,status"})
        code = st.get("status_code")
        if code in ("FINISHED", "PUBLISHED"):
            return
        if code in ("ERROR", "EXPIRED"):
            raise ApiError(f"{what} 처리 실패: {code} {st.get('status', '')}")
        if time.time() - t0 > timeout:
            raise ApiError(f"{what} 처리가 {timeout}초 안에 끝나지 않았어요 (상태 {code})")
        time.sleep(3)


def create_image(ig_id, url, alt=None, carousel_item=False, caption=None):
    params = {"image_url": url}
    if carousel_item:
        params["is_carousel_item"] = "true"
    if caption:
        params["caption"] = caption
    if alt:
        params["alt_text"] = alt[:1000]
    try:
        return api("POST", f"{ig_id}/media", params)["id"]
    except ApiError as e:
        if alt and "alt_text" in str(e):
            log("  (대체 텍스트를 받지 않아 빼고 다시 시도해요)")
            params.pop("alt_text")
            return api("POST", f"{ig_id}/media", params)["id"]
        raise


def check_url(url):
    """인스타가 가져갈 이미지 주소가 열리는지 미리 확인해요"""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ctype = r.headers.get("Content-Type", "")
            if "jpeg" not in ctype:
                raise ApiError(f"이미지 형식이 JPEG가 아니에요 ({ctype}): {url}")
    except urllib.error.HTTPError as e:
        raise ApiError(f"이미지 주소가 열리지 않아요 (HTTP {e.code}): {url}")


def raw_base():
    if os.environ.get("RAW_BASE"):
        return os.environ["RAW_BASE"].rstrip("/")
    repo, sha = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_SHA")
    if not (repo and sha):
        raise SystemExit("GITHUB_REPOSITORY·GITHUB_SHA가 없어요 (깃허브 액션 밖에서는 RAW_BASE를 주세요)")
    return f"https://raw.githubusercontent.com/{repo}/{sha}"


def load_posts():
    posts = []
    for pj in sorted(glob.glob(os.path.join(QUEUE, "*", "post.json"))):
        folder = os.path.dirname(pj)
        try:
            post = json.load(open(pj, encoding="utf-8"))
        except ValueError as e:
            log(f"⚠ {os.path.basename(folder)}: post.json을 읽을 수 없어요 ({e})")
            continue
        post["_folder"], post["_json"] = folder, pj
        posts.append(post)
    return posts


def save_post(post):
    data = {k: v for k, v in post.items() if not k.startswith("_")}
    json.dump(data, open(post["_json"], "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def publish_post(post, ig_id, base):
    folder = post["_folder"]
    pid = os.path.basename(folder)
    images = post.get("images") or sorted(os.path.basename(p) for p in glob.glob(os.path.join(folder, "*.jpg")))
    if not images:
        raise ApiError("이미지가 없어요")
    if len(images) > MAX_ITEMS:
        raise ApiError(f"캐러셀은 {MAX_ITEMS}장까지예요 (지금 {len(images)}장)")
    caption = open(os.path.join(folder, post.get("caption_file", "caption.txt")), encoding="utf-8").read().strip()
    alts = post.get("alt") or []
    urls = [f"{base}/queue/{pid}/{name}" for name in images]
    for u in urls:
        check_url(u)
    if MODE == "dry-run":
        log(f"[시험] {pid}: {len(urls)}장, 캡션 {len(caption)}자 — 이미지 주소 확인 완료, 게시는 하지 않아요")
        return None
    if len(urls) == 1:
        creation = create_image(ig_id, urls[0], alts[0] if alts else None, caption=caption)
        wait_ready(creation, "사진")
    else:
        children = []
        for i, u in enumerate(urls):
            cid = create_image(ig_id, u, alts[i] if i < len(alts) else None, carousel_item=True)
            children.append(cid)
            log(f"  {i + 1}/{len(urls)}장 올림")
        for i, cid in enumerate(children):
            wait_ready(cid, f"{i + 1}장")
        creation = api("POST", f"{ig_id}/media", {"media_type": "CAROUSEL", "children": ",".join(children),
                                                  "caption": caption})["id"]
        wait_ready(creation, "캐러셀")
    media_id = api("POST", f"{ig_id}/media_publish", {"creation_id": creation})["id"]
    info = {}
    try:
        info = api("GET", media_id, {"fields": "permalink,timestamp"})
    except ApiError as e:
        log(f"  (게시는 됐는데 주소를 못 받았어요: {e})")
    return {"media_id": media_id, "permalink": info.get("permalink", ""), "timestamp": info.get("timestamp", ""),
            "images": len(urls)}


def main():
    if not os.environ.get("IG_ACCESS_TOKEN"):
        summary("### ⏸ 인스타 토큰(IG_ACCESS_TOKEN)이 아직 없어요\n저장소 Settings → Secrets and variables → Actions 에 넣으면 게시를 시작해요.")
        log("IG_ACCESS_TOKEN 비밀값이 아직 없어서 쉬어요")
        return 1 if MODE == "check" else 0  # 설정 전에는 매시간 실패 메일이 가지 않게 조용히 넘어가요
    now = datetime.datetime.now(KST)
    if MODE == "check":
        me = whoami()
        log(f"토큰 정상 — @{me.get('username')} ({me.get('account_type')}), user_id {me.get('user_id')}")
        summary(f"### ✅ 토큰 정상\n- 계정: @{me.get('username')} ({me.get('account_type')})\n- 확인 시각: {now:%Y-%m-%d %H:%M}")
        return 0

    posts = load_posts()
    due, waiting, held, stale, dups = [], [], [], [], []
    for p in posts:
        pid = os.path.basename(p["_folder"])
        if os.path.exists(os.path.join(POSTED, pid, "result.json")) and not p.get("repost"):
            dups.append(pid)  # 이미 올린 게시물을 다시 올려 둔 경우
            continue
        if p.get("hold") or os.path.exists(os.path.join(p["_folder"], "HOLD")):
            held.append(pid)
            continue
        try:
            at = datetime.datetime.fromisoformat(p["publish_at"])
        except (KeyError, TypeError, ValueError):
            log(f"⚠ {pid}: publish_at 형식이 이상해요 — 건너뛰어요")
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=KST)
        if now - at > datetime.timedelta(hours=MAX_LATE_HOURS):
            stale.append((at, pid, p))
        else:
            (due if at <= now else waiting).append((at, pid, p))
    due.sort(key=lambda x: x[0])
    waiting.sort(key=lambda x: x[0])
    log(f"지금 {now:%m/%d %H:%M} · 올릴 차례 {len(due)} · 기다림 {len(waiting)} · 보류 {len(held)}")
    if held:
        log("보류 중: " + ", ".join(held))
    if waiting:
        summary("#### 기다리는 게시물\n" + "\n".join(f"- {a:%m/%d %H:%M} · {w} · {p.get('title', '')}" for a, w, p in waiting))

    for pid in dups:
        log(f"↩ {pid}: 이미 올린 게시물이라 다시 올리지 않아요" + ("" if MODE == "publish" else " (시험 실행이라 대기열은 그대로 둬요)"))
        if MODE == "publish":
            shutil.rmtree(os.path.join(QUEUE, pid))
        summary(f"### ↩ {pid}: 이미 올린 게시물이라 대기열에서 뺐어요\n"
                f"같은 날짜 폴더를 다시 올린 것 같아요. 일부러 한 번 더 올리려면 post.json에 \"repost\": true 를 넣어 주세요.")

    failed = False
    for at, pid, p in stale:
        reason = (f"예정 시각({at:%m/%d %H:%M})보다 {MAX_LATE_HOURS}시간 넘게 지나 자동 보류했어요 — "
                  f"그래도 올리려면 publish_at을 새로 정하고 hold를 false로 바꿔 주세요")
        log(f"⏸ {pid}: {reason}" + ("" if MODE == "publish" else " (시험 실행이라 표시만 하고 저장은 안 해요)"))
        if MODE == "publish":
            p["hold"], p["hold_reason"] = True, reason
            save_post(p)
            failed = True  # 알림 메일이 가도록 이번 실행은 실패로 표시해요 (다음부터는 보류로 조용히 넘어가요)
        summary(f"### ⏸ {pid} 자동 보류\n- {p.get('title', '')}\n- {reason}")

    if not due:
        return 1 if failed else 0

    at, pid, post = due[0]  # 한 번에 하나씩 (밀린 게 있으면 다음 실행에서 이어서)
    ig_id = os.environ.get("IG_USER_ID") or whoami().get("user_id")
    base = raw_base()
    log(f"게시 시작: {pid} (예정 {at:%m/%d %H:%M}) — {post.get('title', '')}")
    try:
        result = publish_post(post, ig_id, base)
    except ApiError as e:
        post["attempts"] = int(post.get("attempts", 0)) + 1
        post["last_error"] = str(e)[:500]
        post["last_try"] = now.isoformat(timespec="seconds")
        if post["attempts"] >= MAX_ATTEMPTS:
            post["hold"] = True
            post["hold_reason"] = f"{MAX_ATTEMPTS}번 실패해서 자동 보류했어요 — 원인을 고친 뒤 hold를 false로 바꾸면 다시 올려요"
        if MODE == "publish":
            save_post(post)
        summary(f"### ❌ {pid} 게시 실패 ({post['attempts']}회째)\n- 이유: {e}\n- {post.get('hold_reason', '다음 실행 때 다시 시도해요')}")
        log(f"❌ 실패: {e}")
        return 1
    if result is None:  # dry-run
        summary(f"### 🧪 시험 실행: {pid} — 이미지 주소 확인 완료 (게시 안 함)")
        return 1 if failed else 0
    post.update({"posted_at": now.isoformat(timespec="seconds"), **result})
    save_post(post)
    json.dump(result, open(os.path.join(post["_folder"], "result.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.makedirs(POSTED, exist_ok=True)
    dest = os.path.join(POSTED, pid)
    if os.path.exists(dest):
        dest = f"{dest}_{now:%H%M%S}"
    shutil.move(post["_folder"], dest)
    log(f"✅ 게시 완료: {pid} → {result.get('permalink')}")
    summary(f"### ✅ {pid} 게시 완료\n- {post.get('title', '')}\n- {result.get('permalink', '')}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
