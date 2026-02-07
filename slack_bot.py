# findpage/slack_bot.py
import os
import re
import threading
from dotenv import load_dotenv

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# --- 여기서는 find_page.py의 핵심 함수들만 import해서 재사용하는 걸 권장 ---
# 만약 find_page.py가 아직 CLI 중심이라면, 아래 TODO대로 함수만 꺼내면 됩니다.
from find_page import (
    detect_platform_from_product_url,
    scan,  # scan(template_url) -> list[(name, url)]
)

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
TARGET_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")  # 특정 채널만 감지

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    raise RuntimeError("SLACK_BOT_TOKEN / SLACK_APP_TOKEN 을 .env에 설정하세요.")

app = App(token=SLACK_BOT_TOKEN)

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

def extract_first_url(text: str) -> str | None:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0) if m else None

def format_results(results: list[tuple[str, str]]) -> str:
    if not results:
        return "찾은 제품 페이지가 없습니다."
    lines = []
    for i, (name, url) in enumerate(results, 1):
        lines.append(f"{i}. {name}\n{url}")
    return "\n\n".join(lines)

def run_scan_and_reply(client, channel: str, thread_ts: str, product_url: str):
    # 1) 플랫폼/템플릿 감지
    platform, template_url = detect_platform_from_product_url(product_url)
    if not platform:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"❌ 처음 보는 페이지 패턴입니다.\n입력한 주소: {product_url}\n\n지원:\n- 카페24: https://도메인/surl/p/숫자\n- 카페24(감지): https://도메인/product/.../숫자/category/...\n- 아임웹: https://도메인/Product/?idx=숫자",
        )
        return

    # 2) 시작 안내
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=f"🔎 스캔 시작\n- 감지 플랫폼: {platform}\n- 스캔 패턴: {template_url}\n- 속도: 1초 1회\n- 중단: 연속 30번 실패 (단, 처음 30번 내 0건이면 추가 30번 더 시도)",
    )

    # 3) 스캔 실행 (여기서 scan()은 기존 로직 그대로 사용)
    results = scan(template_url)

    # 4) 요약 업로드
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text="✅ 스캔 결과\n\n" + format_results(results),
    )

@app.event("message")
def handle_message_events(body, event, client, logger):
    # 메시지 이벤트 중 봇 메시지/수정 이벤트 등은 제외
    if event.get("subtype"):
        return

    channel = event.get("channel")
    if TARGET_CHANNEL_ID and channel != TARGET_CHANNEL_ID:
        return

    text = event.get("text", "")
    user = event.get("user")
    ts = event.get("ts")

    url = extract_first_url(text)
    if not url:
        return

    # 즉시 응답(ACK) 후 백그라운드 스레드에서 스캔 (슬랙 이벤트 처리 타임아웃 방지)
    client.chat_postMessage(
        channel=channel,
        thread_ts=ts,
        text=f"URL 감지: {url}\n스캔을 시작합니다…",
    )

    t = threading.Thread(
        target=run_scan_and_reply,
        args=(client, channel, ts, url),
        daemon=True,
    )
    t.start()

if __name__ == "__main__":
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
