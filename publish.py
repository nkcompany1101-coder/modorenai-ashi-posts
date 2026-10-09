"""schedule.json を見て、時刻が来た投稿をInstagramに出す。GitHub Actions から定期的に呼ばれる。

    python publish.py            # 時刻が来たものを投稿する
    python publish.py --dry-run  # 何が投稿されるかを表示するだけ

必要な環境変数: IG_USER_ID, ACCESS_TOKEN, GITHUB_REPOSITORY（owner/repo。画像のURLを作るのに使う）
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
SCHEDULE = ROOT / "schedule.json"
GRAPH = "https://graph.facebook.com/v23.0"
JST = timezone(timedelta(hours=9))


def api(method, path, **params):
    data = urllib.parse.urlencode({**params, "access_token": os.environ["ACCESS_TOKEN"]}).encode()
    url = f"{GRAPH}/{path}"
    req = urllib.request.Request(url + "?" + data.decode()) if method == "GET" else urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(json.load(e).get("error", {}).get("message", str(e))) from None


def wait_ready(container_id):
    for _ in range(75):  # 動画の取り込みは数分かかることがある
        status = api("GET", container_id, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"画像の取り込みに失敗しました（{status}）")
        time.sleep(4)
    raise RuntimeError("画像の取り込みが時間内に終わりませんでした")


def already_posted(ig, caption):
    """同じキャプションの投稿が直近にあれば、二重投稿を避けるためそのURLを返す。"""
    for m in api("GET", f"{ig}/media", fields="caption,permalink", limit=10).get("data", []):
        if (m.get("caption") or "").strip() == caption.strip():
            return m.get("permalink")
    return None


def publish(item):
    ig = os.environ["IG_USER_ID"]
    folder = ROOT / "posts" / item["id"]
    caption = (folder / "caption.txt").read_text(encoding="utf-8").strip()
    done = already_posted(ig, caption)
    if done:
        return done

    repo = os.environ["GITHUB_REPOSITORY"]
    base = f"https://raw.githubusercontent.com/{repo}/main/posts/{urllib.parse.quote(item['id'])}"
    if item.get("type") == "reel":
        reel = api("POST", f"{ig}/media", media_type="REELS", video_url=f"{base}/reel.mp4", caption=caption, share_to_feed="true")["id"]
        wait_ready(reel)
        media = api("POST", f"{ig}/media_publish", creation_id=reel)["id"]
        return api("GET", media, fields="permalink").get("permalink", "")

    images = sorted(p.name for p in folder.glob("*.jpg"))
    if not 2 <= len(images) <= 10:
        raise RuntimeError(f"画像は2〜10枚にしてください（{len(images)}枚）")
    children = []
    for name in images:
        child = api("POST", f"{ig}/media", image_url=f"{base}/{name}", is_carousel_item="true")["id"]
        wait_ready(child)
        children.append(child)
    carousel = api("POST", f"{ig}/media", media_type="CAROUSEL", children=",".join(children), caption=caption)["id"]
    wait_ready(carousel)
    media = api("POST", f"{ig}/media_publish", creation_id=carousel)["id"]
    return api("GET", media, fields="permalink").get("permalink", "")


def main():
    dry = "--dry-run" in sys.argv
    items = json.loads(SCHEDULE.read_text(encoding="utf-8"))
    now = datetime.now(JST)
    failed = False
    for item in items:
        if item["status"] != "scheduled" or datetime.fromisoformat(item["at"]) > now:
            continue
        if dry:
            print(f"投稿対象: {item['id']}（{item['at']}）")
            continue
        try:
            item["permalink"] = publish(item)
            item["status"] = "posted"
            item["posted_at"] = datetime.now(JST).isoformat(timespec="seconds")
            print(f"投稿しました: {item['id']} {item['permalink']}")
        except Exception as e:  # 1本の失敗で残りを止めない。失敗は記録して最後にエラー終了する
            item["status"] = "error"
            item["error"] = str(e)
            failed = True
            print(f"失敗: {item['id']} {e}")
        SCHEDULE.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
