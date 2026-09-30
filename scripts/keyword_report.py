#!/usr/bin/env python3
"""블로그 키워드 분석 — 네이버 공식 검색 데이터로 '지금 많이 찾는 글감' 순위표를 만들어요.

깃허브 액션(.github/workflows/keywords.yml)이 화·토요일 저녁에 실행해요.
결과: trends/latest.md(사람·Claude가 읽는 순위표), trends/latest.json, trends/history/<날짜>.json
일·수 아침 Claude 예약 작업이 trends/latest.md를 읽고 글감 순서와 제목에 참고해요.

쓰는 데이터 (둘 다 네이버 공식 API)
  · 검색광고 키워드 도구(필수): 기본 단어(하나씩 따로 조회)로 연관 키워드와 최근 30일 검색 수(PC+모바일)
  · 데이터랩 검색어 트렌드(선택): 상위 키워드의 최근 4주 일별 흐름(상대값)

직접 돌릴 때
  MODE=check python3 scripts/keyword_report.py   # 키 확인만 (파일 안 만듦)
  MODE=run   python3 scripts/keyword_report.py   # 분석해서 trends/ 에 저장

환경변수 (값은 저장소 비밀값에만 보관해요)
  SEARCHAD_CUSTOMER_ID, SEARCHAD_ACCESS_LICENSE, SEARCHAD_SECRET_KEY   (필수) 검색광고 → 도구 → API 사용 관리
  DATALAB_CLIENT_ID, DATALAB_CLIENT_SECRET                             (선택) API HUB 또는 예전 개발자센터 키
시험용
  SEARCHAD_BASE, DATALAB_URLS("이름=주소" 쉼표 목록), KEYWORD_NOW(YYYY-MM-DD), TRENDS_DIR, KEYWORD_DIR, KEYWORD_PAUSE
"""
import base64
import datetime
import glob
import hashlib
import hmac
import http.client
import json
import os
import re
import sys
import time
import urllib.parse

KST = datetime.timezone(datetime.timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYWORD_DIR = os.environ.get("KEYWORD_DIR") or os.path.join(ROOT, "keywords")
TRENDS = os.environ.get("TRENDS_DIR") or os.path.join(ROOT, "trends")
HISTORY = os.path.join(TRENDS, "history")
SEARCHAD_BASE = os.environ.get("SEARCHAD_BASE", "https://api.searchad.naver.com").rstrip("/")
MODE = (os.environ.get("MODE") or "run").strip().lower()
EVENT = os.environ.get("GITHUB_EVENT_NAME", "")
PAUSE = float(os.environ.get("KEYWORD_PAUSE", "0.4"))
WEEKDAYS = "월화수목금토일"

# 데이터랩 검색어 트렌드 주소 — 2026-07-31부터 새 키는 API HUB에서만 받아요. 예전 키도 되도록 차례로 시도해요.
DATALAB_ENDPOINTS = [
    ("API HUB", "https://naverapihub.apigw.ntruss.com/search-trend/v1/search",
     "X-NCP-APIGW-API-KEY-ID", "X-NCP-APIGW-API-KEY"),
    ("개발자센터", "https://openapi.naver.com/v1/datalab/search",
     "X-Naver-Client-Id", "X-Naver-Client-Secret"),
    ("네이버 클라우드", "https://naveropenapi.apigw.ntruss.com/datalab/v1/search",
     "X-NCP-APIGW-API-KEY-ID", "X-NCP-APIGW-API-KEY"),
]


class ApiError(Exception):
    def __init__(self, where, status, code=None, message="", rid=""):
        self.where, self.status, self.code, self.message, self.rid = where, status, code, message, rid
        super().__init__(f"{where} HTTP {status}" + (f" (코드 {code})" if code else "") + (f": {message}" if message else "")
                         + (f" [요청 ID {rid}]" if rid else ""))

    def auth(self):
        return self.status in (401, 403)


def log(msg):
    print(msg, flush=True)


def summary(md):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def norm(s):
    return re.sub(r"\s+", "", str(s)).lower()


def num(v):
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "").strip()
    if s.startswith("<"):
        return 5  # "< 10" → 대략 5로 봐요
    try:
        return int(float(s))
    except ValueError:
        return 0


def is_tiny(v):
    """키워드 도구가 숫자 대신 '< 10'을 준 칸이에요."""
    return isinstance(v, str) and v.strip().startswith("<")


def vol_label(x):
    """표에 적을 검색 수. PC·모바일 둘 다 '< 10'이면 '10 미만'으로 적어요."""
    return "10 미만" if x.get("tiny") else fmt(x["volume"])


def name_label(x):
    """대표 키워드. 묶는 기준이 된 더 짧은 말이 따로 있으면 함께 적어요 — 예: 부가세계산기 (부가세 묶음)"""
    return f"{x['keyword']} ({x['root']} 묶음)" if x.get("root") else x["keyword"]


def fmt(n):
    return f"{n:,}"


def call(method, url, headers=None, body=None, timeout=30):
    """HTTP 요청 → (상태, 본문, 요청 ID). 헤더 이름을 적은 그대로(X-API-KEY 등 대소문자 유지) 보내요.
    urllib은 헤더 이름을 'X-api-key'처럼 바꿔 보내서, 이름을 정확히 요구하는 서버에서 인증이 실패할 수 있어요."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    u = urllib.parse.urlsplit(url)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    hdrs = {"User-Agent": "alzza-keywords/1.2", "Accept": "application/json"}
    hdrs.update(headers or {})
    try:
        conn = conn_cls(u.hostname, u.port, timeout=timeout)
        try:
            conn.request(method, path, body=data, headers=hdrs)
            r = conn.getresponse()
            text = r.read().decode("utf-8", "replace")
            rid = next((v for k, v in r.getheaders()
                        if "transaction" in k.lower() or k.lower() in ("x-request-id", "x-ncp-trace-id")), "")
            return r.status, text, rid
        finally:
            conn.close()
    except (OSError, http.client.HTTPException) as e:
        return 0, str(e), ""


def error_parts(text):
    try:
        j = json.loads(text)
    except ValueError:
        return None, text.strip()[:200]
    if not isinstance(j, dict):
        return None, text.strip()[:200]
    err = j.get("error") if isinstance(j.get("error"), dict) else {}
    code = j.get("code") or j.get("errorCode") or err.get("errorCode") or err.get("code")
    parts = []  # 서버가 준 설명을 빠짐없이 (title · detail · message …)
    for v in (j.get("title"), j.get("message"), j.get("errorMessage"), j.get("detail"),
              err.get("message"), err.get("details")):
        if v and str(v) not in parts:
            parts.append(str(v))
    try:
        code = int(code)
    except (TypeError, ValueError):
        pass
    return code, " — ".join(parts)[:300]


# ── 검색광고 API (키워드 도구) ───────────────────────────────────────────────

class SearchAd:
    def __init__(self, customer, license_, secret):
        self.customer, self.license, self.secret = customer, license_, secret
        self.calls = 0

    def _headers(self, method, uri):
        ts = str(int(time.time() * 1000))
        msg = f"{ts}.{method}.{uri}".encode("utf-8")
        sig = base64.b64encode(hmac.new(self.secret.encode("utf-8"), msg, hashlib.sha256).digest()).decode()
        return {"X-Timestamp": ts, "X-API-KEY": self.license, "X-Customer": self.customer,
                "X-Signature": sig, "Content-Type": "application/json; charset=UTF-8"}

    def keywords(self, hints):
        """기본 단어(최대 5개) → 연관 키워드 목록 [{relKeyword, monthlyPcQcCnt, monthlyMobileQcCnt, compIdx…}]"""
        uri = "/keywordstool"
        query = urllib.parse.urlencode({"hintKeywords": ",".join(hints), "showDetail": "1"})
        wait = 1.0
        for attempt in range(6):
            self.calls += 1
            status, text, rid = call("GET", f"{SEARCHAD_BASE}{uri}?{query}", self._headers("GET", uri))
            if status == 200:
                try:
                    j = json.loads(text)
                except ValueError:
                    raise ApiError("검색광고", status, None, "응답을 읽을 수 없어요")
                rows = j.get("keywordList", []) if isinstance(j, dict) else j
                return rows if isinstance(rows, list) else []
            code, msg = error_parts(text)
            if status == 429 or code == 1016 or status == 0 or status >= 500:
                time.sleep(wait)
                wait = min(wait * 2, 16)
                continue
            raise ApiError("검색광고", status, code, msg, rid)
        raise ApiError("검색광고", status, code, msg or "여러 번 다시 시도했지만 응답이 없어요", rid)


# ── 데이터랩 검색어 트렌드 ────────────────────────────────────────────────────

def datalab_endpoints():
    custom = os.environ.get("DATALAB_URLS", "").strip()
    if not custom:
        return list(DATALAB_ENDPOINTS)
    out = []
    for part in custom.split(","):
        name, _, url = part.partition("=")
        base = next((e for e in DATALAB_ENDPOINTS if e[0] == name.strip()), DATALAB_ENDPOINTS[0])
        out.append((base[0], url.strip(), base[2], base[3]))
    return out


class DataLab:
    def __init__(self, client_id, client_secret):
        self.id, self.secret = client_id, client_secret
        self.endpoints = datalab_endpoints()
        self.used = None
        self.calls = 0

    def trend(self, keywords, start, end):
        """키워드(최대 5개)의 일별 상대값 {키워드: {날짜: 비율}}"""
        body = {"startDate": start, "endDate": end, "timeUnit": "date",
                "keywordGroups": [{"groupName": k, "keywords": [k]} for k in keywords]}
        tried = []
        for ep in ([self.used] if self.used else self.endpoints):
            name, url, hid, hsecret = ep
            wait = 1.0
            for attempt in range(4):
                self.calls += 1
                status, text, rid = call("POST", url, {hid: self.id, hsecret: self.secret,
                                                       "Content-Type": "application/json"}, body)
                if status == 200:
                    self.used = ep
                    out = {}
                    for res in json.loads(text).get("results", []):
                        out[res.get("title")] = {d["period"]: float(d.get("ratio") or 0) for d in res.get("data", [])}
                    return out
                if status in (429, 500, 502, 503, 504, 0):
                    time.sleep(wait)
                    wait *= 2
                    continue
                break
            code, msg = error_parts(text)
            tried.append(f"{name} {status}" + (f"({code})" if code else "") + (f" {msg}" if msg else ""))
        raise ApiError("데이터랩", status, None, " / ".join(tried))


def trend_stats(series, start, days):
    """28일 일별 값 → 최근 7일 평균 ÷ 그 전 21일 평균, 4주 흐름"""
    values = []
    d0 = datetime.date.fromisoformat(start)
    for i in range(days):
        values.append(series.get((d0 + datetime.timedelta(days=i)).isoformat(), 0.0))
    recent, base = values[-7:], values[:-7]
    r = sum(recent) / len(recent)
    b = sum(base) / len(base) if base else 0.0
    if b < 0.5:
        rise = 9.99 if r >= 1 else None
    else:
        rise = round(r / b, 2)
    weeks = [sum(values[i:i + 7]) / 7 for i in range(0, days - days % 7, 7)]
    top = max(weeks) if weeks and max(weeks) > 0 else 0
    bars = "▁▂▃▄▅▆▇█"
    spark = "".join(bars[min(7, int(w / top * 7))] if top else "▁" for w in weeks)
    return {"rise": rise, "spark": spark}


def trend_label(rise):
    if rise is None:
        return "–"
    if rise >= 2:
        return f"▲▲ {rise:.1f}배"
    if rise >= 1.3:
        return f"▲ {rise:.1f}배"
    if rise <= 0.7:
        return f"▼ {rise:.1f}배"
    return f"– {rise:.1f}배"


# ── 설정·분류 ────────────────────────────────────────────────────────────────

def load_config():
    with open(os.path.join(KEYWORD_DIR, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    watch_files = sorted(glob.glob(os.path.join(KEYWORD_DIR, "watch-*.txt")))
    watch, watch_name = [], None
    if watch_files:
        watch_name = os.path.basename(watch_files[-1])
        with open(watch_files[-1], encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    watch.append(re.sub(r"\s+", "", line))
    cfg["watch"], cfg["watch_file"] = watch, watch_name
    return cfg


def _rules(cfg):
    """분류 규칙을 한 번만 정리해 둬요 (키워드가 수만 개라도 빠르게)."""
    if "_rules" not in cfg:
        excl = [(t, norm(t), [norm(x) for x in cfg.get("exclude_unless", {}).get(t, [])])
                for t in cfg.get("exclude", [])]
        cats = []
        for cat, terms in cfg.get("categories", {}).items():
            for term in terms:
                if isinstance(term, list):
                    cats.append((cat, norm(term[0]), [norm(x) for x in term[1:]]))
                else:
                    cats.append((cat, norm(term), None))
        cfg["_rules"] = (excl, cats)
    return cfg["_rules"]


def classify(keyword, cfg):
    n = norm(keyword)
    excl, cats = _rules(cfg)
    for term, t, rescue in excl:
        if t in n and not any(r in n for r in rescue):
            return None, f"제외어 '{term}'"
    for cat, t, extra in cats:
        if t in n and (extra is None or any(x in n for x in extra)):
            return cat, None
    return None, "주제 밖"


def unique(items):
    seen, out = set(), []
    for x in items:
        k = norm(x)
        if k and k not in seen:
            seen.add(k)
            out.append(x)
    return out


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def is_variant(head, keyword):
    """'근로장려금지급일'은 '근로장려금'의 긴 검색어예요. 두 글자 단어(청약·수당)는 너무 넓어서 묶지 않아요."""
    h, k = norm(head), norm(keyword)
    return len(h) >= 3 and len(k) > len(h) and k.startswith(h)


def group_families(items, limit):
    """검색 수가 많은 순서로 들어온 키워드를 묶음으로 모아요 (같은 말이 표를 채우지 않게).
    · 묶는 기준(root): 묶음에서 가장 짧은 공통 머리말 — 예: 고향사랑기부
    · 대표 키워드(head): 묶음에서 검색이 가장 많은 말 — 예: 고향사랑기부제(10만) ← 표의 순위·검색 수는 이 말 기준
    예전에는 더 짧은 말이 대표가 되면서(고향사랑기부 7천) 많이 찾는 묶음이 아래로 밀렸어요."""
    def var(h, k):  # 둘 다 정리된 말(띄어쓰기 없음·소문자)
        return len(h) >= 3 and len(k) > len(h) and k.startswith(h)

    fams = []
    for x in items:
        k = x.get("key") or norm(x["keyword"])
        hits = [f for f in fams if var(f["rkey"], k) or var(k, f["rkey"])]
        if not hits:
            if len(fams) < limit:
                fams.append({"root": x["keyword"], "rkey": k, "head": x, "members": []})
            continue
        first = hits[0]
        for f in hits[1:]:  # 더 짧은 말이 들어와 여러 묶음을 잇는 경우
            first["members"] += [f["head"]] + f["members"]
            fams.remove(f)
        first["members"].append(x)
        if len(k) < len(first["rkey"]):
            first["root"], first["rkey"] = x["keyword"], k
        everyone = [first["head"]] + first["members"]
        best = max(everyone, key=lambda m: m["volume"])  # 같으면 먼저 온 말
        if best is not first["head"]:
            first["members"] = [m for m in everyone if m is not best]
            first["head"] = best
    return fams


def family_tails(fam, ranked, min_volume=0):
    """묶음(root로 시작하는 말) 가운데 대표 키워드와 그 앞부분을 뺀 검색어 — 검색 수 많은 순.
    rising 묶음은 오른 말만 모여 있어서, 전체 목록(ranked)에서 다시 찾아요."""
    r, h = norm(fam["root"]), norm(fam["head"]["keyword"])
    if len(r) < 3:
        return []
    return [x for x in ranked if x["key"].startswith(r) and x["key"] != h
            and not h.startswith(x["key"]) and x["volume"] >= min_volume]


# ── 수집 ────────────────────────────────────────────────────────────────────

def collect(sa, hints, pool, problems, src="base", per=1):
    """기본 단어를 per개씩(기본 1개) 조회해서 pool 에 모아요. 인증 오류는 바로 멈추고, 나머지 오류는 기록만 해요.
    여러 단어를 한 번에 넣으면 그중 한 단어의 연관 키워드만 잔뜩 오고 나머지는 거의 안 와서(2026-09-30 첫 분석),
    단어마다 따로 조회해요."""
    for group in chunks(hints, max(1, min(5, per))):
        try:
            rows = sa.keywords(group)
        except ApiError as e:
            if e.auth():
                raise
            if e.status == 400 and len(group) > 1:  # 문제 있는 단어만 골라내요
                for h in group:
                    try:
                        merge(pool, sa.keywords([h]), src)
                    except ApiError as e2:
                        if e2.auth():
                            raise
                        problems.append(f"'{h}': {e2}")
                    time.sleep(PAUSE)
                continue
            problems.append(f"{', '.join(group)}: {e}")
            continue
        merge(pool, rows, src)
        time.sleep(PAUSE)


def merge(pool, rows, src="base"):
    for r in rows:
        kw = str(r.get("relKeyword") or "").strip()
        if not kw:
            continue
        raw_pc, raw_mo = r.get("monthlyPcQcCnt"), r.get("monthlyMobileQcCnt")
        pc, mo = num(raw_pc), num(raw_mo)
        tiny = is_tiny(raw_pc) and is_tiny(raw_mo)
        key = norm(kw)
        cur = pool.get(key)
        if cur is None:
            pool[key] = {"keyword": kw, "key": key, "pc": pc, "mobile": mo, "volume": pc + mo, "tiny": tiny,
                         "comp": str(r.get("compIdx") or ""), "src": src}
        elif pc + mo > cur["volume"]:
            cur.update({"pc": pc, "mobile": mo, "volume": pc + mo, "tiny": tiny})


# ── 기록 ────────────────────────────────────────────────────────────────────

def load_previous(today):
    files = sorted(glob.glob(os.path.join(HISTORY, "*.json")))
    for path in reversed(files):
        date = os.path.basename(path)[:-5]
        if date < today:
            try:
                with open(path, encoding="utf-8") as f:
                    j = json.load(f)
                return date, j.get("volumes", {}), j.get("hints_hash")
            except (OSError, ValueError):
                continue
    return None, {}, None


def hints_hash(hints, per=1):
    """기본 단어 목록 + 조회 방식. 둘 중 하나라도 바뀌면 다음 분석에서 '새로 등장'을 한 번 쉬어요
    (모이는 연관 키워드가 달라져서, 새로 보이는 말이 진짜 새 검색어인지 알 수 없어요)."""
    base = ",".join(sorted(norm(h) for h in hints))
    if per != 5:  # 2026-09-30 첫 분석(5개씩)과 같은 방식이면 예전과 같은 값
        base = f"per{per}|{base}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:12]


def prune_history(keep):
    files = sorted(glob.glob(os.path.join(HISTORY, "*.json")))
    for path in files[:-keep] if keep > 0 else []:
        os.remove(path)


# ── 분석 ────────────────────────────────────────────────────────────────────

def analyze(cfg, sa, dl, now):
    today = now.date().isoformat()
    hints = unique(cfg.get("seeds", []) + cfg.get("watch", []))
    hint_keys = {norm(h) for h in hints}
    per = int(cfg.get("hints_per_call", 1) or 1)
    pool, problems = {}, []
    collect(sa, hints, pool, problems, per=per)
    if not pool:
        raise ApiError("검색광고", 200, None, "연관 키워드를 하나도 받지 못했어요 — " + "; ".join(problems[:3]))

    def classify_all():
        for item in pool.values():
            if "category" not in item:
                item["category"], item["reason"] = classify(item["keyword"], cfg)

    classify_all()
    ranked = sorted((x for x in pool.values() if x["category"]), key=lambda x: -x["volume"])
    # 많이 찾는 묶음 가운데 기본 단어에 없는 것(예: 부가세)을 한 번 더 조회해서 긴 검색어를 모아요
    extra = []
    for f in group_families(ranked, cfg.get("expand_top", 10) * 3):
        if norm(f["root"]) in hint_keys or norm(f["head"]["keyword"]) in hint_keys:
            continue
        extra.append(f["root"])
    extra = extra[:cfg.get("expand_top", 10)]
    if extra:
        collect(sa, extra, pool, problems, src="extra", per=per)
        classify_all()
        ranked = sorted((x for x in pool.values() if x["category"]), key=lambda x: -x["volume"])

    prev_date, prev, prev_hash = load_previous(today)
    hh = hints_hash(hints, per)
    # 기본 단어·캘린더 글감·조회 방식이 바뀐 직후에는 '새로 등장'이 그 변화 때문일 수 있어 표시하지 않아요
    new_ok = bool(prev_date) and prev_hash == hh
    for x in pool.values():
        old = prev.get(x["key"])
        # '10 미만'(도구가 수치를 안 준 칸)이 끼면 변화율을 믿을 수 없어서 비워 둬요
        ok = old and old > 10 and not x["tiny"]
        x["delta"] = round(x["volume"] / old - 1, 3) if ok else None
        # 추가 조회로만 모인 키워드는 매번 달라질 수 있어 '새로 등장'으로 보지 않아요
        x["is_new"] = new_ok and old is None and x.get("src") == "base" and not x["tiny"]

    top_fams = group_families(ranked, cfg.get("top_n", 30))
    top_fams.sort(key=lambda f: -f["head"]["volume"])

    # 데이터랩 (선택): 많이 찾는 대표 키워드 + 캘린더 글감의 최근 4주 흐름
    datalab = {"used": None, "error": None, "start": None, "end": None}
    if dl:
        end = now.date() - datetime.timedelta(days=1)
        start = end - datetime.timedelta(days=27)
        datalab["start"], datalab["end"] = start.isoformat(), end.isoformat()
        targets = unique([f["head"]["keyword"] for f in top_fams[:cfg.get("datalab_top", 25)]]
                         + [w for w in cfg.get("watch", []) if norm(w) in pool])
        try:
            for group in chunks(targets, 5):
                res = dl.trend(group, start.isoformat(), end.isoformat())
                for kw in group:
                    if kw in res:
                        pool[norm(kw)]["trend"] = trend_stats(res[kw], start.isoformat(), 28)
                time.sleep(PAUSE)
            datalab["used"] = dl.used[0] if dl.used else None
        except ApiError as e:
            datalab["error"] = str(e)
            datalab["used"] = dl.used[0] if dl.used else None

    def rising_score(x):
        rise = (x.get("trend") or {}).get("rise") or 0
        delta = x["delta"] or 0
        return (min(rise, 5) if rise else 0) + delta * 4 + (0.8 if x["is_new"] else 0)

    min_vol = cfg.get("rising_min_volume", 500)
    rising = [x for x in ranked if x["volume"] >= min_vol and (
        ((x.get("trend") or {}).get("rise") or 0) >= 1.3
        or (x["delta"] is not None and x["delta"] >= cfg.get("rising_delta", 0.15))
        or (x["is_new"] and x["volume"] >= cfg.get("new_min_volume", 1000)))]
    rising = sorted(rising, key=lambda x: (-rising_score(x), -x["volume"]))
    rising_fams = group_families(rising, cfg.get("rising_limit", 15))

    # 묶음마다 '같은 말로 시작하는 다른 검색어' 수 (표의 '긴 검색어' 칸)
    for f in rising_fams + top_fams:
        f["related"] = len(family_tails(f, ranked))

    # 제목에 쓸 만한 긴 검색어: 묶음에서 대표 키워드 다음으로 많이 찾는 말 (너무 적게 찾는 말은 빼요)
    n_tails = cfg.get("longtail_per_keyword", 3)
    tail_min = cfg.get("longtail_min_volume", 100)
    longtail, seen = [], set()
    for f in rising_fams[:8] + top_fams[:cfg.get("longtail_families", 15)]:
        h = f["head"]["key"]
        if h in seen:
            continue
        seen.add(h)
        tails = family_tails(f, ranked, tail_min)[:n_tails]
        if tails:
            item = {"keyword": f["head"]["keyword"],
                    "tails": [{"keyword": t["keyword"], "volume": t["volume"]} for t in tails]}
            if norm(f["root"]) != h:
                item["root"] = f["root"]
            longtail.append(item)

    watch = []
    for w in cfg.get("watch", []):
        x = pool.get(norm(w))
        watch.append({"keyword": w, "volume": x["volume"] if x else None, "tiny": bool(x and x["tiny"]),
                      "delta": x["delta"] if x else None, "is_new": x["is_new"] if x else False,
                      "trend": x.get("trend") if x else None})

    excluded = sorted((x for x in pool.values() if not x["category"] and x["key"] not in hint_keys),
                      key=lambda x: -x["volume"])[:cfg.get("excluded_show", 10)]

    history = {x["keyword"]: x["volume"] for x in ranked[:cfg.get("history_size", 2000)] if x["volume"] >= 100}
    for w in watch:
        if w["volume"] is not None:
            history.setdefault(w["keyword"], w["volume"])

    return {
        "version": 1,
        "generated_at": now.isoformat(timespec="minutes"),
        "date": today,
        "previous_date": prev_date,
        "hints_hash": hh,
        "new_flag": new_ok,
        "watch_file": cfg.get("watch_file"),
        "counts": {"hints": len(hints), "extra": len(extra), "hints_per_call": per, "pool": len(pool),
                   "in_topic": len(ranked), "searchad_calls": sa.calls, "datalab_calls": dl.calls if dl else 0},
        "datalab": datalab,
        "problems": problems,
        "rising": [slim(f) for f in rising_fams],
        "top": [slim(f) for f in top_fams],
        "watch": watch,
        "longtail": longtail,
        "excluded": [{"keyword": x["keyword"], "volume": x["volume"], "reason": x["reason"]} for x in excluded],
        "_history": {norm(k): v for k, v in history.items()},
    }


def slim(fam):
    x = fam["head"]
    out = {"keyword": x["keyword"], "category": x["category"], "volume": x["volume"],
           "pc": x["pc"], "mobile": x["mobile"], "delta": x["delta"], "is_new": x["is_new"],
           "related": fam.get("related", 0)}
    if x.get("tiny"):
        out["tiny"] = True
    if norm(fam["root"]) != x["key"]:
        out["root"] = fam["root"]  # 묶는 기준이 된 더 짧은 말 (예: 고향사랑기부)
    if x.get("trend"):
        out["trend"] = x["trend"]
    return out


# ── 쓰기 ────────────────────────────────────────────────────────────────────

def delta_label(x):
    if x.get("is_new"):
        return "새로 등장"
    d = x.get("delta")
    if d is None:
        return "–"
    return f"{d * 100:+.0f}%"


def trend_cells(x):
    t = x.get("trend")
    if not t:
        return "–", "–"
    return trend_label(t.get("rise")), t.get("spark") or "–"


def render_md(rep):
    now = datetime.datetime.fromisoformat(rep["generated_at"])
    c, dl = rep["counts"], rep["datalab"]
    lines = [f"# 블로그 키워드 순위표 — {rep['date']}({WEEKDAYS[now.weekday()]}) {now.strftime('%H:%M')}", "",
             "> 네이버 검색광고 키워드 도구(최근 30일 검색 수)와 데이터랩 검색어 트렌드(최근 4주 흐름)로 만든 참고 표예요.",
             "> 글감은 이 표만 보고 정하지 않아요. 공식 출처를 확인한 뒤 발행 캘린더 규칙에 맞춰 골라요.", ""]
    if rep["previous_date"]:
        gap = (datetime.date.fromisoformat(rep["date"]) - datetime.date.fromisoformat(rep["previous_date"])).days
        lines.append(f"- 비교 기준: {rep['previous_date']} 분석({gap}일 전) — '지난번 대비'는 최근 30일 검색 수의 변화예요")
        if not rep.get("new_flag"):
            lines.append("- 기본 단어·캘린더 글감·조회 방식이 바뀐 뒤 첫 분석이라 이번에는 '새로 등장'을 표시하지 않아요")
    else:
        lines.append("- 첫 분석이라 '지난번 대비'는 다음 분석부터 나와요")
    how = "단어마다 따로 조회" if c.get("hints_per_call", 5) == 1 else f"{c.get('hints_per_call', 5)}개씩 조회"
    lines.append(f"- 조회: 기본 단어 {c['hints']}개 + 추가 {c['extra']}개({how}) → 연관 키워드 {fmt(c['pool'])}개 중 블로그 주제에 맞는 {fmt(c['in_topic'])}개")
    if dl["used"] and not dl["error"]:
        lines.append(f"- 데이터랩: 연결됨({dl['used']}) · {dl['start']}~{dl['end']} · '최근 1주'는 그 전 3주 평균 대비 배수")
    elif dl["error"]:
        lines.append(f"- 데이터랩: 일부 또는 전체 실패 — {dl['error']}")
    else:
        lines.append("- 데이터랩: 키 없음(선택 기능) — '최근 1주' 칸은 비어 있어요")
    if rep["problems"]:
        lines.append(f"- 조회하지 못한 단어: {'; '.join(rep['problems'][:5])}")
    lines += ["- 같은 말로 시작하는 검색어는 한 줄로 묶어요. '키워드'는 묶음에서 가장 많이 찾는 말이에요(더 짧은 기준 말이 따로 있으면 '(부가세 묶음)'처럼 적어요). '긴 검색어'는 묶음의 다른 검색어 수이고(예: 근로장려금 → 근로장려금지급일), 4장에 많이 찾는 것을 적었어요",
              "", "## 1. 뜨는 키워드", ""]
    if rep["rising"]:
        lines += ["| 키워드 | 분류 | 월 검색수 | 지난번 대비 | 최근 1주 | 4주 흐름 | 긴 검색어 |", "|---|---|---:|---:|---|---|---:|"]
        for x in rep["rising"]:
            t, s = trend_cells(x)
            lines.append(f"| {name_label(x)} | {x['category']} | {vol_label(x)} | {delta_label(x)} | {t} | {s} | {x.get('related', 0)} |")
    else:
        lines.append("아직 없어요. (첫 분석이거나 크게 오른 키워드가 없어요)")
    lines += ["", f"## 2. 많이 찾는 키워드 TOP {len(rep['top'])}", "",
              "| 순위 | 키워드 | 분류 | 월 검색수 | 지난번 대비 | 최근 1주 | 긴 검색어 |", "|---:|---|---|---:|---:|---|---:|"]
    for i, x in enumerate(rep["top"], 1):
        t, _ = trend_cells(x)
        lines.append(f"| {i} | {name_label(x)} | {x['category']} | {vol_label(x)} | {delta_label(x)} | {t} | {x.get('related', 0)} |")
    if rep["watch"]:
        lines += ["", f"## 3. 캘린더 글감 확인 ({rep['watch_file']})", "",
                  "| 키워드 | 월 검색수 | 지난번 대비 | 최근 1주 | 4주 흐름 |", "|---|---:|---:|---|---|"]
        for w in rep["watch"]:
            if w["volume"] is None:
                lines.append(f"| {w['keyword']} | 데이터 없음 | – | – | – |")
                continue
            t, s = trend_cells(w)
            lines.append(f"| {w['keyword']} | {vol_label(w)} | {delta_label(w)} | {t} | {s} |")
        if any(w.get("tiny") or w["volume"] is None for w in rep["watch"]):
            lines += ["", "'10 미만'·'데이터 없음'은 키워드 도구가 그 말의 검색 수를 주지 않은 칸이에요. "
                          "실제로는 많이 찾는 말도 이렇게 나올 때가 있어서, 이 칸만 보고 글감을 미루거나 빼지 않아요."]
    if rep["longtail"]:
        lines += ["", "## 4. 제목에 쓸 만한 긴 검색어", "",
                  "키워드 도구는 띄어쓰기 없이 보여 줘요. 제목·태그에는 자연스럽게 띄어 쓰고, 같은 말을 반복하지 않아요.", ""]
        for lt in rep["longtail"]:
            tails = " · ".join(f"{t['keyword']}({fmt(t['volume'])})" for t in lt["tails"])
            lines.append(f"- **{name_label(lt)}** → {tails}")
    if rep["excluded"]:
        lines += ["", "## 5. 주제 밖이라 뺀 인기 키워드", "",
                  " · ".join(f"{x['keyword']}({fmt(x['volume'])}, {x['reason']})" for x in rep["excluded"])]
    lines += ["", "---", "설정: `keywords/config.json`(기본 단어·분류·제외어), `keywords/watch-*.txt`(캘린더 글감) · 만든 스크립트: `scripts/keyword_report.py`", ""]
    return "\n".join(lines)


def write_outputs(rep):
    os.makedirs(HISTORY, exist_ok=True)
    history = rep.pop("_history")
    with open(os.path.join(HISTORY, f"{rep['date']}.json"), "w", encoding="utf-8") as f:
        json.dump({"date": rep["date"], "generated_at": rep["generated_at"], "hints_hash": rep["hints_hash"],
                   "volumes": history},
                  f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        f.write("\n")
    with open(os.path.join(TRENDS, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
        f.write("\n")
    with open(os.path.join(TRENDS, "latest.md"), "w", encoding="utf-8") as f:
        f.write(render_md(rep))


def step_summary(rep):
    lines = [f"### 🔎 블로그 키워드 순위표 {rep['date']} — 주제에 맞는 키워드 {fmt(rep['counts']['in_topic'])}개"]
    if rep["rising"]:
        lines.append("**뜨는 키워드**: " + " · ".join(
            f"{x['keyword']}({delta_label(x)}{', ' + trend_cells(x)[0] if x.get('trend') else ''})" for x in rep["rising"][:8]))
    lines.append("**많이 찾는 키워드**: " + " · ".join(f"{x['keyword']}({fmt(x['volume'])})" for x in rep["top"][:10]))
    dl = rep["datalab"]
    if dl["error"]:
        lines.append(f"- ⚠ 데이터랩: {dl['error']}")
    elif not dl["used"]:
        lines.append("- ℹ 데이터랩 키 없음(선택) — 검색 수만으로 만들었어요")
    if rep["problems"]:
        lines.append(f"- ⚠ 조회하지 못한 단어 {len(rep['problems'])}건: {'; '.join(rep['problems'][:3])}")
    lines.append("- 전체 표: `trends/latest.md`")
    summary("\n".join(lines))


# ── 실행 ────────────────────────────────────────────────────────────────────

def auth_hint(e):
    if e.where == "검색광고" and e.auth():
        return ("검색광고 키가 맞지 않아요. 네이버 광고 → 도구 → API 사용 관리 화면의 값으로 비밀값을 다시 넣어 주세요: "
                "SEARCHAD_CUSTOMER_ID = CUSTOMER_ID(숫자), SEARCHAD_ACCESS_LICENSE = 액세스라이선스, "
                "SEARCHAD_SECRET_KEY = 비밀키('보기'를 누른 뒤 복사). 라이선스와 비밀키가 서로 바뀌지 않았는지, "
                "라이선스를 다시 발급했다면 예전 값은 못 쓰니 새 값으로 넣었는지 확인해 주세요.")
    return ""


def now_kst():
    fixed = os.environ.get("KEYWORD_NOW", "").strip()
    if fixed:
        d = datetime.datetime.fromisoformat(fixed)
        return d if d.tzinfo else d.replace(hour=d.hour or 20, tzinfo=KST)
    return datetime.datetime.now(KST)


def main():
    cid = os.environ.get("SEARCHAD_CUSTOMER_ID", "").strip()
    lic = os.environ.get("SEARCHAD_ACCESS_LICENSE", "").strip()
    sec = os.environ.get("SEARCHAD_SECRET_KEY", "").strip()
    did = os.environ.get("DATALAB_CLIENT_ID", "").strip()
    dsec = os.environ.get("DATALAB_CLIENT_SECRET", "").strip()
    if not (cid and lic and sec):
        missing = [n for n, v in (("SEARCHAD_CUSTOMER_ID", cid), ("SEARCHAD_ACCESS_LICENSE", lic),
                                  ("SEARCHAD_SECRET_KEY", sec)) if not v]
        if EVENT == "schedule":
            log("검색광고 키가 아직 없어서 이번 분석은 건너뛰어요")
            summary("ℹ 검색광고 키가 아직 없어서 키워드 분석을 건너뛰었어요 (비밀값을 넣으면 다음 예약부터 돌아가요)")
            return 0
        summary(f"### ❌ 검색광고 키가 없어요\n저장소 Settings → Secrets and variables → Actions 에 {', '.join(missing)} 를 넣어 주세요.")
        log(f"❌ 비밀값 없음: {', '.join(missing)}")
        return 1
    sa = SearchAd(cid, lic, sec)
    dl = DataLab(did, dsec) if (did and dsec) else None
    now = now_kst()

    if MODE == "check":
        lines = ["### 🔑 키워드 분석 키 확인"]
        try:
            rows = sa.keywords(["지원금"])
            lines.append(f"- ✅ 검색광고 키 정상 — '지원금' 연관 키워드 {len(rows)}개를 받았어요")
        except ApiError as e:
            lines.append(f"- ❌ 검색광고: {e}")
            hint = auth_hint(e)
            if hint:
                lines.append(f"  - {hint}")
            summary("\n".join(lines))
            log("\n".join(lines))
            return 1
        if dl:
            end = now.date() - datetime.timedelta(days=1)
            start = end - datetime.timedelta(days=6)
            try:
                dl.trend(["지원금"], start.isoformat(), end.isoformat())
                lines.append(f"- ✅ 데이터랩 연결 정상 ({dl.used[0]})")
            except ApiError as e:
                lines.append(f"- ⚠ 데이터랩 연결 실패 — {e.message or e}")
                lines.append("  - 선택 기능이라 없어도 순위표는 만들어져요. 키(Client ID·Secret)와 '검색어 트렌드' 신청 여부를 확인해 주세요.")
        else:
            lines.append("- ℹ 데이터랩 키 없음 — 선택 기능이에요 (없으면 '최근 1주' 칸만 비어요)")
        lines.append("- 준비가 끝났으면 Run workflow에서 `run`을 골라 첫 순위표를 만들어 보세요.")
        summary("\n".join(lines))
        log("\n".join(lines))
        return 0

    cfg = load_config()
    try:
        rep = analyze(cfg, sa, dl, now)
    except ApiError as e:
        hint = auth_hint(e)
        summary(f"### ❌ 키워드 분석 실패\n{e}" + (f"\n\n{hint}" if hint else ""))
        log(f"❌ {e}")
        return 1
    os.makedirs(TRENDS, exist_ok=True)
    write_outputs(rep)
    prune_history(cfg.get("history_keep", 24))
    step_summary(rep)
    log(f"✅ 순위표 저장: 주제 키워드 {rep['counts']['in_topic']}개, 뜨는 키워드 {len(rep['rising'])}개, "
        f"검색광고 {rep['counts']['searchad_calls']}회·데이터랩 {rep['counts']['datalab_calls']}회 호출")
    return 0


if __name__ == "__main__":
    sys.exit(main())
