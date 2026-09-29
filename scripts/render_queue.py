#!/usr/bin/env python3
"""인스타 카드 만들기 — queue/<게시일>/card.txt 를 읽어 카드(JPEG)·caption.txt·post.json 을 만들어요.

Claude 예약 작업은 깃허브에 직접 올릴 수 없어서, 카드 내용을 압축한 card.txt 를 '새 파일 만들기' 링크로
보내요. 야호님이 링크를 눌러 Commit 하면 깃허브 액션(.github/workflows/render.yml)이 이 스크립트로
kit/insta_card_kit.py 를 돌려 Claude가 미리 보여 준 것과 같은 카드를 다시 그려요.

직접 돌릴 때: python3 scripts/render_queue.py   (Pillow·numpy·scipy·playwright(Chromium)·npm 필요)
"""
import glob
import importlib.util
import os
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True  # kit/__pycache__ 가 저장소에 생기지 않게

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE = os.path.join(ROOT, "queue")
POSTED = os.path.join(ROOT, "posted")
KIT = os.path.join(ROOT, "kit", "insta_card_kit.py")
POSES = os.path.join(ROOT, "kit", "poses")


def log(msg):
    print(msg, flush=True)


def summary(md):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def set_aside(path):
    """같은 실패가 매번 되풀이되지 않게 card.txt 이름을 바꿔 둬요 (링크를 다시 누르면 새로 올라가요)"""
    dest = os.path.join(os.path.dirname(path), "card_실패.txt")
    if os.path.exists(dest):
        os.remove(dest)
    os.rename(path, dest)


def load_kit():
    spec = importlib.util.spec_from_file_location("insta_card_kit", KIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    cards = sorted(glob.glob(os.path.join(QUEUE, "*", "card.txt")))
    todo = [c for c in cards if not os.path.exists(os.path.join(os.path.dirname(c), "post.json"))]
    log(f"card.txt {len(cards)}개 · 새로 만들 것 {len(todo)}개")
    if not todo:
        return 0

    kit = load_kit()
    if not kit.ensure_fonts():
        summary("### ❌ 글꼴(Jua·Pretendard)을 설치하지 못해 카드를 만들지 않았어요\n다음에 card.txt를 올리거나 Actions에서 다시 실행하면 다시 시도해요.")
        log("❌ 글꼴 설치 실패 — 모양이 달라질 수 있어 멈춰요")
        return 1
    poses = POSES if glob.glob(os.path.join(POSES, "pose_*.png")) else None
    failed = 0
    for path in todo:
        folder = os.path.dirname(path)
        pid = os.path.basename(folder)
        try:
            card = kit.read_card(open(path, encoding="utf-8").read())
        except Exception as e:  # 링크가 잘렸거나 내용이 바뀐 경우
            failed += 1
            set_aside(path)
            log(f"❌ {pid}: card.txt를 읽을 수 없어요 ({e})")
            summary(f"### ❌ {pid}: card.txt를 읽을 수 없어요\n링크 내용이 잘렸거나 바뀐 것 같아요. "
                    f"Claude가 보낸 링크를 다시 눌러 올려 주세요(읽지 못한 파일은 card_실패.txt로 바꿔 뒀어요). ({e})")
            continue
        if os.path.exists(os.path.join(POSTED, pid, "result.json")) and not card.get("repost"):
            shutil.rmtree(folder)
            log(f"↩ {pid}: 이미 올린 게시물이라 카드를 만들지 않고 대기열에서 뺐어요")
            summary(f"### ↩ {pid}: 이미 올린 게시물이라 대기열에서 뺐어요")
            continue
        notes = []
        if card.get("date") and card["date"] != pid:
            notes.append(f"카드의 게시일({card['date']})과 폴더 이름({pid})이 달라 폴더 이름을 따라요")
        if card.get("kit") and card["kit"] != kit.kit_md5():
            notes.append("Claude가 쓴 카드 스크립트와 저장소 kit 버전이 달라요 — 그림이 조금 다를 수 있어요")
        publish_at = card.get("publish_at")
        if publish_at and card.get("date") and card["date"] != pid:
            publish_at = None  # 폴더 날짜의 12:00 으로
        tmp = tempfile.mkdtemp()
        try:
            files, msgs = kit.build({"insta": card["insta"]}, tmp, poses, card.get("handle") or None, "4x5",
                                    QUEUE, pid, publish_at)
        except (Exception, SystemExit) as e:
            failed += 1
            set_aside(path)
            log(f"❌ {pid}: 카드를 만들지 못했어요 ({e})")
            summary(f"### ❌ {pid}: 카드를 만들지 못했어요\n{e}\n\ncard.txt는 card_실패.txt로 바꿔 뒀어요. Claude에게 알려 주세요.")
            continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        lines = [f"### 🎨 {pid} 카드 {len(files)}장 완료 — 게시 예정 {publish_at or pid + ' 12:00'}"]
        lines += [f"- ⚠ {m}" for m in notes + msgs]
        summary("\n".join(lines))
        log(f"✅ {pid}: 카드 {len(files)}장")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
