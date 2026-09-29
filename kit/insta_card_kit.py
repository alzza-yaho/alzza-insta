#!/usr/bin/env python3
"""insta_card_kit.py — 인스타그램 카드뉴스(캐러셀) 패키지 생성기

블로그 spec.json의 "insta" 블록(또는 insta 블록만 담은 json) →
  insta/01.jpg … NN.jpg   1080×1350(4:5, 기본) 또는 1080×1440(3:4) 슬라이드
  insta/caption.txt       캡션 + 해시태그(최대 5개)
  insta/insta.html        미리보기 · 캡션 복사 · 대체 텍스트 · 업로드 순서 · 검수 목록
  insta/insta_slides.zip  슬라이드 + 캡션 묶음
  (--queue 를 주면) <queue>/<게시일>/ 01.jpg… + caption.txt + post.json  ← 깃허브 자동 게시 대기열

사용: python3 insta_card_kit.py spec.json OUT_DIR [--char-dir 포즈폴더] [--handle @아이디]
      [--ratio 4x5|3x4] [--queue 대기열폴더 --date YYYY-MM-DD [--publish-at 2026-10-01T12:00:00+09:00]]

인스타 공식 API는 4:5~1.91:1 비율의 JPEG만, 캐러셀은 10장까지 받아요. 자동 게시용은 기본값(4:5)을 써요.

캐릭터: 포즈폴더에 pose_<이름>.png(예: pose_megaphone.png)가 있으면 흰 배경을 지워서 쓰고,
없으면 코드로 그린 임시 다람쥐 '알짜'를 써요.
필요: Pillow, numpy, scipy, playwright(Chromium), npm(글꼴 설치용)
"""
import argparse, base64, datetime, glob, hashlib, html, io, json, os, re, shutil, subprocess, urllib.parse, zipfile, zlib

# ============================================================
# 0. 기본 설정
# ============================================================
BRAND = "놓치면 손해 생활 알짜정보"
HANDLE = ""   # 인스타 아이디가 정해지면 --handle @아이디 또는 spec의 insta.handle로 넣어요 (비어 있으면 카드에 표시 안 함)
MAX_TAGS, MAX_CAPTION, MAX_SLIDES = 5, 2200, 20
W, H = 1080, 1350
LAYOUTS = {  # 비율별 배치 값
    "4x5": {"H": 1350, "char": 340, "char_top": 112, "body_min": 468, "cover_title": 124},
    "3x4": {"H": 1440, "char": 370, "char_top": 118, "body_min": 500, "cover_title": 132},
}
L = LAYOUTS["4x5"]
API_MAX_ITEMS = 10  # 인스타 API 캐러셀 최대 장수
REPO = "alzza-yaho/alzza-insta"  # 깃허브 자동 게시 저장소
CARD_HEADER = "# alzza-insta card v1 (do not edit)"
LINK_WARN = 8000  # 깃허브 새 파일 링크가 이보다 길면 경고


def set_ratio(ratio):
    global H, L
    L = LAYOUTS[ratio]
    H = L["H"]

POSE_NAMES = ["hello", "megaphone", "calendar", "pointer", "magnifier", "acorns", "phone",
              "checklist", "surprised", "stop", "thinking", "thumbsup", "saving", "heart"]

FONT_PKGS = ["@fontsource/jua", "pretendard"]


def ensure_fonts():
    """Jua(제목) + Pretendard(본문) 글꼴 폴더를 찾고, 없으면 npm으로 설치. 실패하면 None(시스템 글꼴 사용)"""
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.getcwd(), os.path.expanduser("~/.cache/insta_fonts")):
        nm = os.path.join(base, "node_modules")
        if os.path.exists(os.path.join(nm, "@fontsource/jua/400.css")) and \
           os.path.exists(os.path.join(nm, "pretendard/dist/web/static/woff2/Pretendard-Bold.woff2")):
            return nm
    cache = os.path.expanduser("~/.cache/insta_fonts")
    try:
        os.makedirs(cache, exist_ok=True)
        subprocess.run(["npm", "install", "--prefix", cache, "--no-audit", "--no-fund", *FONT_PKGS],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        return os.path.join(cache, "node_modules")
    except Exception as e:
        print("⚠ 점검: 글꼴 설치 실패 — 시스템 글꼴(Noto Sans CJK)로 만들어요:", e)
        return None


def font_css(nm):
    if not nm:
        return ""
    jua = f"file://{nm}/@fontsource/jua/400.css"
    pre = f"{nm}/pretendard/dist/web/static/woff2"
    faces = "".join(
        f"@font-face{{font-family:'Pretendard';font-weight:{w};src:url('file://{pre}/Pretendard-{n}.woff2') format('woff2')}}"
        for w, n in ((400, "Regular"), (500, "Medium"), (600, "SemiBold"), (700, "Bold"), (800, "ExtraBold"), (900, "Black")))
    return f'<link rel="stylesheet" href="{jua}"><style>{faces}</style>'


# ============================================================
# 1. 임시 캐릭터 — 다람쥐 '알짜' (SVG)
# ============================================================
FUR, FUR_L, FUR_D = "#E3914F", "#F2B173", "#B9672F"
CREAM, OUT, BLUSH, EAR_IN = "#FFF1DA", "#4A2C1D", "#FF9C8A", "#F7B9A3"
NAVY, NAVY_D, YEL, GOLD = "#15315B", "#0D2342", "#FFD34D", "#FFC53D"
SW = 7


def _star4(cx, cy, r, fill=YEL, stroke=OUT, sw=4):
    k = r * 0.28
    d = (f"M{cx},{cy-r} Q{cx+k},{cy-k} {cx+r},{cy} Q{cx+k},{cy+k} {cx},{cy+r} "
         f"Q{cx-k},{cy+k} {cx-r},{cy} Q{cx-k},{cy-k} {cx},{cy-r} Z")
    return f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" stroke-linejoin="round"/>'


def _acorn(cx, cy, s=1.0, nut=GOLD):
    w, h = 44 * s, 50 * s
    sw = max(3, 6 * s)
    return (f'<g stroke="{OUT}" stroke-width="{sw:.1f}" stroke-linejoin="round">'
            f'<path d="M{cx-w/2},{cy-h*0.1} Q{cx-w/2},{cy+h*0.55} {cx},{cy+h*0.62} '
            f'Q{cx+w/2},{cy+h*0.55} {cx+w/2},{cy-h*0.1} Z" fill="{nut}"/>'
            f'<path d="M{cx-w*0.62},{cy-h*0.08} Q{cx-w*0.55},{cy-h*0.55} {cx},{cy-h*0.58} '
            f'Q{cx+w*0.55},{cy-h*0.55} {cx+w*0.62},{cy-h*0.08} Q{cx},{cy+h*0.06} {cx-w*0.62},{cy-h*0.08} Z" fill="#9A5B2E"/>'
            f'<path d="M{cx},{cy-h*0.56} Q{cx+w*0.05},{cy-h*0.78} {cx+w*0.2},{cy-h*0.82}" fill="none" stroke-linecap="round"/>'
            f'</g><ellipse cx="{cx-w*0.2}" cy="{cy+h*0.15}" rx="{w*0.08}" ry="{h*0.14}" fill="#fff" opacity=".7"/>')


def _paw(cx, cy, r=21):
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{FUR}" stroke="{OUT}" stroke-width="{SW}"/>'


def _arm(cx, cy, rot, rx=22, ry=36):
    return (f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" transform="rotate({rot} {cx} {cy})" '
            f'fill="{FUR}" stroke="{OUT}" stroke-width="{SW}"/>')


def _tail():
    outer = ("M318,440 C430,452 492,352 474,258 C460,178 404,108 338,118 "
             "C292,126 284,182 322,190 C352,196 384,206 388,248 C394,306 362,362 300,392 Z")
    inner = ("M338,410 C420,414 454,340 440,268 C430,214 398,164 356,158 "
             "C376,176 404,210 406,252 C410,312 380,370 338,410 Z")
    return (f'<path d="{outer}" fill="{FUR}" stroke="{OUT}" stroke-width="{SW}" stroke-linejoin="round"/>'
            f'<path d="{inner}" fill="{FUR_L}"/>')


def _sq_body():
    return (f'<ellipse cx="240" cy="376" rx="102" ry="92" fill="{FUR}" stroke="{OUT}" stroke-width="{SW}"/>'
            f'<ellipse cx="240" cy="394" rx="62" ry="60" fill="{CREAM}"/>'
            f'<ellipse cx="186" cy="462" rx="40" ry="21" fill="{FUR}" stroke="{OUT}" stroke-width="{SW}"/>'
            f'<ellipse cx="294" cy="462" rx="40" ry="21" fill="{FUR}" stroke="{OUT}" stroke-width="{SW}"/>')


def _scarf():
    band = "M146,298 Q240,340 334,298 Q340,318 330,332 Q240,372 150,332 Q140,318 146,298 Z"
    knot = "M286,336 L322,382 L296,390 L278,344 Z"
    return (f'<path d="{knot}" fill="{NAVY}" stroke="{NAVY_D}" stroke-width="5" stroke-linejoin="round"/>'
            f'<path d="{band}" fill="{NAVY}" stroke="{NAVY_D}" stroke-width="5" stroke-linejoin="round"/>'
            + _acorn(204, 334, 0.42))


def _head(expr="happy"):
    g = []
    for cx, rot in ((140, -24), (340, 24)):
        g.append(f'<ellipse cx="{cx}" cy="96" rx="38" ry="46" transform="rotate({rot} {cx} 96)" fill="{FUR}" stroke="{OUT}" stroke-width="{SW}"/>')
        g.append(f'<ellipse cx="{cx}" cy="102" rx="20" ry="27" transform="rotate({rot} {cx} 102)" fill="{EAR_IN}"/>')
    g.append(f'<ellipse cx="240" cy="192" rx="132" ry="114" fill="{FUR}" stroke="{OUT}" stroke-width="{SW}"/>')
    g.append(f'<path d="M240,82 L240,122" stroke="{FUR_D}" stroke-width="13" stroke-linecap="round"/>')
    g.append(f'<path d="M212,90 L215,116" stroke="{FUR_D}" stroke-width="9" stroke-linecap="round"/>')
    g.append(f'<path d="M268,90 L265,116" stroke="{FUR_D}" stroke-width="9" stroke-linecap="round"/>')
    g.append(f'<ellipse cx="240" cy="240" rx="64" ry="44" fill="{CREAM}"/>')
    g.append(f'<ellipse cx="148" cy="234" rx="23" ry="13" fill="{BLUSH}" opacity=".75"/>')
    g.append(f'<ellipse cx="332" cy="234" rx="23" ry="13" fill="{BLUSH}" opacity=".75"/>')

    def eye(cx, cy=192):
        return (f'<ellipse cx="{cx}" cy="{cy}" rx="16" ry="20" fill="#2A1810"/>'
                f'<circle cx="{cx-5}" cy="{cy-8}" r="6.5" fill="#fff"/><circle cx="{cx+6}" cy="{cy+8}" r="3" fill="#fff"/>')

    def happy_eye(cx, cy=196):
        return f'<path d="M{cx-17},{cy+4} Q{cx},{cy-18} {cx+17},{cy+4}" fill="none" stroke="#2A1810" stroke-width="7" stroke-linecap="round"/>'

    if expr in ("happy", "curious", "serious"):
        g += [eye(182), eye(298)]
    elif expr == "joy":
        g += [happy_eye(182), happy_eye(298)]
    elif expr == "wink":
        g += [eye(182), happy_eye(298)]
    elif expr == "surprised":
        for cx in (182, 298):
            g.append(f'<ellipse cx="{cx}" cy="190" rx="19" ry="23" fill="#fff" stroke="#2A1810" stroke-width="6"/>')
            g.append(f'<circle cx="{cx}" cy="192" r="8" fill="#2A1810"/>')
    if expr == "serious":
        g.append(f'<path d="M160,158 L200,170" stroke="{OUT}" stroke-width="7" stroke-linecap="round"/>')
        g.append(f'<path d="M320,158 L280,170" stroke="{OUT}" stroke-width="7" stroke-linecap="round"/>')
    if expr == "curious":
        g.append(f'<path d="M164,160 Q182,148 200,158" fill="none" stroke="{OUT}" stroke-width="6" stroke-linecap="round"/>')
    g.append('<path d="M228,218 Q240,210 252,218 Q248,230 240,232 Q232,230 228,218 Z" fill="#3A2218"/>')
    if expr == "surprised":
        g.append(f'<ellipse cx="240" cy="252" rx="12" ry="14" fill="#8C3B2E" stroke="{OUT}" stroke-width="5"/>')
    elif expr == "serious":
        g.append(f'<path d="M226,246 L254,246" stroke="{OUT}" stroke-width="5" stroke-linecap="round"/>')
    elif expr == "joy":
        g.append(f'<path d="M218,240 Q240,276 262,240 Z" fill="#8C3B2E" stroke="{OUT}" stroke-width="5" stroke-linejoin="round"/>')
        g.append(f'<rect x="232" y="240" width="16" height="11" rx="2" fill="#fff" stroke="{OUT}" stroke-width="3"/>')
    else:
        g.append(f'<path d="M222,241 Q231,252 240,242 Q249,252 258,241" fill="none" stroke="{OUT}" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>')
        g.append(f'<rect x="232" y="246" width="16" height="13" rx="3" fill="#fff" stroke="{OUT}" stroke-width="3.5"/>')
        g.append(f'<path d="M240,246 L240,259" stroke="{OUT}" stroke-width="2.5"/>')
    return "".join(g)


def squirrel_svg(pose="hello"):
    """포즈별 임시 다람쥐. viewBox 0 0 520 520"""
    front, extra = [], []
    expr = "happy"
    arms = [_arm(160, 380, 22), _arm(320, 380, -22)]
    if pose == "hello":
        expr = "joy"
        arms = [_arm(166, 384, 22), _arm(378, 300, -145)]
        extra.append(f'<path d="M424,236 q14,-10 26,-2 M432,262 q16,-4 24,8" fill="none" stroke="{OUT}" stroke-width="6" stroke-linecap="round"/>')
    elif pose == "megaphone":
        expr = "joy"
        front += [f'<path d="M300,292 L430,228 Q456,266 442,304 L312,320 Z" fill="{NAVY}" stroke="{OUT}" stroke-width="{SW}" stroke-linejoin="round"/>',
                  f'<ellipse cx="438" cy="266" rx="15" ry="40" transform="rotate(-14 438 266)" fill="{YEL}" stroke="{OUT}" stroke-width="{SW}"/>',
                  f'<path d="M318,296 L326,318" stroke="{YEL}" stroke-width="10" stroke-linecap="round"/>', _paw(306, 312)]
        extra.append(f'<path d="M474,222 q14,10 14,26 M486,262 q10,0 18,6 M474,306 q12,-4 20,-16" fill="none" stroke="{YEL}" stroke-width="8" stroke-linecap="round"/>')
        arms = [_arm(166, 384, 22), _arm(318, 338, -60)]
    elif pose == "calendar":
        b = [f'<rect x="162" y="318" width="156" height="134" rx="16" fill="#fff" stroke="{OUT}" stroke-width="{SW}"/>',
             f'<path d="M162,352 L162,334 Q162,318 178,318 L302,318 Q318,318 318,334 L318,352 Z" fill="#FF7B6B" stroke="{OUT}" stroke-width="{SW}" stroke-linejoin="round"/>',
             f'<circle cx="200" cy="316" r="7" fill="{NAVY}"/><circle cx="280" cy="316" r="7" fill="{NAVY}"/>']
        for i in range(3):
            for j in range(4):
                b.append(f'<rect x="{182 + j*36}" y="{370 + i*26}" width="22" height="14" rx="5" fill="#E6E9EF"/>')
        b.append('<circle cx="229" cy="403" r="17" fill="none" stroke="#FF5A4A" stroke-width="5"/>')
        front += b + [_paw(162, 396), _paw(318, 396)]
        arms = [_arm(170, 388, 30), _arm(310, 388, -30)]
    elif pose == "pointer":
        front += ['<path d="M388,316 L474,196" stroke="#8A5A34" stroke-width="9" stroke-linecap="round"/>', _star4(478, 188, 24), _paw(384, 318)]
        arms = [_arm(166, 384, 22), _arm(352, 348, -55)]
    elif pose == "magnifier":
        expr = "curious"
        front += [f'<path d="M352,330 L318,372" stroke="{NAVY}" stroke-width="16" stroke-linecap="round"/>',
                  f'<circle cx="392" cy="286" r="52" fill="#DDF0FF" fill-opacity=".85" stroke="{NAVY}" stroke-width="12"/>',
                  '<path d="M366,270 q10,-22 34,-26" fill="none" stroke="#fff" stroke-width="8" stroke-linecap="round"/>', _paw(328, 360)]
        arms = [_arm(166, 384, 22), _arm(318, 372, -40)]
    elif pose == "acorns":
        expr = "joy"
        front += [_acorn(240, 398, 1.9), _paw(172, 414), _paw(308, 414)]
        extra += [_star4(118, 250, 20), _star4(396, 338, 16), _star4(420, 118, 14)]
        arms = [_arm(172, 400, 30), _arm(308, 400, -30)]
    elif pose == "phone":
        front += [f'<rect x="200" y="330" width="80" height="124" rx="14" fill="{NAVY}" stroke="{OUT}" stroke-width="{SW}"/>',
                  '<rect x="211" y="345" width="58" height="92" rx="6" fill="#DDEBFF"/>',
                  f'<rect x="220" y="360" width="40" height="10" rx="5" fill="{YEL}"/>',
                  '<rect x="220" y="380" width="30" height="8" rx="4" fill="#AFC3E3"/><rect x="220" y="396" width="36" height="8" rx="4" fill="#AFC3E3"/>',
                  _paw(198, 410), _paw(282, 410)]
        arms = [_arm(178, 396, 30), _arm(302, 396, -30)]
    elif pose == "checklist":
        cb = [f'<rect x="166" y="322" width="148" height="140" rx="14" fill="#fff" stroke="{OUT}" stroke-width="{SW}"/>',
              f'<rect x="210" y="308" width="60" height="26" rx="8" fill="{NAVY}" stroke="{OUT}" stroke-width="5"/>']
        for i in range(3):
            y = 360 + i * 32
            cb.append(f'<path d="M184,{y} l9,10 l17,-18" fill="none" stroke="#2E9E6A" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>')
            cb.append(f'<rect x="222" y="{y-4}" width="74" height="11" rx="5" fill="#E3E7EE"/>')
        front += cb + [_paw(166, 404), _paw(314, 404)]
        arms = [_arm(172, 394, 30), _arm(308, 394, -30)]
    elif pose == "surprised":
        expr = "surprised"
        front += [_paw(118, 250, 23), _paw(362, 250, 23)]
        arms = [_arm(132, 314, 20, 22, 44), _arm(348, 314, -20, 22, 44)]
        extra += [f'<path d="M400,150 q18,26 0,40 q-18,-14 0,-40 Z" fill="#9FD3FF" stroke="{OUT}" stroke-width="4"/>',
                  '<path d="M92,120 L104,150 M70,142 L94,160 M120,108 L122,140" stroke="#FF5A4A" stroke-width="7" stroke-linecap="round"/>']
    elif pose == "stop":
        expr = "serious"
        front += [f'<rect x="150" y="360" width="180" height="46" rx="23" transform="rotate(35 240 383)" fill="{FUR}" stroke="{OUT}" stroke-width="{SW}"/>',
                  f'<rect x="150" y="360" width="180" height="46" rx="23" transform="rotate(-35 240 383)" fill="{FUR}" stroke="{OUT}" stroke-width="{SW}"/>']
        arms = []
    elif pose == "thinking":
        expr = "curious"
        front += [_paw(214, 282, 22)]
        arms = [_arm(192, 340, 35), _arm(320, 380, -22)]
        extra.append(f'<text x="412" y="150" font-family="Jua" font-size="96" fill="{NAVY}" stroke="#fff" stroke-width="10" paint-order="stroke">?</text>')
    elif pose == "thumbsup":
        expr = "wink"
        front += [f'<ellipse cx="356" cy="272" rx="13" ry="26" fill="{FUR}" stroke="{OUT}" stroke-width="6"/>', _paw(352, 318, 28)]
        arms = [_arm(166, 384, 22), _arm(338, 358, -30)]
        extra += [_star4(424, 236, 20), _star4(96, 200, 15)]
    elif pose == "saving":
        expr = "joy"
        front += [f'<path d="M180,360 Q176,452 240,456 Q304,452 300,360 Z" fill="#E8F4FF" stroke="{OUT}" stroke-width="{SW}" stroke-linejoin="round"/>',
                  f'<rect x="170" y="340" width="140" height="28" rx="12" fill="{NAVY}" stroke="{OUT}" stroke-width="{SW}"/>',
                  _acorn(216, 420, 0.6), _acorn(262, 424, 0.6), _acorn(338, 296, 0.75), _paw(346, 322)]
        arms = [_arm(166, 384, 22), _arm(330, 346, -40)]
    elif pose == "heart":
        expr = "joy"
        front += ['<path d="M240,430 C170,386 180,330 214,330 C230,330 240,344 240,352 C240,344 250,330 266,330 C300,330 310,386 240,430 Z" '
                  f'fill="#FF7B8A" stroke="{OUT}" stroke-width="{SW}" stroke-linejoin="round"/>', _paw(196, 380), _paw(284, 380)]
        arms = [_arm(172, 384, 30), _arm(308, 384, -30)]
    parts = [_tail(), _sq_body()] + arms + [_scarf(), _head(expr)] + front + extra
    return f'<svg viewBox="0 0 520 520" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'


# ============================================================
# 2. 포즈 이미지 (Topview에서 만든 PNG) — 흰 배경 자동 제거
# ============================================================
def cutout_png(path, cache_dir):
    """흰(밝은) 배경을 투명하게 → 여백을 잘라 PNG 바이트로 반환. 결과는 캐시에 저장"""
    import numpy as np
    from PIL import Image
    from scipy import ndimage
    os.makedirs(cache_dir, exist_ok=True)
    key = os.path.join(cache_dir, os.path.basename(path).rsplit(".", 1)[0] + f"_{int(os.path.getmtime(path))}_cut.png")
    if os.path.exists(key):
        return open(key, "rb").read()
    im = Image.open(path).convert("RGBA")
    if max(im.size) > 1100:
        im.thumbnail((1100, 1100), Image.LANCZOS)
    a = np.asarray(im).astype(np.float32)
    rgb, alpha0 = a[..., :3], a[..., 3]
    if (alpha0 < 250).mean() < 0.03:  # 불투명 이미지 → 배경 제거
        mn, mx = rgb.min(axis=2), rgb.max(axis=2)
        near = (mn > 226) & ((mx - mn) < 26)
        lab, nlab = ndimage.label(near)
        edge = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
        bg = np.isin(lab, edge[edge > 0])
        # 꼬리·팔 사이처럼 갇힌 배경 구멍도 지우기: 배경색과 같고, 안에 그림(선·무늬)이 없는 덩어리만
        if bg.any():
            bg_col = rgb[bg].mean(axis=0)
            total = near.size
            sizes = ndimage.sum(near, lab, index=np.arange(1, nlab + 1))
            objs = ndimage.find_objects(lab)
            for idx in range(1, nlab + 1):
                if idx in edge:
                    continue
                area = sizes[idx - 1]
                if not (0.0015 * total < area < 0.08 * total):
                    continue
                sl = objs[idx - 1]
                comp = lab[sl] == idx
                if (ndimage.binary_fill_holes(comp) & ~comp).sum() > 0.01 * area:
                    continue  # 안에 무늬가 있으면 흰 소품(종이·달력)으로 보고 남겨요
                if np.abs(rgb[sl][comp].mean(axis=0) - bg_col).max() > 6:
                    continue
                bg[sl] |= comp
        band = ndimage.binary_dilation(bg, iterations=3) & ~bg
        alpha = np.full(bg.shape, 255.0)
        alpha[bg] = 0
        # 경계 띠: 흰색과의 차이만큼만 불투명하게 (부드러운 가장자리)
        dev = (255.0 - rgb).max(axis=2) / 255.0
        ba = np.clip(dev * 1.6, 0, 1)
        alpha[band] = ba[band] * 255
        safe = np.clip(ba, 0.05, 1)[..., None]
        fixed = np.clip((rgb - (1 - safe) * 255) / safe, 0, 255)
        rgb = np.where(band[..., None], fixed, rgb)
        out = np.dstack([rgb, alpha]).astype(np.uint8)
    else:
        out = a.astype(np.uint8)
    img = Image.fromarray(out, "RGBA")
    bbox = img.getchannel("A").point(lambda v: 255 if v > 12 else 0).getbbox()
    if bbox:
        pad = 12
        bbox = (max(0, bbox[0] - pad), max(0, bbox[1] - pad), min(img.width, bbox[2] + pad), min(img.height, bbox[3] + pad))
        img = img.crop(bbox)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    open(key, "wb").write(buf.getvalue())
    return buf.getvalue()


class Characters:
    def __init__(self, char_dir, cache_dir):
        self.dir, self.cache, self.used, self.missing = char_dir, cache_dir, set(), set()

    def html(self, pose):
        if not pose:
            return ""
        if self.dir:
            for cand in (f"pose_{pose}.png", f"{pose}.png", f"pose_{pose}.webp", f"pose_{pose}.jpg"):
                matches = glob.glob(os.path.join(self.dir, cand)) + glob.glob(os.path.join(self.dir, "pose_*_" + pose + ".png"))
                if matches:
                    data = base64.b64encode(cutout_png(matches[0], self.cache)).decode()
                    self.used.add(pose)
                    return f'<img src="data:image/png;base64,{data}" alt="">'
        self.missing.add(pose)
        return squirrel_svg(pose)


# ============================================================
# 3. 아이콘 (64×64)
# ============================================================
def icon(name):
    s = f'stroke="{NAVY}" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"'
    body = {
        "calendar": f'<rect x="10" y="14" width="44" height="40" rx="8" fill="#fff" {s}/><path d="M10 26h44" {s}/><path d="M22 9v10M42 9v10" {s}/><circle cx="32" cy="40" r="6" fill="{YEL}" {s}/>',
        "person": f'<circle cx="32" cy="22" r="10" fill="{YEL}" {s}/><path d="M13 54c2-11 10-17 19-17s17 6 19 17z" fill="#fff" {s}/>',
        "won": f'<circle cx="32" cy="32" r="22" fill="{YEL}" {s}/><path d="M21 24l5 17 6-14 6 14 5-17M20 33h24" fill="none" {s}/>',
        "percent": f'<circle cx="32" cy="32" r="22" fill="{YEL}" {s}/><path d="M23 42l18-20" {s}/><circle cx="24" cy="25" r="4" fill="#fff" {s}/><circle cx="40" cy="39" r="4" fill="#fff" {s}/>',
        "check": f'<circle cx="32" cy="32" r="22" fill="{YEL}" {s}/><path d="M22 33l7 7 13-15" fill="none" {s}/>',
        "bank": f'<path d="M10 26L32 12l22 14z" fill="{YEL}" {s}/><path d="M16 30v16M27 30v16M37 30v16M48 30v16M10 52h44" {s}/>',
        "warn": f'<path d="M32 10L56 52H8z" fill="{YEL}" {s}/><path d="M32 26v12" {s}/><circle cx="32" cy="45" r="2.5" fill="{NAVY}"/>',
        "clock": f'<circle cx="32" cy="32" r="22" fill="#fff" {s}/><path d="M32 20v13l9 6" fill="none" {s}/>',
        "doc": f'<path d="M16 8h22l10 10v38H16z" fill="#fff" {s}/><path d="M38 8v10h10M23 30h18M23 39h18M23 48h10" fill="none" {s}/>',
        "phone": f'<rect x="19" y="8" width="26" height="48" rx="6" fill="#fff" {s}/><path d="M28 48h8" {s}/><rect x="24" y="16" width="16" height="7" rx="3" fill="{YEL}"/>',
        "gift": f'<rect x="12" y="26" width="40" height="28" rx="4" fill="#fff" {s}/><rect x="9" y="18" width="46" height="10" rx="3" fill="{YEL}" {s}/><path d="M32 18v36M32 18c-6-10-16-8-12-1 2 3 12 1 12 1zM32 18c6-10 16-8 12-1-2 3-12 1-12 1z" fill="none" {s}/>',
        "home": f'<path d="M10 30L32 12l22 18" fill="none" {s}/><path d="M16 26v26h32V26" fill="#fff" {s}/><rect x="27" y="36" width="10" height="16" fill="{YEL}" {s}/>',
        "acorn": f'<path d="M18 30c0 14 6 22 14 24 8-2 14-10 14-24z" fill="{GOLD}" {s}/><path d="M13 30c1-10 9-16 19-16s18 6 19 16z" fill="#9A5B2E" {s}/><path d="M32 14c1-4 3-6 6-7" fill="none" {s}/>',
        "link": f'<path d="M27 37l10-10" {s}/><path d="M24 30l-6 6a8 8 0 0 0 11 11l6-6M40 34l6-6a8 8 0 0 0-11-11l-6 6" fill="none" {s}/>',
        "bookmark": f'<path d="M18 8h28v48L32 44 18 56z" fill="{YEL}" {s}/>',
        "receipt": f'<path d="M16 8h32v48l-6-4-5 4-5-4-5 4-5-4-6 4z" fill="#fff" {s}/><path d="M24 22h16M24 31h16M24 40h9" {s}/>',
        "chart": f'<path d="M10 54h44" {s}/><rect x="15" y="34" width="9" height="20" rx="2" fill="{YEL}" {s}/><rect x="28" y="22" width="9" height="32" rx="2" fill="#fff" {s}/><rect x="41" y="12" width="9" height="42" rx="2" fill="{YEL}" {s}/>',
        "heart": f'<path d="M32 52C12 40 10 22 20 17c6-3 10 0 12 5 2-5 6-8 12-5 10 5 8 23-12 35z" fill="#FF8C98" {s}/>',
        "search": f'<circle cx="28" cy="28" r="15" fill="#fff" {s}/><path d="M39 39l14 14" {s}/>',
        "star": f'<path d="M32 9l7 14 15 2-11 11 3 15-14-7-14 7 3-15-11-11 15-2z" fill="{YEL}" {s}/>',
        "tax": f'<rect x="12" y="10" width="40" height="44" rx="6" fill="#fff" {s}/><path d="M20 22h24M20 32h10" {s}/><circle cx="40" cy="42" r="8" fill="{YEL}" {s}/>',
        "car": f'<path d="M10 40l5-14c1-3 3-4 6-4h22c3 0 5 1 6 4l5 14v10H10z" fill="{YEL}" {s}/><circle cx="20" cy="50" r="5" fill="#fff" {s}/><circle cx="44" cy="50" r="5" fill="#fff" {s}/>',
        "baby": f'<circle cx="32" cy="30" r="18" fill="#FFE3D1" {s}/><circle cx="25" cy="30" r="2.5" fill="{NAVY}"/><circle cx="39" cy="30" r="2.5" fill="{NAVY}"/><path d="M27 38c3 3 7 3 10 0M28 12c2 4 6 4 8 0" fill="none" {s}/>',
        "fire": f'<path d="M32 56c-12 0-18-8-16-18 2-8 9-12 8-22 8 4 12 10 12 16 3-2 4-6 4-9 6 5 9 12 7 19-2 9-8 14-15 14z" fill="#FF9A5C" {s}/>',
        "bolt": f'<path d="M36 8L16 36h14l-4 20 22-30H34z" fill="{YEL}" {s}/>',
    }.get(name)
    if body is None:
        body = f'<circle cx="32" cy="32" r="22" fill="{YEL}" {s}/>'
    return f'<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">{body}</svg>'


# ============================================================
# 4. 텍스트 서식
# ============================================================
def fmt(s):
    """**굵게**, ==형광펜==, \\n 줄바꿈"""
    s = html.escape(str(s))
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"==(.+?)==", r'<span class="mk">\1</span>', s)
    return s.replace("\\n", "<br>").replace("\n", "<br>")


def strip_fmt(s):
    return re.sub(r"\*\*|==", "", str(s)).replace("\\n", " ").replace("\n", " ")


# ============================================================
# 5. 슬라이드 템플릿
# ============================================================
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#ddd;width:1080px}
.slide{width:1080px;height:__H__px;position:relative;overflow:hidden;background:#FFF7E8;
 background-image:radial-gradient(#F3E3C4 2.6px,transparent 3px);background-size:46px 46px;background-position:23px 23px;
 font-family:'Pretendard','Noto Sans CJK KR',sans-serif;color:#1E2A3B;word-break:keep-all;overflow-wrap:anywhere;--k:1}
b{font-weight:800}
.mk{background:linear-gradient(transparent 56%,#FFD34D 56%,#FFD34D 94%,transparent 94%);padding:0 4px}
.jua{font-family:'Jua','Noto Sans CJK KR'}
/* 공통 머리·꼬리 */
.top{position:absolute;left:60px;right:60px;top:54px;display:flex;justify-content:space-between;align-items:center;z-index:5}
.brandchip{display:flex;align-items:center;gap:10px;font-family:'Jua','Noto Sans CJK KR';font-size:30px;color:#15315B;background:#fff;border:3px solid #15315B;border-radius:999px;padding:9px 24px 7px 14px}
.brandchip svg{width:38px;height:38px}
.count{font-family:'Jua','Noto Sans CJK KR';font-size:30px;color:#15315B;background:#FFD34D;border:3px solid #15315B;border-radius:999px;padding:6px 20px 4px}
.cat{font-family:'Jua','Noto Sans CJK KR';font-size:30px;color:#fff;background:#15315B;border-radius:999px;padding:9px 24px 7px}
.foot{position:absolute;left:60px;right:60px;bottom:40px;display:flex;justify-content:space-between;align-items:flex-end;gap:30px;font-size:25px;color:#7A8497;font-weight:500;z-index:5}
.foot .h{font-family:'Jua','Noto Sans CJK KR';font-size:28px;color:#15315B;white-space:nowrap}
/* 제목 */
.head{position:absolute;left:60px;top:150px;right:60px;z-index:3}
.head.withchar{right:__HR__px}
.title{font-family:'Jua','Noto Sans CJK KR';font-size:82px;line-height:1.16;color:#15315B;letter-spacing:-1px}
.subtitle{margin-top:18px;font-size:36px;font-weight:600;color:#46546A;line-height:1.4}
/* 캐릭터 */
.char{position:absolute;z-index:4;display:flex;align-items:flex-end;justify-content:center}
.char svg,.char img{width:100%;height:100%;object-fit:contain;object-position:bottom}
.char.tr{right:26px;top:__CT__px;width:__CS__px;height:__CS__px}
.char.br{right:18px;bottom:78px;width:400px;height:400px}
/* 말풍선 */
.bubble{position:absolute;z-index:6;background:#fff;border:4px solid #15315B;border-radius:34px;padding:18px 30px 14px;
 font-family:'Jua','Noto Sans CJK KR';font-size:44px;line-height:1.25;color:#15315B;box-shadow:0 8px 0 rgba(21,49,91,.12)}
.bubble:before,.bubble:after{content:"";position:absolute;width:0;height:0;border-style:solid}
.bubble.r:before{right:-30px;top:calc(50% - 18px);border-width:18px 0 18px 30px;border-color:transparent transparent transparent #15315B}
.bubble.r:after{right:-22px;top:calc(50% - 13px);border-width:13px 0 13px 23px;border-color:transparent transparent transparent #fff}
.bubble.b:before{left:60%;bottom:-30px;border-width:30px 18px 0 18px;border-color:#15315B transparent transparent transparent}
.bubble.b:after{left:calc(60% + 5px);bottom:-22px;border-width:23px 13px 0 13px;border-color:#fff transparent transparent transparent}
/* 본문 카드 */
.body{position:absolute;left:60px;right:60px;z-index:2;display:flex;flex-direction:column;gap:calc(22px*var(--k))}
.card{background:#fff;border:3.5px solid #15315B;border-radius:36px;box-shadow:0 10px 0 #EBD8B3}
.callout{display:flex;gap:18px;align-items:flex-start;border-radius:28px;padding:calc(24px*var(--k)) 30px;font-size:calc(34px*var(--k));font-weight:700;line-height:1.42}
.callout svg{flex:none;width:calc(54px*var(--k));height:calc(54px*var(--k));margin-top:-2px}
.callout.tip{background:#E4F0FF;color:#15315B}
.callout.warn{background:#FFE4E1;color:#B7342B;border:3.5px solid #E0554B}
.callout.good{background:#DDF5EA;color:#1D6B47}
.note{font-size:calc(27px*var(--k));color:#5E6A7E;line-height:1.5;font-weight:500;padding:0 8px}
/* list */
.rows{padding:calc(10px*var(--k)) 30px}
.row{display:flex;gap:24px;align-items:center;padding:calc(20px*var(--k)) 0;border-bottom:3px dashed #E6DCC8}
.row:last-child{border-bottom:0}
.lc{flex:none;width:150px;display:flex;flex-direction:column;align-items:center;gap:6px}
.ic{flex:none;width:calc(70px*var(--k));height:calc(70px*var(--k));border-radius:22px;background:#FFF4CC;display:flex;align-items:center;justify-content:center}
.ic svg{width:76%;height:76%}
.lab{font-family:'Jua','Noto Sans CJK KR';font-size:calc(29px*var(--k));color:#23497F;text-align:center;line-height:1.2}
.vc{flex:1;min-width:0}
.val{font-size:calc(40px*var(--k));font-weight:800;line-height:1.3}
.sub{font-size:calc(28px*var(--k));font-weight:600;color:#5E6A7E;line-height:1.42;margin-top:4px}
/* timeline */
.tl{padding:calc(20px*var(--k)) 30px;position:relative}
.tl-row{display:flex;gap:26px;align-items:flex-start;padding:calc(16px*var(--k)) 14px;border-radius:24px;position:relative}
.tl-row.hl{background:#FFF1BF}
.tl-date{flex:none;width:270px;text-align:center;font-family:'Jua','Noto Sans CJK KR';font-size:calc(36px*var(--k));color:#fff;background:#15315B;border-radius:18px;padding:calc(12px*var(--k)) 8px calc(9px*var(--k))}
.tl-row.hl .tl-date{background:#15315B;box-shadow:0 0 0 4px #FFD34D}
.tl-t{font-size:calc(40px*var(--k));font-weight:800;line-height:1.3}
.tl-d{font-size:calc(29px*var(--k));font-weight:600;color:#5E6A7E;margin-top:4px;line-height:1.4}
/* table */
.tbl{display:grid;border-radius:32px;overflow:hidden}
.tbl .c{padding:calc(20px*var(--k)) 20px;font-size:calc(32px*var(--k));font-weight:600;line-height:1.38;border-bottom:3px dashed #E6DCC8;display:flex;flex-direction:column;justify-content:center}
.tbl .c.l{font-family:'Jua','Noto Sans CJK KR';font-weight:400;color:#23497F;font-size:calc(32px*var(--k))}
.tbl .c.hd{background:#15315B;color:#fff;font-family:'Jua','Noto Sans CJK KR';font-weight:400;font-size:calc(40px*var(--k));text-align:center;align-items:center;border-bottom:0;padding:calc(22px*var(--k)) 14px}
.tbl .c.hd.l{color:#C9D6EA}
.tbl .c.ac{background:#FFF6D5}
.tbl .c.hd.ac{background:#FFD34D;color:#15315B}
.tbl .c.last{border-bottom:0}
.tbl .c b{font-size:1.28em;color:#15315B}
.badge{display:inline-block;margin-top:6px;font-family:'Jua','Noto Sans CJK KR';font-size:26px;background:#E0554B;color:#fff;border-radius:999px;padding:4px 14px 2px}
/* bignum */
.formula{align-self:flex-start;font-family:'Jua','Noto Sans CJK KR';font-size:calc(40px*var(--k));color:#15315B;background:#fff;border:3.5px solid #15315B;border-radius:999px;padding:14px 34px 10px}
.nums{display:flex;gap:26px}
.num{flex:1;padding:calc(30px*var(--k)) 26px;text-align:center}
.num.ac{background:#FFF1BF}
.num .nl{font-family:'Jua','Noto Sans CJK KR';font-size:calc(36px*var(--k));color:#23497F}
.num .nv{font-family:'Jua','Noto Sans CJK KR';font-size:calc(96px*var(--k));color:#15315B;line-height:1.15;margin:8px 0 2px;white-space:nowrap}
.num .ns{font-size:calc(28px*var(--k));font-weight:600;color:#5E6A7E}
/* steps */
.intro{font-size:calc(34px*var(--k));font-weight:600;line-height:1.5;color:#2B3950;padding:0 6px}
.steps{padding:calc(16px*var(--k)) 30px}
.st{display:flex;gap:24px;align-items:flex-start;padding:calc(18px*var(--k)) 0;position:relative}
.st:not(:last-child):after{content:"";position:absolute;left:35px;top:calc(92px*var(--k));bottom:calc(-14px*var(--k));border-left:4px dotted #C9B98F}
.st-n{flex:none;width:72px;height:72px;border-radius:50%;background:#FFD34D;border:3.5px solid #15315B;font-family:'Jua','Noto Sans CJK KR';font-size:40px;color:#15315B;display:flex;align-items:center;justify-content:center;padding-top:4px}
.st-t{font-size:calc(40px*var(--k));font-weight:800;line-height:1.3}
.st-t .dt{font-family:'Jua','Noto Sans CJK KR';font-weight:400;font-size:.8em;color:#fff;background:#23497F;border-radius:12px;padding:2px 12px 0;margin-left:10px;vertical-align:3px;white-space:nowrap}
.st-d{font-size:calc(29px*var(--k));font-weight:600;color:#5E6A7E;margin-top:4px;line-height:1.42}
.chipbox{padding:calc(22px*var(--k)) 28px}
.chipbox .ct{font-family:'Jua','Noto Sans CJK KR';font-size:calc(32px*var(--k));color:#23497F;margin-bottom:12px}
.chips{display:flex;flex-wrap:wrap;gap:10px}
.chips span{font-size:calc(28px*var(--k));font-weight:700;background:#F2F5FA;border:2.5px solid #C9D3E3;border-radius:999px;padding:6px 18px}
/* faq */
.qa{padding:calc(26px*var(--k)) 30px}
.q{display:flex;gap:16px;align-items:flex-start;font-size:calc(36px*var(--k));font-weight:800;line-height:1.36;color:#15315B}
.q i,.a i{flex:none;font-style:normal;font-family:'Jua','Noto Sans CJK KR';width:54px;height:54px;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:34px;padding-top:3px}
.q i{background:#FFD34D;color:#15315B}
.a{display:flex;gap:16px;align-items:flex-start;font-size:calc(31px*var(--k));font-weight:600;line-height:1.46;color:#2B3950;margin-top:14px}
.a i{background:#E4F0FF;color:#23497F}
/* text */
.para{font-size:calc(36px*var(--k));line-height:1.55;font-weight:600;color:#2B3950;padding:calc(30px*var(--k)) 36px}
.para p+p{margin-top:18px}
/* cover */
.cover .pill{position:absolute;left:60px;top:188px;font-family:'Jua','Noto Sans CJK KR';font-size:46px;color:#15315B;background:#FFD34D;border:4px solid #15315B;border-radius:999px;padding:12px 34px 8px;box-shadow:0 8px 0 #15315B}
.cover .ct{position:absolute;left:60px;right:60px;top:300px;font-family:'Jua','Noto Sans CJK KR';font-size:__CTF__px;line-height:1.12;color:#15315B;letter-spacing:-2px}
.cover .ct div,.ending .et div{white-space:nowrap}
.cover .cs{position:absolute;left:62px;right:60px;font-size:44px;font-weight:700;color:#46546A;line-height:1.4}
.cover .sun{position:absolute;right:-120px;bottom:-60px;width:760px;height:760px;border-radius:50%;background:#FFE7A3}
.cover .sun2{position:absolute;right:40px;bottom:120px;width:520px;height:520px;border-radius:50%;background:#FFD970}
.cover .char.big{right:26px;bottom:36px}
.cover .swipe{position:absolute;left:60px;bottom:64px;font-family:'Jua','Noto Sans CJK KR';font-size:36px;color:#fff;background:#15315B;border-radius:999px;padding:16px 34px 12px;z-index:6}
.cover .hd2{position:absolute;left:66px;bottom:150px;font-family:'Jua','Noto Sans CJK KR';font-size:32px;color:#15315B;z-index:6}
/* ending */
.ending .et{position:absolute;left:60px;right:60px;top:160px;font-family:'Jua','Noto Sans CJK KR';font-size:98px;line-height:1.18;color:#15315B;letter-spacing:-1px}
.ending .echips{position:absolute;left:60px;right:60px;display:flex;flex-wrap:wrap;gap:14px}
.ending .echips span{font-family:'Jua','Noto Sans CJK KR';font-size:36px;color:#15315B;background:#fff;border:3.5px solid #15315B;border-radius:999px;padding:12px 26px 8px;box-shadow:0 6px 0 #EBD8B3}
.ending .emid{position:absolute;left:60px;width:560px;bottom:300px;display:flex;flex-direction:column;justify-content:center;gap:22px;z-index:5}
.ending .ecard{display:flex;gap:18px;align-items:center;background:#fff;border:3.5px solid #15315B;border-radius:30px;box-shadow:0 8px 0 #EBD8B3;padding:24px 26px;font-size:32px;font-weight:700;color:#15315B;line-height:1.42}
.ending .ecard svg{flex:none;width:64px;height:64px}
.ending .char.big{right:0;bottom:288px;width:480px;height:480px}
.ending .disc{position:absolute;left:60px;right:60px;bottom:112px;background:rgba(255,255,255,.85);border:3px solid #E6DCC8;border-radius:28px;padding:22px 28px;font-size:25px;line-height:1.55;color:#5E6A7E;font-weight:500;z-index:5}
.ending .disc b{color:#15315B}
.ending .follow{position:absolute;left:60px;bottom:40px;font-family:'Jua','Noto Sans CJK KR';font-size:30px;color:#15315B;z-index:5}
"""

FIT_JS = """
() => {
  const out = [];
  document.querySelectorAll('.slide').forEach((sl, i) => {
    const st = sl.getBoundingClientRect().top;
    // 1) 표지·마무리 큰 제목: 가장 긴 줄에 맞춰 모든 줄을 같은 크기로
    sl.querySelectorAll('.fitgroup').forEach(g => {
      let s = parseFloat(getComputedStyle(g).fontSize);
      const lines = [...g.querySelectorAll('.fitw')];
      const over = () => lines.some(el => el.scrollWidth > g.clientWidth + 1);
      while (over() && s > 48) { s -= 2; g.style.fontSize = s + 'px'; }
    });
    // 2) 한 줄 요소(큰 숫자 등)는 너비에 맞춰 줄이기
    sl.querySelectorAll('.fitw:not(.fitgroup .fitw)').forEach(el => {
      let s = parseFloat(getComputedStyle(el).fontSize);
      while (el.scrollWidth > el.clientWidth + 1 && s > 36) { s -= 2; el.style.fontSize = s + 'px'; }
    });
    const body = sl.querySelector('.body'), head = sl.querySelector('.head');
    let k = 1, over = false;
    if (body) {
      // 3) 제목이 길어 본문과 겹치면 본문을 아래로
      if (head) {
        const hb = head.getBoundingClientRect().bottom - st;
        if (hb + 30 > parseFloat(body.style.top)) body.style.top = (hb + 30) + 'px';
      }
      const limit = __LIMIT__ - parseFloat(body.style.top);
      // 4) 본문이 넘치면 글자 비율(--k)을 줄이기
      while (body.scrollHeight > limit && k > 0.62) { k -= 0.03; sl.style.setProperty('--k', k.toFixed(2)); }
      over = body.scrollHeight > limit;
      // 5) 남는 공간이 많으면 본문을 조금 내려 균형 맞추기
      const spare = limit - body.scrollHeight;
      if (!over && spare > 140) body.style.top = (parseFloat(body.style.top) + Math.min(110, spare * 0.35)) + 'px';
    }
    const r = e => e ? e.getBoundingClientRect() : null;
    const bd = r(body), foot = r(sl.querySelector('.foot'));
    const issues = [];
    if (over) issues.push('본문이 칸을 넘쳐요');
    if (bd && foot && bd.bottom > foot.top - 4) issues.push('본문이 꼬리말에 닿아요');
    sl.querySelectorAll('.fitw').forEach(el => { if (el.scrollWidth > el.clientWidth + 2) issues.push('큰 글자가 잘려요: ' + el.textContent.slice(0, 12)); });
    out.push({i: i + 1, k: +k.toFixed(2), issues});
  });
  return out;
}
"""


def _top(sp, idx, total, cfg):
    return (f'<div class="top"><div class="brandchip">{icon("acorn")}{html.escape(cfg["brand"])}</div>'
            f'<div class="count">{idx}/{total}</div></div>')


def _foot(sp, cfg):
    src = sp.get("source", cfg.get("source", ""))
    h = f'<div class="h">{html.escape(cfg["handle"])}</div>' if cfg.get("handle") else ""
    return f'<div class="foot"><div>{fmt(src)}</div>{h}</div>'


def _head_block(sp, chars, pos="tr"):
    has_char = bool(sp.get("pose")) and pos == "tr"
    sub = f'<div class="subtitle">{fmt(sp["subtitle"])}</div>' if sp.get("subtitle") else ""
    head = f'<div class="head{" withchar" if has_char else ""}"><div class="title">{fmt(sp.get("title", ""))}</div>{sub}</div>'
    ch = f'<div class="char {pos}">{chars.html(sp["pose"])}</div>' if sp.get("pose") else ""
    return head + ch


def _callout(c):
    if not c:
        return ""
    t = c.get("type", "tip")
    ic = {"tip": "star", "warn": "warn", "good": "check"}.get(t, "star")
    return f'<div class="callout {t}">{icon(ic)}<div>{fmt(c["text"])}</div></div>'


def _note(sp):
    return f'<div class="note">{fmt(sp["note"])}</div>' if sp.get("note") else ""


def _body(inner, top_px, bottom_px=None):
    bottom_px = bottom_px or H - 140
    return f'<div class="body" style="top:{top_px}px" data-max="{bottom_px - top_px}">{inner}</div>'


def body_top(sp):
    """제목 줄 수에 따라 본문 시작 위치를 정해요 (캐릭터가 오른쪽 위에 있으면 최소 500)"""
    lines = str(sp.get("title", "")).count("\\n") + str(sp.get("title", "")).count("\n") + 1
    t = 150 + 96 * lines + (70 if sp.get("subtitle") else 0) + 40
    if sp.get("pose") and sp.get("char_pos", "tr") == "tr":
        t = max(t, L["body_min"])
    return sp.get("body_top", t)


def slide_cover(sp, idx, total, cfg, chars):
    lines = sp.get("lines", [])
    hl = set(sp.get("highlight", []))
    t = "".join(f'<div class="fitw">{"<span class=mk>" if i in hl else ""}{fmt(l)}{"</span>" if i in hl else ""}</div>' for i, l in enumerate(lines))
    cs_top = 300 + int(len(lines) * L["cover_title"] * 1.12) + 26
    cs_bottom = cs_top + (64 if sp.get("sub") else 0) + 24
    size = max(400, min(660, H - 36 - cs_bottom))
    btop = min(max(cs_bottom + 70, 880), H - 370)
    bubble = f'<div class="bubble r" style="left:64px;top:{btop}px">{fmt(sp["bubble"])}</div>' if sp.get("bubble") else ""
    cat = f'<div class="cat">{html.escape(cfg.get("category", ""))}</div>' if cfg.get("category") else "<div></div>"
    return (f'<section class="slide cover"><div class="sun"></div><div class="sun2"></div>'
            f'<div class="top"><div class="brandchip">{icon("acorn")}{html.escape(cfg["brand"])}</div>{cat}</div>'
            + (f'<div class="pill">{fmt(sp["pill"])}</div>' if sp.get("pill") else "")
            + f'<div class="ct fitgroup">{t}</div>'
            + (f'<div class="cs" style="top:{cs_top}px">{fmt(sp["sub"])}</div>' if sp.get("sub") else "")
            + f'<div class="char big" style="width:{size}px;height:{size}px">{chars.html(sp.get("pose", "megaphone"))}</div>{bubble}'
            + (f'<div class="hd2">{html.escape(cfg["handle"])}</div>' if cfg.get("handle") else "")
            + f'<div class="swipe">{html.escape(sp.get("swipe", "넘겨서 확인하기 →"))}</div></section>')


def slide_list(sp, idx, total, cfg, chars):
    rows = "".join(
        f'<div class="row"><div class="lc"><div class="ic">{icon(r.get("icon", "check"))}</div>'
        f'<div class="lab">{fmt(r.get("label", ""))}</div></div><div class="vc">'
        f'<div class="val">{fmt(r.get("value", ""))}</div>'
        + (f'<div class="sub">{fmt(r["sub"])}</div>' if r.get("sub") else "") + '</div></div>'
        for r in sp["rows"])
    inner = f'<div class="card rows">{rows}</div>{_callout(sp.get("callout"))}{_note(sp)}'
    return (f'<section class="slide">{_top(sp, idx, total, cfg)}{_head_block(sp, chars)}'
            f'{_body(inner, body_top(sp))}{_foot(sp, cfg)}</section>')


def slide_timeline(sp, idx, total, cfg, chars):
    rows = "".join(
        f'<div class="tl-row{" hl" if r.get("hl") else ""}"><div class="tl-date">{fmt(r["date"])}</div><div>'
        f'<div class="tl-t">{fmt(r.get("title", ""))}</div>'
        + (f'<div class="tl-d">{fmt(r["desc"])}</div>' if r.get("desc") else "") + '</div></div>'
        for r in sp["rows"])
    inner = f'<div class="card tl">{rows}</div>{_callout(sp.get("callout"))}{_note(sp)}'
    return (f'<section class="slide">{_top(sp, idx, total, cfg)}{_head_block(sp, chars)}'
            f'{_body(inner, body_top(sp))}{_foot(sp, cfg)}</section>')


def slide_table(sp, idx, total, cfg, chars):
    headers, rows = sp.get("headers"), sp["rows"]
    n = len(rows[0])
    ac = sp.get("accent")
    lab_w = sp.get("label_width", 230 if n > 2 else 300)
    cols = f"{lab_w}px " + " ".join(["1fr"] * (n - 1))
    cells = []
    if headers:
        for j, h in enumerate(headers):
            badge = ""
            if sp.get("badge") and sp["badge"].get("col") == j:
                badge = f'<span class="badge">{html.escape(sp["badge"]["text"])}</span>'
            cells.append(f'<div class="c hd{" l" if j == 0 else ""}{" ac" if j == ac else ""}"><div>{fmt(h)}</div>{badge}</div>')
    for i, r in enumerate(rows):
        last = " last" if i == len(rows) - 1 else ""
        for j, v in enumerate(r):
            cells.append(f'<div class="c{" l" if j == 0 else ""}{" ac" if j == ac else ""}{last}"><div>{fmt(v)}</div></div>')
    inner = (f'<div class="card"><div class="tbl" style="grid-template-columns:{cols}">{"".join(cells)}</div></div>'
             f'{_callout(sp.get("callout"))}{_note(sp)}')
    return (f'<section class="slide">{_top(sp, idx, total, cfg)}{_head_block(sp, chars)}'
            f'{_body(inner, body_top(sp))}{_foot(sp, cfg)}</section>')


def slide_bignum(sp, idx, total, cfg, chars):
    items = "".join(
        f'<div class="card num{" ac" if it.get("accent") else ""}"><div class="nl">{fmt(it.get("label", ""))}</div>'
        f'<div class="nv fitw">{fmt(it["value"])}</div><div class="ns">{fmt(it.get("sub", ""))}</div></div>'
        for it in sp["items"])
    formula = f'<div class="formula">{fmt(sp["formula"])}</div>' if sp.get("formula") else ""
    inner = f'{formula}<div class="nums">{items}</div>{_callout(sp.get("callout"))}{_note(sp)}'
    return (f'<section class="slide">{_top(sp, idx, total, cfg)}{_head_block(sp, chars)}'
            f'{_body(inner, body_top(sp))}{_foot(sp, cfg)}</section>')


def slide_steps(sp, idx, total, cfg, chars):
    steps = "".join(
        f'<div class="st"><div class="st-n">{i + 1}</div><div><div class="st-t">{fmt(s["title"])}'
        + (f'<span class="dt">{fmt(s["date"])}</span>' if s.get("date") else "") + '</div>'
        + (f'<div class="st-d">{fmt(s["desc"])}</div>' if s.get("desc") else "") + '</div></div>'
        for i, s in enumerate(sp["steps"]))
    intro = f'<div class="intro">{fmt(sp["intro"])}</div>' if sp.get("intro") else ""
    chips = ""
    if sp.get("chips"):
        ct = f'<div class="ct">{fmt(sp["chips_title"])}</div>' if sp.get("chips_title") else ""
        chips = f'<div class="card chipbox">{ct}<div class="chips">{"".join(f"<span>{html.escape(c)}</span>" for c in sp["chips"])}</div></div>'
    inner = f'{intro}<div class="card steps">{steps}</div>{chips}{_callout(sp.get("callout"))}{_note(sp)}'
    return (f'<section class="slide">{_top(sp, idx, total, cfg)}{_head_block(sp, chars)}'
            f'{_body(inner, body_top(sp))}{_foot(sp, cfg)}</section>')


def slide_faq(sp, idx, total, cfg, chars):
    qa = "".join(f'<div class="card qa"><div class="q"><i>Q</i><div>{fmt(it["q"])}</div></div>'
                 f'<div class="a"><i>A</i><div>{fmt(it["a"])}</div></div></div>' for it in sp["items"])
    inner = f'{qa}{_note(sp)}'
    return (f'<section class="slide">{_top(sp, idx, total, cfg)}{_head_block(sp, chars)}'
            f'{_body(inner, body_top(sp))}{_foot(sp, cfg)}</section>')


def slide_text(sp, idx, total, cfg, chars):
    paras = "".join(f"<p>{fmt(p)}</p>" for p in sp.get("paras", []))
    inner = f'<div class="card para">{paras}</div>{_callout(sp.get("callout"))}{_note(sp)}'
    return (f'<section class="slide">{_top(sp, idx, total, cfg)}{_head_block(sp, chars)}'
            f'{_body(inner, body_top(sp))}{_foot(sp, cfg)}</section>')


def slide_ending(sp, idx, total, cfg, chars):
    lines = sp.get("lines", ["저장해 두고", "필요할 때 다시 봐요!"])
    hl = set(sp.get("highlight", []))
    t = "".join(f'<div class="fitw">{"<span class=mk>" if i in hl else ""}{fmt(l)}{"</span>" if i in hl else ""}</div>' for i, l in enumerate(lines))
    y = 160 + int(len(lines) * 98 * 1.18) + 36
    chips = ""
    if sp.get("chips"):
        chips = f'<div class="echips" style="top:{y}px">{"".join(f"<span>{fmt(c)}</span>" for c in sp["chips"])}</div>'
        y += 92 * ((len(sp["chips"]) + 2) // 3) + 20
    cards = [("bookmark", sp.get("save", "오른쪽 아래 **저장**을 눌러 두면\n필요할 때 바로 찾아요"))]
    if sp.get("link"):
        cards.append(("link", sp["link"]))
    mid = "".join(f'<div class="ecard">{icon(ic)}<div>{fmt(tx)}</div></div>' for ic, tx in cards)
    disc = sp.get("disclaimer") or cfg.get("disclaimer", "")
    src = sp.get("source", cfg.get("source", ""))
    disc_html = f'<div class="disc"><b>{fmt(src)}</b><br>{fmt(disc)}</div>' if (disc or src) else ""
    follow_txt = html.escape(sp.get("follow", "팔로우하면 매일 알짜 정보가 와요")) + (f' · {html.escape(cfg["handle"])}' if cfg.get("handle") else "")
    follow = f'<div class="follow">{follow_txt}</div>'
    return (f'<section class="slide ending">{_top(sp, idx, total, cfg)}<div class="et fitgroup">{t}</div>{chips}'
            f'<div class="emid" style="top:{y}px">{mid}</div>'
            f'<div class="char big">{chars.html(sp.get("pose", "thumbsup"))}</div>{disc_html}{follow}</section>')


KINDS = {"cover": slide_cover, "list": slide_list, "timeline": slide_timeline, "table": slide_table,
         "bignum": slide_bignum, "steps": slide_steps, "faq": slide_faq, "text": slide_text, "ending": slide_ending}


# ============================================================
# 6. 렌더링
# ============================================================
def page_css():
    return (CSS.replace("__H__", str(H)).replace("__CT__", str(L["char_top"])).replace("__CS__", str(L["char"]))
               .replace("__HR__", str(L["char"] + 30)).replace("__CTF__", str(L["cover_title"])))


def render(slides_html, out_dir, nm):
    """슬라이드를 Chromium으로 찍어 JPEG(sRGB, 품질 92)로 저장"""
    from playwright.sync_api import sync_playwright
    from PIL import Image
    page_html = f'<!doctype html><html lang="ko"><head><meta charset="utf-8">{font_css(nm)}<style>{page_css()}</style></head><body>{"".join(slides_html)}</body></html>'
    tmp = os.path.abspath(os.path.join(out_dir, "_render.html"))
    open(tmp, "w", encoding="utf-8").write(page_html)
    files = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        pg.goto("file://" + tmp)
        pg.evaluate("document.fonts.ready")
        pg.wait_for_timeout(400)
        report = pg.evaluate(FIT_JS.replace("__LIMIT__", str(H - 140)))
        pg.wait_for_timeout(100)
        for i, el in enumerate(pg.query_selector_all("section.slide")):
            png = os.path.join(out_dir, f"_{i + 1:02d}.png")
            el.screenshot(path=png)
            jpg = os.path.join(out_dir, f"{i + 1:02d}.jpg")
            Image.open(png).convert("RGB").save(jpg, "JPEG", quality=92, optimize=True, subsampling=0)
            os.remove(png)
            files.append(jpg)
        b.close()
    os.remove(tmp)
    return files, report


# ============================================================
# 6-1. 깃허브 자동 게시용 카드 파일·링크 (card.txt)
#      Claude 예약 작업은 깃허브에 직접 올릴 수 없어서, 카드 내용을 압축한 글자 파일을
#      '새 파일 만들기' 링크에 담아요. 야호님이 링크를 눌러 Commit 하면 깃허브 액션이
#      같은 스크립트로 카드를 다시 그려 queue/<게시일>/ 에 넣고, 게시 시각에 올려요.
# ============================================================
CARD_KEYS = ("title", "category", "source", "brand", "disclaimer", "slides", "caption", "hashtags", "alt")


def kit_md5():
    return hashlib.md5(open(os.path.abspath(__file__), "rb").read()).hexdigest()


def make_card(ins, date, publish_at=None, handle=None):
    """카드 내용을 card.txt 글자로 (zlib + base64). 검수용 checklist 등은 빼요"""
    datetime.date.fromisoformat(date)
    publish_at = publish_at or ins.get("publish_at") or f"{date}T12:00:00+09:00"
    datetime.datetime.fromisoformat(publish_at)
    data = {"v": 1, "date": date, "publish_at": publish_at, "handle": handle or ins.get("handle", HANDLE),
            "kit": kit_md5(), "insta": {k: ins[k] for k in CARD_KEYS if k in ins}}
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    return CARD_HEADER + "\n" + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=") + "\n"


def read_card(text):
    """card.txt → dict (make_card 의 반대)"""
    body = "".join(l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#"))
    body += "=" * (-len(body) % 4)
    data = json.loads(zlib.decompress(base64.urlsafe_b64decode(body)).decode())
    if data.get("v") != 1 or "insta" not in data:
        raise ValueError("카드 형식이 달라요")
    return data


def card_link(card_text, date, repo=REPO):
    """깃허브 '새 파일' 화면을 이름·내용이 채워진 채로 여는 링크"""
    return (f"https://github.com/{repo}/new/main?filename=" + urllib.parse.quote(f"queue/{date}/card.txt", safe="/")
            + "&value=" + urllib.parse.quote(card_text, safe=""))


def gh_panel(link, publish_at):
    if not link:
        return ""
    when = publish_at or ""
    try:
        t = datetime.datetime.fromisoformat(publish_at)
        when = f"{t.month}/{t.day} {t:%H:%M}"
    except (TypeError, ValueError):
        pass
    return ('<div class="panel" style="border:3px solid #FFD34D"><p class="lab">깃허브 자동 게시</p>'
            '<p class="meta">카드·캡션을 확인했으면 아래 버튼을 눌러요 → 깃허브 화면 오른쪽 위 <b>Commit changes...</b> → '
            f'창에서 한 번 더 <b>Commit changes</b>. 깃허브가 카드를 만들어 <b>{html.escape(when)}</b>쯤 자동으로 올려요.<br>'
            '이 방법을 쓰면 아래 ④ 앱 예약은 하지 않아요(두 번 올라가요).</p>'
            f'<a class="btn" href="{html.escape(link)}" target="_blank" rel="noopener">깃허브 대기열에 올리기</a></div>\n')


# ============================================================
# 7. 미리보기 페이지 (insta.html)
# ============================================================
PREVIEW = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
body{{margin:0;background:#F3EFE6;font-family:'Pretendard','Apple SD Gothic Neo','Malgun Gothic',sans-serif;color:#1E2A3B}}
.wrap{{max-width:780px;margin:0 auto;padding:20px 16px 60px}}
.panel{{background:#fff;border-radius:16px;padding:18px 20px;margin:0 0 16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.lab{{font-size:12px;font-weight:800;color:#7A8497;letter-spacing:.04em;margin:0 0 8px}}
h1{{font-size:20px;margin:0 0 6px;line-height:1.45}}
.meta{{font-size:14px;line-height:1.8;color:#46546A}}
.car{{display:flex;gap:10px;overflow-x:auto;scroll-snap-type:x mandatory;padding:4px 0 10px}}
.car img{{flex:none;width:min(360px,82vw);border-radius:12px;scroll-snap-align:start;box-shadow:0 2px 8px rgba(0,0,0,.12)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px}}
.grid img{{width:100%;border-radius:8px}}
.cap{{white-space:pre-wrap;font-size:15px;line-height:1.75;background:#FAFAF7;border:1px solid #E6E1D6;border-radius:12px;padding:14px}}
.btn{{display:inline-block;margin:10px 8px 0 0;padding:9px 14px;border-radius:9px;border:0;background:#15315B;color:#fff;font-size:14px;font-weight:700;cursor:pointer;text-decoration:none}}
.btn.alt{{background:#FFD34D;color:#15315B}}
.ok{{color:#1E7B45;font-size:13px;margin-left:6px}}
ol,ul{{margin:0;padding-left:22px}} li{{margin:0 0 8px;line-height:1.6;font-size:15px}}
.alt{{font-size:14px;line-height:1.6;margin:0 0 10px}} .alt b{{color:#15315B}}
.warn{{color:#B7342B;font-weight:700}}
</style></head><body><div class="wrap">
<div class="panel"><p class="lab">인스타 카드뉴스 · {n}장 · {size}</p><h1>{title}</h1>
<div class="meta">{meta}</div></div>
{gh}<div class="panel"><p class="lab">① 미리보기 (옆으로 넘겨 보세요)</p><div class="car">{car}</div>
<p class="meta" style="margin:6px 0 0">사진 파일: <b>insta_slides.zip</b> 안의 01.jpg ~ {last}.jpg (번호 순서대로 선택)</p></div>
<div class="panel"><p class="lab">② 캡션 (해시태그 포함 · {clen}자 / 2,200자)</p><div class="cap" id="cap">{caption}</div>
<button class="btn" onclick="cp('cap')">캡션 복사</button><span class="ok" id="ok-cap"></span>
<div class="cap" id="tg" style="margin-top:12px">{tags}</div>
<button class="btn alt" onclick="cp('tg')">해시태그만 복사</button><span class="ok" id="ok-tg"></span></div>
<div class="panel"><p class="lab">③ 대체 텍스트 (선택 · 고급 설정 → 접근성)</p>{alts}</div>
<div class="panel"><p class="lab">④ 올리는 순서</p><ol>{howto}</ol></div>
<div class="panel"><p class="lab">⑤ 올리기 전 확인 (야호님 검수)</p><ol>{check}</ol></div>
</div>
<script>
function flag(id,m){{var e=document.getElementById('ok-'+id);if(e){{e.textContent=m;setTimeout(function(){{e.textContent=''}},2500)}}}}
function cp(id){{var el=document.getElementById(id),t=el.innerText;
 if(navigator.clipboard&&navigator.clipboard.writeText){{navigator.clipboard.writeText(t).then(function(){{flag(id,'복사됨')}},function(){{fb(el,id)}});}}else fb(el,id)}}
function fb(el,id){{var r=document.createRange();r.selectNodeContents(el);var s=getSelection();s.removeAllRanges();s.addRange(r);
 var ok=false;try{{ok=document.execCommand('copy')}}catch(e){{}}flag(id,ok?'복사됨':'선택됨 — Ctrl+C')}}
</script></body></html>"""

HOWTO = [
    "사진 옮기기: PC 카카오톡 <b>나와의 채팅</b>에 01~{last}.jpg를 한 번에 보내고, 휴대폰에서 <b>전체 저장</b>",
    "인스타 앱 <b>＋</b> → 게시물 → 오른쪽 위 <b>여러 장 선택</b> → 01번부터 번호 순서대로 선택 → 비율이 <b>{ratio}</b>인지 확인",
    "필터 없이 다음 → 캡션 칸에 <b>캡션 복사</b> 내용 붙여넣기",
    "(선택) 고급 설정 → 접근성 → 대체 텍스트 입력",
    "고급 설정(iOS) 또는 옵션 더 보기(Android) → <b>이 게시물 예약</b> → 날짜·시간 → 예약",
]


def ratio_label():
    return "4:5" if H == 1350 else "3:4"


def build_preview(cfg, ins, files, caption, tags, out_dir, lint_msgs, gh_link=None, publish_at=None):
    car = "".join(f'<img src="data:image/{"jpeg" if f.endswith(".jpg") else "png"};base64,{base64.b64encode(open(f, "rb").read()).decode()}" alt="{i + 1}">' for i, f in enumerate(files))
    alts = ""
    for i, a in enumerate(ins.get("alt", [])):
        alts += f'<p class="alt"><b>{i + 1}장</b> <span id="alt{i}">{html.escape(a)}</span> <button class="btn" style="margin:0 0 0 6px;padding:4px 10px;font-size:12px" onclick="cp(\'alt{i}\')">복사</button><span class="ok" id="ok-alt{i}"></span></p>'
    if not alts:
        alts = '<p class="alt">대체 텍스트가 없어요. 넣지 않아도 인스타가 자동으로 만들어요.</p>'
    last = f"{len(files):02d}"
    meta = []
    if ins.get("schedule"):
        meta.append(f"<b>예약 게시</b>: {fmt(ins['schedule'])}")
    meta.append(f"<b>계정</b>: {html.escape(cfg['handle'] or '아이디 미정 (카드에 표시 안 함)')} · <b>카테고리</b>: {html.escape(cfg.get('category', '-'))}")
    for m in lint_msgs:
        meta.append(f'<span class="warn">⚠ {html.escape(m)}</span>')
    check = "".join(f"<li>{fmt(c)}</li>" for c in ins.get("checklist", []))
    check += "<li>1장 표지 글자가 프로필 격자(작은 썸네일)에서도 읽히는지 확인</li>"
    page = PREVIEW.format(title=html.escape(ins.get("title") or strip_fmt(caption.split("\n")[0])), n=len(files), meta="<br>".join(meta),
                          car=car, last=last, caption=html.escape(caption), clen=len(caption), tags=html.escape(" ".join("#" + t for t in tags)),
                          alts=alts, howto="".join(f"<li>{h.format(last=last, ratio=ratio_label())}</li>" for h in HOWTO), check=check,
                          size=f"{W}×{H} ({ratio_label()})", gh=gh_panel(gh_link, publish_at))
    open(os.path.join(out_dir, "insta.html"), "w", encoding="utf-8").write(page)


def contact_sheet(files, path, cols=5, w=360):
    from PIL import Image
    h = int(w * H / W)
    rows = (len(files) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (w + 10) + 10, rows * (h + 10) + 10), "#C8C8C8")
    for i, f in enumerate(files):
        sheet.paste(Image.open(f).convert("RGB").resize((w, h), Image.LANCZOS), (10 + (i % cols) * (w + 10), 10 + (i // cols) * (h + 10)))
    sheet.save(path)


# ============================================================
# 8. 점검 + 빌드
# ============================================================
def lint(ins, caption, tags):
    w = []
    n = len(ins.get("slides", []))
    if not 3 <= n <= MAX_SLIDES:
        w.append(f"슬라이드 {n}장 (3~{MAX_SLIDES}장)")
    if len(tags) > MAX_TAGS:
        w.append(f"해시태그 {len(tags)}개 — 인스타는 게시물당 {MAX_TAGS}개까지만 돼요")
    if len(caption) > MAX_CAPTION:
        w.append(f"캡션 {len(caption)}자 — {MAX_CAPTION}자를 넘어요")
    if "공식 안내가 아니" not in caption and "개인 계정" not in caption:
        w.append("캡션에 '정부기관이 아닌 개인 계정' 안내가 없어요")
    if "출처" not in caption:
        w.append("캡션에 출처가 없어요")
    kinds = [s.get("kind") for s in ins.get("slides", [])]
    if kinds and kinds[0] != "cover":
        w.append("첫 장이 표지(cover)가 아니에요")
    if kinds and kinds[-1] != "ending":
        w.append("마지막 장이 마무리(ending)가 아니에요")
    body = json.dumps(ins, ensure_ascii=False)
    for bad in ("무조건", "100% 지급", "누구나 받는", "공식 계정", "정부 공식", "공식 안내입니다", "TODO", "확인필요"):
        if bad in body:
            w.append(f"'{bad}' 표현이 있어요")
    if ins.get("alt") and len(ins["alt"]) != n:
        w.append(f"대체 텍스트 {len(ins['alt'])}개 ≠ 슬라이드 {n}장")
    return w


def export_queue(files, full_caption, ins, queue_dir, date, publish_at=None):
    """깃허브 자동 게시 대기열: <queue>/<게시일>/01.jpg… + caption.txt + post.json"""
    if len(files) > API_MAX_ITEMS:
        raise SystemExit(f"⚠ 인스타 API 캐러셀은 {API_MAX_ITEMS}장까지예요 (지금 {len(files)}장) — 슬라이드를 줄여 주세요")
    datetime.date.fromisoformat(date)  # 형식 확인 (YYYY-MM-DD)
    publish_at = publish_at or ins.get("publish_at") or f"{date}T12:00:00+09:00"
    datetime.datetime.fromisoformat(publish_at)
    d = os.path.join(queue_dir, date)
    os.makedirs(d, exist_ok=True)
    for old in glob.glob(os.path.join(d, "*.jpg")):
        os.remove(old)
    names = []
    for f in files:
        shutil.copyfile(f, os.path.join(d, os.path.basename(f)))
        names.append(os.path.basename(f))
    open(os.path.join(d, "caption.txt"), "w", encoding="utf-8").write(full_caption + "\n")
    post = {"id": date, "publish_at": publish_at, "title": strip_fmt(ins.get("title", "")),
            "images": names, "alt": [strip_fmt(a)[:1000] for a in ins.get("alt", [])][:len(names)],
            "caption_file": "caption.txt", "hold": False,
            "made_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat(timespec="seconds")}
    json.dump(post, open(os.path.join(d, "post.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"대기열 → {d}  (게시 {publish_at}, {len(names)}장)")
    return d


def build(spec, out_root, char_dir=None, handle=None, ratio="4x5", queue_dir=None, date=None, publish_at=None,
          link=False, repo=REPO):
    set_ratio(ratio)
    ins = spec.get("insta", spec)
    cfg = {"brand": ins.get("brand", BRAND), "handle": handle or ins.get("handle", HANDLE),
           "category": ins.get("category", ""), "source": ins.get("source", ""),
           "disclaimer": ins.get("disclaimer", "")}
    out = os.path.join(out_root, "insta")
    os.makedirs(out, exist_ok=True)
    for old in glob.glob(os.path.join(out, "[0-9][0-9].png")) + glob.glob(os.path.join(out, "[0-9][0-9].jpg")):
        os.remove(old)
    nm = ensure_fonts()
    chars = Characters(char_dir or os.environ.get("ALZZA_CHAR_DIR"), os.path.expanduser("~/.cache/insta_char_cut"))
    slides = ins["slides"]
    total = len(slides)
    htmls = [KINDS[sp["kind"]](sp, i + 1, total, cfg, chars) for i, sp in enumerate(slides)]
    files, report = render(htmls, out, nm)

    tags = [t.lstrip("#") for t in ins.get("hashtags", [])]
    caption = ins.get("caption", "").strip()
    full_caption = caption + ("\n\n" + " ".join("#" + t for t in tags) if tags else "")
    msgs = lint(ins, full_caption, tags)
    if queue_dir and len(files) > API_MAX_ITEMS:
        msgs.append(f"자동 게시는 {API_MAX_ITEMS}장까지예요 (지금 {len(files)}장)")
    for r in report:
        for iss in r["issues"]:
            msgs.append(f"{r['i']}장: {iss}")
        if r["k"] < 0.8:
            msgs.append(f"{r['i']}장: 글자가 많아 {int(r['k'] * 100)}%로 줄였어요 — 내용을 덜어 내는 게 좋아요")
    open(os.path.join(out, "caption.txt"), "w", encoding="utf-8").write(full_caption + "\n")
    if ins.get("alt"):
        open(os.path.join(out, "alt.txt"), "w", encoding="utf-8").write(
            "\n".join(f"{i + 1}. {a}" for i, a in enumerate(ins["alt"])) + "\n")
    gh = None
    if link:
        if not date:
            raise SystemExit("⚠ --link 에는 --date YYYY-MM-DD(게시일)가 필요해요")
        if len(files) > API_MAX_ITEMS:
            msgs.append(f"자동 게시는 {API_MAX_ITEMS}장까지예요 (지금 {len(files)}장)")
        card = make_card(ins, date, publish_at, cfg["handle"])
        gh = card_link(card, date, repo)
        open(os.path.join(out, "card.txt"), "w", encoding="utf-8").write(card)
        open(os.path.join(out, "github_link.txt"), "w", encoding="utf-8").write(gh + "\n")
        if len(gh) > LINK_WARN:
            msgs.append(f"깃허브 링크가 {len(gh):,}자로 길어요 — 슬라이드 글을 줄여 주세요")
        publish_at = publish_at or ins.get("publish_at") or f"{date}T12:00:00+09:00"
    build_preview(cfg, ins, files, full_caption, tags, out, msgs, gh, publish_at)
    contact_sheet(files, os.path.join(out, "_sheet.png"))
    with zipfile.ZipFile(os.path.join(out, "insta_slides.zip"), "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, os.path.basename(f))
        z.write(os.path.join(out, "caption.txt"), "caption.txt")
    for m in msgs:
        print("⚠ 점검:", m)
    src = "포즈 이미지 " + (", ".join(sorted(chars.used)) if chars.used else "없음")
    if chars.missing:
        src += f" / 임시 캐릭터 사용: {', '.join(sorted(chars.missing))}"
    print(f"OK → {out}  ({W}×{H}, 슬라이드 {len(files)}장, 캡션 {len(full_caption)}자, 해시태그 {len(tags)}개, {src})")
    if gh:
        print(f"깃허브 링크 → {os.path.join(out, 'github_link.txt')}  ({len(gh):,}자)")
    if queue_dir:
        if not date:
            raise SystemExit("⚠ --queue 에는 --date YYYY-MM-DD(게시일)가 필요해요")
        export_queue(files, full_caption, ins, queue_dir, date, publish_at)
    return files, msgs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("out")
    ap.add_argument("--char-dir")
    ap.add_argument("--handle")
    ap.add_argument("--ratio", choices=sorted(LAYOUTS), default="4x5")
    ap.add_argument("--queue", help="깃허브 자동 게시 대기열 폴더 (예: alzza-insta/queue)")
    ap.add_argument("--date", help="게시일 YYYY-MM-DD (--queue와 함께)")
    ap.add_argument("--publish-at", help="게시 시각 ISO, 기본 <게시일>T12:00:00+09:00")
    ap.add_argument("--link", action="store_true", help="깃허브 자동 게시용 card.txt·github_link.txt 만들기 (--date 필요)")
    ap.add_argument("--repo", default=REPO, help="깃허브 저장소 (기본 %(default)s)")
    ap.add_argument("--card", action="store_true", help="spec 대신 card.txt 를 읽어요 (깃허브 액션용)")
    a = ap.parse_args()
    if a.card:
        c = read_card(open(a.spec, encoding="utf-8").read())
        build({"insta": c["insta"]}, a.out, a.char_dir, a.handle or c.get("handle"), a.ratio, a.queue,
              a.date or c["date"], a.publish_at or c.get("publish_at"))
    else:
        build(json.load(open(a.spec, encoding="utf-8")), a.out, a.char_dir, a.handle,
              a.ratio, a.queue, a.date, a.publish_at, a.link, a.repo)
