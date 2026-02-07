# findpage/slack_bot.py
import os
import re
import threading

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ✅ find_page.py에 추가한 wrapper 함수 사용
from find_page import scan_for_slack

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
TARGET_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")  # 특정 채널만 감지(비워두면 모든 채널)

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
    """
    Slack 메시지로 요약 출력 (제품명 + URL)
    """
    if not results:
        return "찾은 제품 페이지가 없습니다."

    lines = []
    for i, (name, url) in enumerate(results, 1):
        lines.append(f"{i}. {name}\n{url}")
    return "\n\n".join(lines)


def run_scan_and_reply(client, channel: str, thread_ts: str, product_url: str):
    """
    백그라운드 스레드에서 스캔 실행 → 스레드에 결과 업로드
    """
    try:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"🔎 스캔 시작\n입력 URL: {product_url}\n(1초에 1회 요청 / 연속 100번 실패 시 중단, 조건부 추가 스캔 포함)",
        )

        results = scan_for_slack(product_url)

        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="✅ 스캔 결과\n\n" + format_results(results),
        )

    except Exception as e:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"❌ 스캔 중 오류 발생\n{type(e).__name__}: {e}",
        )


@app.event("message")
def handle_message_events(body, event, client, logger):
    # 봇 메시지/수정/알림 등 subtype 이벤트는 무시
    if event.get("subtype"):
        return

    channel = event.get("channel")

    # 특정 채널만 감지하도록 제한
    if TARGET_CHANNEL_ID and channel != TARGET_CHANNEL_ID:
        return

    text = event.get("text", "")
    ts = event.get("ts")  # 원문 메시지 ts를 thread_ts로 사용

    url = extract_first_url(text)
    if not url:
        return

    # 즉시 스레드에 "감지" 메시지 남기고, 스캔은 별도 스레드에서 실행
    client.chat_postMessage(
        channel=channel,
        thread_ts=ts,
        text=f"URL 감지 ✅\n{url}\n스캔을 시작합니다.",
    )

    t = threading.Thread(
        target=run_scan_and_reply,
        args=(client, channel, ts, url),
        daemon=True,
    )
    t.start()


if __name__ == "__main__":
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
