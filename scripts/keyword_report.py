#!/usr/bin/env python3
"""블로그 키워드 분석 — 네이버 공식 검색 데이터로 '지금 많이 찾는 글감' 순위표를 만들어요.

깃허브 액션(.github/workflows/keywords.yml)이 화·토요일 저녁에 실행해요.
결과: trends/latest.md(사람·Claude가 읽는 순위표), trends/latest.json, trends/history/<날짜>.json
일·수 아침 Claude 예약 작업이 trends/latest.md를 읽고 글감 순서와 제목에 참고해요.

쓰는 데이터 (둘 다 네이버 공식 API)
  · 검색광고 키워드 도구(필수): 기본 단어로 연관 키워드와 최근 30일 검색 수(PC+모바일)
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


def fmt(n):
    return f"{n:,}"


def call(method, url, headers=None, body=None, timeout=30):
    """HTTP 요청 → (상태, 본문, 요청 ID). 헤더 이름을 적은 그대로(X-API-KEY 등 대소문자 유지) 보내요.
    urllib은 헤더 이름을 'X-api-key'처럼 바꿔 보내서, 이름을 정확히 요구하는 서버에서 인증이 실패할 수 있어요."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    u = urllib.parse.urlsplit(url)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    hdrs = {"User-Agent": "alzza-keywords/1.1", "Accept": "application/json"}
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


def classify(keyword, cfg):
    n = norm(keyword)
    for term in cfg.get("exclude", []):
        if norm(term) in n:
            rescue = [norm(x) for x in cfg.get("exclude_unless", {}).get(term, [])]
            if not any(r in n for r in rescue):
                return None, f"제외어 '{term}'"
    for cat, terms in cfg.get("categories", {}).items():
        for term in terms:
            if isinstance(term, list):
                if norm(term[0]) in n and any(norm(x) in n for x in term[1:]):
                    return cat, None
            elif norm(term) in n:
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
    """우선순위대로 들어온 키워드를 '대표 키워드 + 긴 검색어' 묶음으로 모아요 (같은 말이 표를 채우지 않게)."""
    fams = []
    for x in items:
        hits = [f for f in fams if is_variant(f["head"]["keyword"], x["keyword"])
                or is_variant(x["keyword"], f["head"]["keyword"])]
        if not hits:
            if len(fams) < limit:
                fams.append({"head": x, "members": []})
            continue
        first = hits[0]
        for f in hits[1:]:
            first["members"] += [f["head"]] + f["members"]
            fams.remove(f)
        if is_variant(x["keyword"], first["head"]["keyword"]):
            first["members"].append(first["head"])
            first["head"] = x
        else:
            first["members"].append(x)
    return fams


# ── 수집 ────────────────────────────────────────────────────────────────────

def collect(sa, hints, pool, problems, src="base"):
    """기본 단어를 5개씩 조회해서 pool 에 모아요. 인증 오류는 바로 멈추고, 나머지 오류는 기록만 해요."""
    for group in chunks(hints, 5):
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
        pc, mo = num(r.get("monthlyPcQcCnt")), num(r.get("monthlyMobileQcCnt"))
        key = norm(kw)
        cur = pool.get(key)
        if cur is None:
            pool[key] = {"keyword": kw, "pc": pc, "mobile": mo, "volume": pc + mo,
                         "comp": str(r.get("compIdx") or ""), "src": src}
        elif pc + mo > cur["volume"]:
            cur.update({"pc": pc, "mobile": mo, "volume": pc + mo})


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


def hints_hash(hints):
    return hashlib.md5(",".join(sorted(norm(h) for h in hints)).encode("utf-8")).hexdigest()[:12]


def prune_history(keep):
    files = sorted(glob.glob(os.path.join(HISTORY, "*.json")))
    for path in files[:-keep] if keep > 0 else []:
        os.remove(path)


# ── 분석 ────────────────────────────────────────────────────────────────────

def analyze(cfg, sa, dl, now):
    today = now.date().isoformat()
    hints = unique(cfg.get("seeds", []) + cfg.get("watch", []))
    hint_keys = {norm(h) for h in hints}
    pool, problems = {}, []
    collect(sa, hints, pool, problems)
    if not pool:
        raise ApiError("검색광고", 200, None, "연관 키워드를 하나도 받지 못했어요 — " + "; ".join(problems[:3]))

    def classify_all():
        for item in pool.values():
            if "category" not in item:
                item["category"], item["reason"] = classify(item["keyword"], cfg)

    classify_all()
    ranked = sorted((x for x in pool.values() if x["category"]), key=lambda x: -x["volume"])
    # 많이 찾는 '새 대표 키워드'(기본 단어가 아닌 것)의 긴 검색어를 한 번 더 모아요
    heads0 = [f["head"]["keyword"] for f in group_families(ranked, cfg.get("expand_top", 10) * 3)]
    extra = [h for h in heads0 if norm(h) not in hint_keys][:cfg.get("expand_top", 10)]
    if extra:
        collect(sa, extra, pool, problems, src="extra")
        classify_all()
        ranked = sorted((x for x in pool.values() if x["category"]), key=lambda x: -x["volume"])

    prev_date, prev, prev_hash = load_previous(today)
    hh = hints_hash(hints)
    # 기본 단어나 캘린더 글감이 바뀐 직후에는 '새로 등장'이 단어 목록 변화 때문일 수 있어 표시하지 않아요
    new_ok = bool(prev_date) and prev_hash == hh
    for x in pool.values():
        old = prev.get(norm(x["keyword"]))
        x["delta"] = round(x["volume"] / old - 1, 3) if old else None
        # 추가 조회로만 모인 키워드는 매번 달라질 수 있어 '새로 등장'으로 보지 않아요
        x["is_new"] = new_ok and old is None and x.get("src") == "base"

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

    # 제목에 쓸 만한 긴 검색어: 대표 키워드로 시작하는 더 긴 키워드 중 검색이 많은 것
    per = cfg.get("longtail_per_keyword", 3)
    heads = unique([f["head"]["keyword"] for f in rising_fams[:8]] + [f["head"]["keyword"] for f in top_fams[:12]])
    longtail = []
    for h in heads:
        tails = [x for x in ranked if is_variant(h, x["keyword"])]
        if tails:
            longtail.append({"keyword": h, "tails": [{"keyword": t["keyword"], "volume": t["volume"]}
                                                     for t in tails[:per]]})

    watch = []
    for w in cfg.get("watch", []):
        x = pool.get(norm(w))
        watch.append({"keyword": w, "volume": x["volume"] if x else None,
                      "delta": x["delta"] if x else None, "is_new": x["is_new"] if x else False,
                      "trend": x.get("trend") if x else None})

    excluded = sorted((x for x in pool.values() if not x["category"] and norm(x["keyword"]) not in hint_keys),
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
        "counts": {"hints": len(hints), "extra": len(extra), "pool": len(pool), "in_topic": len(ranked),
                   "searchad_calls": sa.calls, "datalab_calls": dl.calls if dl else 0},
        "datalab": datalab,
        "problems": problems,
        "rising": [slim(f["head"], f["members"]) for f in rising_fams],
        "top": [slim(f["head"], f["members"]) for f in top_fams],
        "watch": watch,
        "longtail": longtail,
        "excluded": [{"keyword": x["keyword"], "volume": x["volume"], "reason": x["reason"]} for x in excluded],
        "_history": {norm(k): v for k, v in history.items()},
    }


def slim(x, members=()):
    out = {"keyword": x["keyword"], "category": x["category"], "volume": x["volume"],
           "pc": x["pc"], "mobile": x["mobile"], "delta": x["delta"], "is_new": x["is_new"],
           "related": len(members)}
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
            lines.append("- 기본 단어·캘린더 글감이 바뀐 뒤 첫 분석이라 이번에는 '새로 등장'을 표시하지 않아요")
    else:
        lines.append("- 첫 분석이라 '지난번 대비'는 다음 분석부터 나와요")
    lines.append(f"- 조회: 기본 단어 {c['hints']}개 + 추가 {c['extra']}개 → 연관 키워드 {fmt(c['pool'])}개 중 블로그 주제에 맞는 {fmt(c['in_topic'])}개")
    if dl["used"] and not dl["error"]:
        lines.append(f"- 데이터랩: 연결됨({dl['used']}) · {dl['start']}~{dl['end']} · '최근 1주'는 그 전 3주 평균 대비 배수")
    elif dl["error"]:
        lines.append(f"- 데이터랩: 일부 또는 전체 실패 — {dl['error']}")
    else:
        lines.append("- 데이터랩: 키 없음(선택 기능) — '최근 1주' 칸은 비어 있어요")
    if rep["problems"]:
        lines.append(f"- 조회하지 못한 단어: {'; '.join(rep['problems'][:5])}")
    lines += ["- '긴 검색어'는 대표 키워드로 시작하는 더 긴 검색어 수예요(예: 근로장려금 → 근로장려금지급일). 4장에 많이 찾는 것을 적었어요",
              "", "## 1. 뜨는 키워드", ""]
    if rep["rising"]:
        lines += ["| 키워드 | 분류 | 월 검색수 | 지난번 대비 | 최근 1주 | 4주 흐름 | 긴 검색어 |", "|---|---|---:|---:|---|---|---:|"]
        for x in rep["rising"]:
            t, s = trend_cells(x)
            lines.append(f"| {x['keyword']} | {x['category']} | {fmt(x['volume'])} | {delta_label(x)} | {t} | {s} | {x.get('related', 0)} |")
    else:
        lines.append("아직 없어요. (첫 분석이거나 크게 오른 키워드가 없어요)")
    lines += ["", f"## 2. 많이 찾는 키워드 TOP {len(rep['top'])}", "",
              "| 순위 | 키워드 | 분류 | 월 검색수 | 지난번 대비 | 최근 1주 | 긴 검색어 |", "|---:|---|---|---:|---:|---|---:|"]
    for i, x in enumerate(rep["top"], 1):
        t, _ = trend_cells(x)
        lines.append(f"| {i} | {x['keyword']} | {x['category']} | {fmt(x['volume'])} | {delta_label(x)} | {t} | {x.get('related', 0)} |")
    if rep["watch"]:
        lines += ["", f"## 3. 캘린더 글감 확인 ({rep['watch_file']})", "",
                  "| 키워드 | 월 검색수 | 지난번 대비 | 최근 1주 | 4주 흐름 |", "|---|---:|---:|---|---|"]
        for w in rep["watch"]:
            if w["volume"] is None:
                lines.append(f"| {w['keyword']} | 데이터 없음 | – | – | – |")
                continue
            t, s = trend_cells(w)
            lines.append(f"| {w['keyword']} | {fmt(w['volume'])} | {delta_label(w)} | {t} | {s} |")
    if rep["longtail"]:
        lines += ["", "## 4. 제목에 쓸 만한 긴 검색어", "",
                  "키워드 도구는 띄어쓰기 없이 보여 줘요. 제목·태그에는 자연스럽게 띄어 쓰고, 같은 말을 반복하지 않아요.", ""]
        for lt in rep["longtail"]:
            tails = " · ".join(f"{t['keyword']}({fmt(t['volume'])})" for t in lt["tails"])
            lines.append(f"- **{lt['keyword']}** → {tails}")
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
