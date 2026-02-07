# findpage/slack_bot.py
import os
import re
import threading

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from find_page import scan_for_slack, detect_platform_from_product_url

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
TARGET_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")  # 비워두면 모든 채널

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


def post_thread(client, channel: str, thread_ts: str, text: str):
    # ✅ 항상 스레드에만 답글
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


def run_scan_and_reply(client, channel: str, thread_ts: str, product_url: str):
    try:
        platform, template_url = detect_platform_from_product_url(product_url)
        if not platform:
            post_thread(
                client,
                channel,
                thread_ts,
                f"❌ 처음 보는 페이지 패턴입니다.\n입력한 주소: {product_url}",
            )
            return

        # ✅ 스캔 시작 전에 플랫폼/패턴 포함해서 안내
        post_thread(
            client,
            channel,
            thread_ts,
            "🔎 스캔 시작\n"
            f"- 감지 플랫폼: {platform}\n"
            f"- 스캔 패턴: {template_url}\n"
            f"- 속도: 1초 1회\n"
            f"- 중단: 연속 100회 NOT FOUND/ERROR\n"
            f"- 추가: 초반 0건이면 100회 1회 추가, "
            f"또는 (발견 수 < 입력 제품ID*0.01)면 입력 제품ID부터 재스캔",
        )

        results = scan_for_slack(product_url)

        post_thread(
            client,
            channel,
            thread_ts,
            "✅ 스캔 결과\n\n" + format_results(results),
        )

    except Exception as e:
        post_thread(
            client,
            channel,
            thread_ts,
            f"❌ 스캔 중 오류 발생\n{type(e).__name__}: {e}",
        )


@app.event("message")
def handle_message_events(body, event, client, logger):
    if event.get("subtype"):
        return

    channel = event.get("channel")
    if TARGET_CHANNEL_ID and channel != TARGET_CHANNEL_ID:
        return

    text = event.get("text", "")
    ts = event.get("ts")  # 원문 메시지 ts = 스레드 루트

    url = extract_first_url(text)
    if not url:
        return

    # ✅ 채널에 새 메시지 만들지 않고 스레드에만
    post_thread(client, channel, ts, f"URL을 감지했습니다. 확인해 보겠습니다!")

    t = threading.Thread(
        target=run_scan_and_reply,
        args=(client, channel, ts, url),
        daemon=True,
    )
    t.start()


if __name__ == "__main__":
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
